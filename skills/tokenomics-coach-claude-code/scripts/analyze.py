#!/usr/bin/env python3
"""
Tokenomics Coach — deterministic cache-economics analyzer for Claude Code logs.

Parses ~/.claude/projects/*.jsonl and quantifies avoidable spend, focusing on
prompt-cache behaviour rather than prompt wording.

Key facts this encodes (verified against platform.claude.com docs, 2026-08-07):
  * Cache write multipliers vs base input: 5m = 1.25x, 1h = 2x. Read = 0.1x.
  * A cache READ refreshes the TTL at no cost. The clock therefore runs from the
    last request touching the prefix, not from session start.
  * TTL is measured from the START of the writing/reading request, not the end
    of its response — long streaming turns eat into the window.
  * Break-even: a 1h write needs 2 subsequent reads to beat no caching; a 5m
    write needs 1.
  * Claude Code uses 1h TTL on subscription auth, 5m on API-key auth.
    Subagents (isSidechain) are ALWAYS 5m.

Usage:
  python3 analyze.py --mode all --days 30 --pretty
  python3 analyze.py --mode cache --project ~/code/my-app --days 14 --pretty
"""

from __future__ import annotations
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
PRICING_PATH = Path(__file__).parent / "pricing.json"

TTL_1H_SECONDS = 3600
TTL_5M_SECONDS = 300
# Only flag a re-cache as material above this size; tiny rewrites are noise.
MIN_MATERIAL_REWRITE = 5_000
# A prefix must have been at least this large to count as "established" — below
# this, losing it costs nothing worth reporting.
MIN_ESTABLISHED_PREFIX = 20_000
# A turn reading less than this fraction of the established prefix has dropped it.
CACHE_DROP_RATIO = 0.5


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def load_pricing() -> dict:
    try:
        return json.loads(PRICING_PATH.read_text())
    except Exception as e:
        print(f"warning: could not load pricing.json ({e}); using fallback rates",
              file=sys.stderr)
        return {"models": {}, "families": {}, "fallback": {
            "input": 3.0, "output": 15.0,
            "cache_write_5m": 3.75, "cache_write_1h": 6.0, "cache_read": 0.3}}


def rates_for(model: str, pricing: dict) -> dict:
    """Resolve a model id to its rate table, falling back by family then default."""
    if not model or model == "<synthetic>":
        return pricing["fallback"]
    mn = model.lower()
    for entry in pricing.get("models", {}).values():
        for pat in entry.get("match", []):
            if pat.lower() in mn:
                return entry
    for family, key in pricing.get("families", {}).items():
        if family in mn:
            entry = pricing.get("models", {}).get(key)
            if entry:
                return entry
    return pricing["fallback"]


def cost_of(usage: dict, model: str, pricing: dict) -> float:
    """Exact cost of one request, splitting 1h and 5m cache writes."""
    r = rates_for(model, pricing)
    cc = usage.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens", 0)
    w5m = cc.get("ephemeral_5m_input_tokens", 0)
    # Older entries lack the breakdown; attribute the lump sum to the 5m rate.
    lump = usage.get("cache_creation_input_tokens", 0)
    if not cc and lump:
        w5m = lump
    return (
        usage.get("input_tokens", 0) / 1e6 * r["input"]
        + usage.get("output_tokens", 0) / 1e6 * r["output"]
        + w1h / 1e6 * r["cache_write_1h"]
        + w5m / 1e6 * r["cache_write_5m"]
        + usage.get("cache_read_input_tokens", 0) / 1e6 * r["cache_read"]
    )


# ---------------------------------------------------------------------------
# Project + file resolution
# ---------------------------------------------------------------------------

def escape_project_path(p: str) -> str:
    path = os.path.abspath(os.path.expanduser(p))
    return path.replace("/", "-").replace(".", "-")


def resolve_project_dirs(project: str | None, root: Path) -> list[Path]:
    if not root.exists():
        return []
    if not project:
        return [d for d in root.iterdir() if d.is_dir()]
    esc = escape_project_path(project)
    direct = root / esc
    if direct.exists():
        return [direct]
    stem = esc.lstrip("-")
    matches = [d for d in root.iterdir() if d.is_dir() and stem in d.name]
    return matches or [direct]


def list_jsonl(dirs: list[Path], days: int) -> list[Path]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for d in dirs:
        if not d.exists():
            continue
        for f in d.glob("*.jsonl"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) >= cutoff:
                    out.append(f)
            except OSError:
                continue
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_ts(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def collect(files: list[Path], days: int, pricing: dict) -> dict:
    """Build a deduplicated, chronologically ordered list of billable requests."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seen: set = set()
    requests: list[dict] = []
    compactions: list[dict] = []
    sessions_meta: dict[str, dict] = defaultdict(
        lambda: {"project": None, "compactions": 0, "plan_toggles": 0})
    total_lines = duplicates = 0

    for f in files:
        project = f.parent.name
        # permissionMode is emitted as its own entry; carry it forward in file order.
        current_mode = "default"
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for line in text.splitlines():
            if not line.strip():
                continue
            total_lines += 1
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            sid = d.get("sessionId") or d.get("session_id") or "unknown"

            if d.get("type") == "permission-mode":
                new_mode = d.get("permissionMode", "default")
                if new_mode != current_mode:
                    sessions_meta[sid]["plan_toggles"] += 1
                current_mode = new_mode
                continue

            if d.get("subtype") == "compact_boundary" or d.get("isCompactSummary"):
                ts = parse_ts(d.get("timestamp") or "")
                sessions_meta[sid]["compactions"] += 1
                compactions.append({"session": sid, "project": project,
                                    "ts": ts.isoformat() if ts else None})
                continue

            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not usage:
                continue
            ts = parse_ts(d.get("timestamp") or "")
            if ts is None or ts < cutoff:
                continue

            key = (msg.get("id"), d.get("requestId"))
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)

            cc = usage.get("cache_creation") or {}
            lump = usage.get("cache_creation_input_tokens", 0)
            w1h = cc.get("ephemeral_1h_input_tokens", 0)
            w5m = cc.get("ephemeral_5m_input_tokens", 0)
            if not cc and lump:
                w5m = lump

            model = msg.get("model") or "<unknown>"
            sessions_meta[sid]["project"] = project

            requests.append({
                "ts": ts,
                "session": sid,
                "project": project,
                "model": model,
                "sidechain": bool(d.get("isSidechain")),
                "mode": current_mode,
                "effort": d.get("effort"),
                "write_1h": w1h,
                "write_5m": w5m,
                "read": usage.get("cache_read_input_tokens", 0),
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "cost": cost_of(usage, model, pricing),
            })

    requests.sort(key=lambda r: r["ts"])
    return {"requests": requests, "compactions": compactions,
            "sessions_meta": dict(sessions_meta),
            "meta": {"files_scanned": len(files), "total_lines": total_lines,
                     "duplicate_entries": duplicates, "unique_requests": len(requests),
                     "days": days}}


# ---------------------------------------------------------------------------
# Analysis: cache economics
# ---------------------------------------------------------------------------

def observed_ttl(rows: list[dict], sidechain: bool) -> tuple[int, str]:
    """Derive the actual TTL from which ephemeral bucket the lane wrote into.

    Claude Code does not log the auth type, but the TTL is directly observable:
    subscription auth writes 1h entries, API-key auth writes 5m. Subagents are
    always 5m. Fall back to the documented default only if nothing was written.
    """
    w1h = sum(r["write_1h"] for r in rows)
    w5m = sum(r["write_5m"] for r in rows)
    if w1h and w1h >= w5m:
        return TTL_1H_SECONDS, "observed_1h"
    if w5m:
        return TTL_5M_SECONDS, "observed_5m"
    return (TTL_5M_SECONDS, "assumed_5m_subagent") if sidechain else \
           (TTL_1H_SECONDS, "assumed_1h_default")


def analyze_cache(data: dict, pricing: dict) -> dict:
    """Detect every collapse of an established cache, and attribute a cause.

    A "cache drop" is a turn that rebuilds a prefix which was warm on the
    previous turn. TTL expiry is only one cause; model switches, tool/system
    changes (CLAUDE.md edits, MCP servers connecting) and session resumes drop
    the cache with no idle gap at all. Compaction also drops it, but legitimately.
    """
    lanes: dict[tuple, list] = defaultdict(list)
    for r in data["requests"]:
        lanes[(r["session"], r["sidechain"])].append(r)

    # Compaction timestamps per session, so drops can be excused.
    compact_ts: dict[str, list] = defaultdict(list)
    for c in data["compactions"]:
        if c["ts"]:
            t = parse_ts(c["ts"])
            if t:
                compact_ts[c["session"]].append(t)

    drops = []
    unamortized = []
    ttl_modes = Counter()
    total_w1h = total_w5m = total_read = 0

    for (sid, sidechain), rows in lanes.items():
        ttl, ttl_source = observed_ttl(rows, sidechain)
        ttl_modes[ttl_source] += 1
        prev = None

        for i, r in enumerate(rows):
            total_w1h += r["write_1h"]
            total_w5m += r["write_5m"]
            total_read += r["read"]
            written = r["write_1h"] + r["write_5m"]

            if prev is not None and written >= MIN_MATERIAL_REWRITE:
                # Was a prefix established, and did this turn stop reading it?
                established = prev["read"] + prev["write_1h"] + prev["write_5m"]
                dropped = (established >= MIN_ESTABLISHED_PREFIX
                           and r["read"] < established * CACHE_DROP_RATIO)
                if dropped:
                    gap = (r["ts"] - prev["ts"]).total_seconds()
                    compacted = any(prev["ts"] < t <= r["ts"]
                                    for t in compact_ts.get(sid, []))
                    if compacted:
                        cause = "compaction"
                    elif gap > ttl:
                        cause = "ttl_expiry"
                    elif r["model"] != prev["model"]:
                        cause = "model_switch"
                    else:
                        # Warm window, same model, no compaction: something
                        # invalidated the prefix — tools, system prompt, or a resume.
                        cause = "invalidation_no_gap"

                    rt = rates_for(r["model"], pricing)
                    wr = rt["cache_write_1h"] if r["write_1h"] else rt["cache_write_5m"]
                    paid = written / 1e6 * wr
                    would_have = written / 1e6 * rt["cache_read"]
                    drops.append({
                        "cause": cause,
                        "session": sid[:8], "project": r["project"],
                        "at": r["ts"].isoformat(),
                        "idle_minutes": round(gap / 60, 1),
                        "ttl_minutes": ttl // 60, "ttl_source": ttl_source,
                        "sidechain": sidechain,
                        "prefix_lost_tokens": established,
                        "tokens_rewritten": written,
                        "model": r["model"],
                        "model_changed_from": prev["model"] if cause == "model_switch" else None,
                        "paid_usd": round(paid, 4),
                        "read_would_have_cost_usd": round(would_have, 4),
                        "avoidable_usd": round(paid - would_have, 4),
                    })

            if r["write_1h"] >= MIN_MATERIAL_REWRITE:
                later_reads = sum(1 for x in rows[i + 1:] if x["read"] > 0)
                if later_reads < 2:
                    rt = rates_for(r["model"], pricing)
                    penalty = r["write_1h"] / 1e6 * (rt["cache_write_1h"] - rt["input"])
                    unamortized.append({
                        "session": sid[:8], "at": r["ts"].isoformat(),
                        "tokens": r["write_1h"], "subsequent_reads": later_reads,
                        "premium_paid_usd": round(penalty, 4),
                    })
            prev = r

    drops.sort(key=lambda e: -e["avoidable_usd"])
    unamortized.sort(key=lambda e: -e["premium_paid_usd"])
    total_cost = sum(r["cost"] for r in data["requests"])

    def bucket(cause: str) -> dict:
        rows = [d for d in drops if d["cause"] == cause]
        return {
            "event_count": len(rows),
            "tokens_rewritten": sum(d["tokens_rewritten"] for d in rows),
            "avoidable_usd": round(sum(d["avoidable_usd"] for d in rows), 2),
            "pct_of_total_spend": round(
                sum(d["avoidable_usd"] for d in rows) / max(total_cost, 1e-9) * 100, 1),
            "worst": rows[:10],
        }

    avoidable_causes = ["ttl_expiry", "model_switch", "invalidation_no_gap"]
    avoidable = sum(d["avoidable_usd"] for d in drops if d["cause"] in avoidable_causes)
    premium = sum(e["premium_paid_usd"] for e in unamortized)

    return {
        "totals": {
            "cache_write_1h_tokens": total_w1h,
            "cache_write_5m_tokens": total_w5m,
            "cache_read_tokens": total_read,
            "read_to_write_ratio": round(total_read / max(total_w1h + total_w5m, 1), 1),
            "total_cost_usd": round(total_cost, 2),
            "ttl_detection": dict(ttl_modes),
        },
        "avoidable_total_usd": round(avoidable + premium, 2),
        "cache_drops": {
            "note": "A cache drop is a turn that rebuilds a prefix that was warm "
                    "on the previous turn. Cause is attributed, not assumed.",
            "ttl_expiry": bucket("ttl_expiry"),
            "model_switch": bucket("model_switch"),
            "invalidation_no_gap": {
                **bucket("invalidation_no_gap"),
                "note": "Cache collapsed inside the TTL window, same model, no "
                        "compaction. Typical causes: CLAUDE.md or system prompt "
                        "edit, an MCP server connecting, a tool-definition change, "
                        "or a session resume. Not provable from logs alone.",
            },
            "compaction": {
                **bucket("compaction"),
                "note": "Expected and usually worthwhile — not counted as waste.",
            },
        },
        "unamortized_1h_writes": {
            "count": len(unamortized),
            "premium_paid_usd": round(premium, 2),
            "note": "1h writes cost 2x input and need >=2 later cache reads to beat "
                    "not caching. These did not reach that.",
            "worst": unamortized[:10],
        },
    }


# ---------------------------------------------------------------------------
# Analysis: session hygiene
# ---------------------------------------------------------------------------

def analyze_sessions(data: dict, pricing: dict) -> dict:
    by_session: dict[str, list] = defaultdict(list)
    for r in data["requests"]:
        if not r["sidechain"]:
            by_session[r["session"]].append(r)

    rows = []
    for sid, reqs in by_session.items():
        if not reqs:
            continue
        meta = data["sessions_meta"].get(sid, {})
        span = (reqs[-1]["ts"] - reqs[0]["ts"]).total_seconds() / 60
        gaps = [(reqs[i]["ts"] - reqs[i - 1]["ts"]).total_seconds() / 60
                for i in range(1, len(reqs))]
        peak_read = max((r["read"] for r in reqs), default=0)
        rows.append({
            "session": sid[:8],
            "project": reqs[0]["project"],
            "started": reqs[0]["ts"].isoformat(),
            "turns": len(reqs),
            "span_minutes": round(span, 1),
            "cost_usd": round(sum(r["cost"] for r in reqs), 3),
            "peak_context_tokens": peak_read,
            "gaps_over_ttl": sum(1 for g in gaps if g > 60),
            "longest_gap_minutes": round(max(gaps), 1) if gaps else 0,
            "compactions": meta.get("compactions", 0),
            "models": sorted({r["model"] for r in reqs}),
        })

    rows.sort(key=lambda s: -s["cost_usd"])

    # A session is "bloated" when it carries a large context for many turns and
    # was never compacted — every turn re-reads the whole prefix at 0.1x.
    bloated = [s for s in rows
               if s["peak_context_tokens"] > 150_000 and s["compactions"] == 0
               and s["turns"] >= 10]
    resumed_cold = [s for s in rows if s["gaps_over_ttl"] > 0]

    return {
        "session_count": len(rows),
        "total_cost_usd": round(sum(s["cost_usd"] for s in rows), 2),
        "never_compacted_large_sessions": {
            "count": len(bloated),
            "cost_usd": round(sum(s["cost_usd"] for s in bloated), 2),
            "note": "Large context, 10+ turns, zero compaction. Every turn pays a "
                    "cache read across the full prefix.",
            "sessions": bloated[:10],
        },
        "resumed_after_ttl": {
            "count": len(resumed_cold),
            "note": "Sessions containing at least one idle gap longer than the 1h "
                    "cache TTL. Each such gap forces a full re-cache.",
            "sessions": resumed_cold[:10],
        },
        "most_expensive": rows[:15],
    }


# ---------------------------------------------------------------------------
# Analysis: model strategy
# ---------------------------------------------------------------------------

def analyze_models(data: dict, pricing: dict) -> dict:
    by_model = defaultdict(lambda: {"requests": 0, "cost_usd": 0.0, "output_tokens": 0})
    by_mode = defaultdict(lambda: defaultdict(lambda: {"requests": 0, "cost_usd": 0.0}))
    switches = []

    by_session: dict[str, list] = defaultdict(list)
    for r in data["requests"]:
        m = by_model[r["model"]]
        m["requests"] += 1
        m["cost_usd"] += r["cost"]
        m["output_tokens"] += r["output"]
        slot = by_mode[r["mode"]][r["model"]]
        slot["requests"] += 1
        slot["cost_usd"] += r["cost"]
        if not r["sidechain"]:
            by_session[r["session"]].append(r)

    for sid, reqs in by_session.items():
        for i in range(1, len(reqs)):
            a, b = reqs[i - 1], reqs[i]
            if a["model"] != b["model"]:
                written = b["write_1h"] + b["write_5m"]
                rt = rates_for(b["model"], pricing)
                wr = rt["cache_write_1h"] if b["write_1h"] else rt["cache_write_5m"]
                switches.append({
                    "session": sid[:8], "at": b["ts"].isoformat(),
                    "from": a["model"], "to": b["model"],
                    "recache_tokens": written,
                    "recache_cost_usd": round(written / 1e6 * wr, 4),
                })

    for v in by_model.values():
        v["cost_usd"] = round(v["cost_usd"], 3)
    modes = {k: {m: {"requests": s["requests"], "cost_usd": round(s["cost_usd"], 3)}
                 for m, s in v.items()} for k, v in by_mode.items()}

    switches.sort(key=lambda s: -s["recache_cost_usd"])
    opus_cost = sum(v["cost_usd"] for k, v in by_model.items() if "opus" in k.lower())
    total = sum(v["cost_usd"] for v in by_model.values())

    return {
        "by_model": dict(by_model),
        "by_permission_mode": modes,
        "note_plan_vs_execute": "permissionMode 'plan' marks planning turns; "
                                "'default'/'auto' are execution. The opusplan alias "
                                "runs Opus for planning and Sonnet for execution.",
        "opus_share_of_spend_pct": round(opus_cost / max(total, 1e-9) * 100, 1),
        "model_switches": {
            "count": len(switches),
            "recache_cost_usd": round(sum(s["recache_cost_usd"] for s in switches), 2),
            "note": "Switching models mid-session invalidates the cached prefix.",
            "worst": switches[:10],
        },
    }


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Tokenomics analyzer for Claude Code logs")
    ap.add_argument("--mode", default="all",
                    choices=["all", "cache", "sessions", "models"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--project", type=str, default=None)
    ap.add_argument("--projects-root", type=str, default=str(DEFAULT_PROJECTS_ROOT))
    ap.add_argument("--output", type=str, default=None)
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    pricing = load_pricing()
    root = Path(os.path.expanduser(args.projects_root))
    dirs = resolve_project_dirs(args.project, root)
    if not dirs:
        print(f"error: no project logs found under {root}", file=sys.stderr)
        return 1
    files = list_jsonl(dirs, args.days)
    if not files:
        print(f"error: no .jsonl logs modified in the last {args.days} days "
              f"under {root}", file=sys.stderr)
        return 1

    data = collect(files, args.days, pricing)
    if not data["requests"]:
        print(f"error: scanned {len(files)} files but found no billable requests "
              f"in the last {args.days} days", file=sys.stderr)
        return 1

    out = {"meta": data["meta"]}
    if args.mode in ("all", "cache"):
        out["cache"] = analyze_cache(data, pricing)
    if args.mode in ("all", "sessions"):
        out["sessions"] = analyze_sessions(data, pricing)
    if args.mode in ("all", "models"):
        out["models"] = analyze_models(data, pricing)

    text = json.dumps(out, indent=2 if args.pretty else None, default=str)
    if args.output:
        Path(os.path.expanduser(args.output)).write_text(text)
        print(f"wrote {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    human_prompts: list[dict] = []
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

            # A real human prompt, as opposed to a tool result routed back as a
            # 'user' entry. Tool results carry a list of tool_result blocks.
            if d.get("type") == "user" and not d.get("isMeta"):
                content = msg.get("content")
                is_human = isinstance(content, str) or (
                    isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "text"
                        for b in content))
                if is_human:
                    t = parse_ts(d.get("timestamp") or "")
                    if t and t >= cutoff:
                        human_prompts.append({"ts": t, "session": sid})
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
    human_prompts.sort(key=lambda h: h["ts"])
    return {"requests": requests, "human_prompts": human_prompts,
            "compactions": compactions,
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

    # Human prompt timestamps per session, to separate "the user walked away"
    # from "a long tool loop ran". Both widen the gap; only one is a habit.
    human: dict[str, list] = defaultdict(list)
    for h in data["human_prompts"]:
        human[h["session"]].append(h["ts"])
    for v in human.values():
        v.sort()

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

                    # Split the gap: how long the human was away, versus how long
                    # the machine was busy before this request went out.
                    trigger = next((t for t in human.get(sid, [])
                                    if prev["ts"] < t <= r["ts"]), None)
                    if trigger:
                        human_idle = (trigger - prev["ts"]).total_seconds()
                        machine = (r["ts"] - trigger).total_seconds()
                    else:
                        human_idle, machine = 0.0, gap
                    if cause == "ttl_expiry":
                        # 'no_human_input' means no human prompt fell in the gap at
                        # all. That covers a long tool execution, the session sitting
                        # on a permission approval (approvals are not logged as user
                        # messages), or the machine sleeping — indistinguishable here.
                        subcause = ("human_away" if human_idle > ttl
                                    else "no_human_input" if not trigger
                                    else "combined")
                    else:
                        subcause = None

                    rt = rates_for(r["model"], pricing)
                    wr = rt["cache_write_1h"] if r["write_1h"] else rt["cache_write_5m"]
                    paid = written / 1e6 * wr
                    would_have = written / 1e6 * rt["cache_read"]
                    drops.append({
                        "cause": cause,
                        "subcause": subcause,
                        "session": sid[:8], "project": r["project"],
                        "at": r["ts"].isoformat(),
                        "idle_minutes": round(gap / 60, 1),
                        "human_idle_minutes": round(human_idle / 60, 1),
                        "machine_busy_minutes": round(machine / 60, 1),
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

    # What would execution-phase Opus turns have cost on Sonnet? This is the
    # opusplan opportunity, net of the re-cache tax that toggling would add.
    sonnet = rates_for("claude-sonnet-4-6", pricing)
    exec_opus_actual = exec_opus_on_sonnet = 0.0
    exec_opus_requests = 0
    for r in data["requests"]:
        if r["mode"] == "plan" or "opus" not in r["model"].lower():
            continue
        exec_opus_requests += 1
        exec_opus_actual += r["cost"]
        exec_opus_on_sonnet += (
            r["input"] / 1e6 * sonnet["input"]
            + r["output"] / 1e6 * sonnet["output"]
            + r["write_1h"] / 1e6 * sonnet["cache_write_1h"]
            + r["write_5m"] / 1e6 * sonnet["cache_write_5m"]
            + r["read"] / 1e6 * sonnet["cache_read"]
        )
    switch_tax = sum(s["recache_cost_usd"] for s in switches)

    return {
        "by_model": dict(by_model),
        "by_permission_mode": modes,
        "opusplan_opportunity": {
            "execution_opus_requests": exec_opus_requests,
            "execution_opus_cost_usd": round(exec_opus_actual, 2),
            "same_work_on_sonnet_usd": round(exec_opus_on_sonnet, 2),
            "gross_saving_usd": round(exec_opus_actual - exec_opus_on_sonnet, 2),
            "observed_switch_tax_usd": round(switch_tax, 2),
            "net_saving_usd": round(exec_opus_actual - exec_opus_on_sonnet - switch_tax, 2),
            "note": "Rate-difference estimate for non-plan-mode Opus turns if run on "
                    "Sonnet 4.6, less the re-cache cost already observed from model "
                    "switching. Assumes equal token counts, which is optimistic: "
                    "weaker models may iterate more.",
        },
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
# Daily series
# ---------------------------------------------------------------------------

def analyze_daily(data: dict) -> list[dict]:
    """Per-day cost split, so re-cache spikes are visible against normal spend."""
    days: dict[str, dict] = defaultdict(
        lambda: {"read_usd": 0.0, "write_usd": 0.0, "output_usd": 0.0,
                 "cost_usd": 0.0, "requests": 0})
    pricing = load_pricing()
    for r in data["requests"]:
        d = days[r["ts"].date().isoformat()]
        rt = rates_for(r["model"], pricing)
        d["read_usd"] += r["read"] / 1e6 * rt["cache_read"]
        d["write_usd"] += (r["write_1h"] / 1e6 * rt["cache_write_1h"]
                           + r["write_5m"] / 1e6 * rt["cache_write_5m"])
        d["output_usd"] += r["output"] / 1e6 * rt["output"]
        d["cost_usd"] += r["cost"]
        d["requests"] += 1
    return [{"date": k, **{kk: round(vv, 4) if isinstance(vv, float) else vv
                           for kk, vv in v.items()}}
            for k, v in sorted(days.items())]


# ---------------------------------------------------------------------------
# Deterministic HTML report
# ---------------------------------------------------------------------------

def _actions(out: dict) -> list[dict]:
    """Prioritized, dollar-weighted behaviour changes derived from the findings."""
    acts = []
    cache = out.get("cache", {})
    drops = cache.get("cache_drops", {})
    sess = out.get("sessions", {})
    models = out.get("models", {})

    # TTL expiry splits by who was idle — the verdicts are different.
    t = drops.get("ttl_expiry", {})
    ev = t.get("worst") or []
    away = [e for e in ev if e.get("subcause") == "human_away"]
    nohuman = [e for e in ev if e.get("subcause") == "no_human_input"]
    if away:
        w = away[0]
        acts.append({"usd": round(sum(e["avoidable_usd"] for e in away), 2),
                     "title": "Clear before long breaks",
                     "detail": f"{len(away)} re-cache(s) where you were away longer than the "
                               f"TTL. Worst: session {w['session']} idle "
                               f"{w['human_idle_minutes']} min on a "
                               f"{w['prefix_lost_tokens']:,}-token prefix. Use /clear before "
                               f"stepping away, then start fresh."})
    if nohuman:
        w = nohuman[0]
        acts.append({"usd": round(sum(e["avoidable_usd"] for e in nohuman), 2),
                     "title": "Split very long unattended runs",
                     "detail": f"{len(nohuman)} re-cache(s) with no human prompt in the gap — "
                               f"worst was {w['machine_busy_minutes']} min of machine time on a "
                               f"{w['prefix_lost_tokens']:,}-token prefix. A long tool loop, a "
                               f"session parked on a permission approval, or a sleeping machine "
                               f"will all outrun the TTL. Approve promptly, and break multi-hour "
                               f"unattended runs into stages. /clear will not help here."})

    u = cache.get("unamortized_1h_writes", {})
    if u.get("premium_paid_usd", 0) > 0:
        acts.append({"usd": u["premium_paid_usd"], "title": "Consolidate short sessions",
                     "detail": f"{u['count']} one-hour cache writes never reached the 2 reads "
                               f"needed to break even, paying a 2x premium for nothing."})

    op = models.get("opusplan_opportunity", {})
    if op.get("net_saving_usd", 0) > 0:
        acts.append({"usd": op["net_saving_usd"], "title": "Run execution on Sonnet",
                     "detail": f"{op['execution_opus_requests']} Opus turns outside plan mode cost "
                               f"${op['execution_opus_cost_usd']}; the same tokens on Sonnet 4.6 "
                               f"would be ${op['same_work_on_sonnet_usd']}. Net of the "
                               f"${op['observed_switch_tax_usd']} switch tax. Consider opusplan."})

    b = sess.get("never_compacted_large_sessions", {})
    if b.get("count", 0) > 0:
        acts.append({"usd": 0.0, "title": "Compact long sessions at task boundaries",
                     "detail": f"{b['count']} session(s) over 150k context with 10+ turns and zero "
                               f"compaction, totalling ${b.get('cost_usd', 0)}. Compact while the "
                               f"cache is warm — compacting cold costs far more."})

    i = drops.get("invalidation_no_gap", {})
    if i.get("avoidable_usd", 0) > 0:
        acts.append({"usd": i["avoidable_usd"], "title": "Change config between sessions",
                     "detail": f"{i['event_count']} cache collapse(s) inside the TTL window with no "
                               f"compaction and no model change. Likely a CLAUDE.md edit, an MCP "
                               f"server connecting, or a resume. Cause is not provable from logs."})

    acts.sort(key=lambda a: -a["usd"])
    return acts


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _chart(daily: list[dict]) -> str:
    """Stacked bar chart of daily spend as inline SVG. No external libraries."""
    if not daily:
        return "<p class='muted'>No daily data.</p>"
    W, H, PAD_L, PAD_B, PAD_T = 900, 260, 52, 46, 12
    peak = max((d["cost_usd"] for d in daily), default=0) or 1
    n = len(daily)
    bw = max(4.0, (W - PAD_L - 12) / n * 0.72)
    step = (W - PAD_L - 12) / n
    parts = [f"<svg viewBox='0 0 {W} {H}' preserveAspectRatio='xMidYMid meet' "
             f"role='img' aria-label='Daily spend split by cost type'>"]
    # y gridlines
    for f in (0, 0.25, 0.5, 0.75, 1.0):
        y = PAD_T + (H - PAD_T - PAD_B) * (1 - f)
        parts.append(f"<line x1='{PAD_L}' y1='{y:.1f}' x2='{W-8}' y2='{y:.1f}' class='grid'/>")
        parts.append(f"<text x='{PAD_L-8}' y='{y+4:.1f}' class='ylab'>${peak*f:.2f}</text>")
    plot_h = H - PAD_T - PAD_B
    for i, d in enumerate(daily):
        x = PAD_L + i * step + (step - bw) / 2
        y = PAD_T + plot_h
        for key, cls in (("read_usd", "b-read"), ("write_usd", "b-write"),
                         ("output_usd", "b-out")):
            h = d[key] / peak * plot_h
            if h <= 0:
                continue
            y -= h
            parts.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw:.1f}' height='{h:.1f}' "
                         f"class='{cls}'><title>{d['date']} {key.replace('_usd','')}: "
                         f"${d[key]:.2f}</title></rect>")
        if n <= 20 or i % max(1, n // 12) == 0:
            parts.append(f"<text x='{x+bw/2:.1f}' y='{H-PAD_B+16}' class='xlab' "
                         f"transform='rotate(-45 {x+bw/2:.1f} {H-PAD_B+16})'>"
                         f"{d['date'][5:]}</text>")
    parts.append("</svg>")
    return "".join(parts)


def _table(headers: list[str], rows: list[list], sortable=True) -> str:
    cls = " class='sortable'" if sortable else ""
    h = "".join(f"<th>{_esc(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>"
                   for r in rows)
    return (f"<div class='scroll'><table{cls}><thead><tr>{h}</tr></thead>"
            f"<tbody>{body}</tbody></table></div>")


CSS = """
:root{--bg:#fbfbfa;--panel:#fff;--fg:#1a1a19;--muted:#6b6b68;--line:#e5e5e2;
--accent:#c8613a;--read:#4a7a63;--write:#c8613a;--out:#7a6ba8;--warn:#b4442a}
:root:not([data-theme=light]){}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#191917;--panel:#222220;--fg:#f0efec;--muted:#a3a29d;--line:#34342f;
--accent:#e08a5f;--read:#6fae8f;--write:#e08a5f;--out:#a294cc;--warn:#e5714f}}
:root[data-theme=dark]{--bg:#191917;--panel:#222220;--fg:#f0efec;--muted:#a3a29d;
--line:#34342f;--accent:#e08a5f;--read:#6fae8f;--write:#e08a5f;--out:#a294cc;--warn:#e5714f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,
-apple-system,'Segoe UI',Roboto,sans-serif;padding:32px 20px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:17px;margin:34px 0 12px;letter-spacing:-.01em}
.muted{color:var(--muted)}
.sub{color:var(--muted);font-size:14px;margin:0 0 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:24px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.card.hot .v{color:var(--warn)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
ol.acts{list-style:none;counter-reset:a;padding:0;margin:0}
ol.acts li{counter-increment:a;border-bottom:1px solid var(--line);padding:13px 0}
ol.acts li:last-child{border-bottom:0}
ol.acts .hd{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
ol.acts .n{color:var(--muted);font-variant-numeric:tabular-nums}
ol.acts .t{font-weight:600}
ol.acts .amt{margin-left:auto;color:var(--warn);font-weight:600;font-variant-numeric:tabular-nums}
ol.acts .d{color:var(--muted);font-size:14px;margin-top:4px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
background:var(--panel);position:sticky;top:0}
table.sortable th{cursor:pointer;user-select:none}
table.sortable th:hover{color:var(--fg)}
tbody tr:last-child td{border-bottom:0}
td{font-variant-numeric:tabular-nums}
svg{width:100%;height:auto;display:block}
.grid{stroke:var(--line);stroke-width:1}
.ylab{fill:var(--muted);font-size:10px;text-anchor:end}
.xlab{fill:var(--muted);font-size:10px;text-anchor:end}
.b-read{fill:var(--read)}.b-write{fill:var(--write)}.b-out{fill:var(--out)}
.legend{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;font-size:13px;color:var(--muted)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
footer{margin-top:36px;color:var(--muted);font-size:12.5px;border-top:1px solid var(--line);
padding-top:14px}
"""

JS = """
document.querySelectorAll('table.sortable').forEach(function(t){
  t.querySelectorAll('th').forEach(function(th,i){
    th.addEventListener('click',function(){
      var tb=t.tBodies[0], rows=Array.prototype.slice.call(tb.rows);
      var asc=th.dataset.asc!=='1'; th.dataset.asc=asc?'1':'0';
      rows.sort(function(a,b){
        var x=a.cells[i].textContent.trim(), y=b.cells[i].textContent.trim();
        var nx=parseFloat(x.replace(/[$,]/g,'')), ny=parseFloat(y.replace(/[$,]/g,''));
        if(!isNaN(nx)&&!isNaN(ny)) return asc?nx-ny:ny-nx;
        return asc?x.localeCompare(y):y.localeCompare(x);
      });
      rows.forEach(function(r){tb.appendChild(r)});
    });
  });
});
"""


def render_html(out: dict, path: Path) -> None:
    meta = out["meta"]
    cache = out.get("cache", {})
    totals = cache.get("totals", {})
    drops = cache.get("cache_drops", {})
    sess = out.get("sessions", {})
    models = out.get("models", {})
    daily = out.get("daily", [])
    acts = _actions(out)

    total = totals.get("total_cost_usd", 0)
    avoid = cache.get("avoidable_total_usd", 0)
    pct = round(avoid / total * 100, 1) if total else 0
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    cards = [("Window", f"{meta['days']} days", False),
             ("Total spend", f"${total:,.2f}", False),
             ("Avoidable", f"${avoid:,.2f}", True),
             ("Share wasted", f"{pct}%", True),
             ("Read/write ratio", f"{totals.get('read_to_write_ratio', 0)}x", False),
             ("Requests", f"{meta['unique_requests']:,}", False)]
    cards_html = "".join(
        f"<div class='card{' hot' if hot else ''}'><div class='k'>{_esc(k)}</div>"
        f"<div class='v'>{_esc(v)}</div></div>" for k, v, hot in cards)

    acts_html = "".join(
        f"<li><div class='hd'><span class='n'>{i+1}.</span>"
        f"<span class='t'>{_esc(a['title'])}</span>"
        f"<span class='amt'>{('$%.2f' % a['usd']) if a['usd'] else '—'}</span></div>"
        f"<div class='d'>{_esc(a['detail'])}</div></li>"
        for i, a in enumerate(acts)) or "<li class='muted'>No avoidable waste detected.</li>"

    # Cache drops by cause
    drop_rows = []
    for cause in ("ttl_expiry", "model_switch", "invalidation_no_gap", "compaction"):
        b = drops.get(cause, {})
        if not b:
            continue
        drop_rows.append([cause, b.get("event_count", 0),
                          f"{b.get('tokens_rewritten', 0):,}",
                          f"${b.get('avoidable_usd', 0):.2f}",
                          f"{b.get('pct_of_total_spend', 0)}%",
                          "no" if cause == "compaction" else "yes"])
    drops_html = _table(["Cause", "Events", "Tokens rewritten", "Cost",
                         "% of spend", "Avoidable"], drop_rows)

    # Worst individual events across avoidable causes
    ev = []
    for cause in ("ttl_expiry", "model_switch", "invalidation_no_gap"):
        for e in drops.get(cause, {}).get("worst", []):
            ev.append([f"${e['avoidable_usd']:.2f}", cause, e["session"],
                       e["at"][:16].replace("T", " "), f"{e['idle_minutes']}m",
                       f"{e['prefix_lost_tokens']:,}", f"{e['tokens_rewritten']:,}",
                       e["model"], e["project"][:44]])
    ev.sort(key=lambda r: -float(r[0][1:]))
    events_html = _table(["Cost", "Cause", "Session", "When", "Idle", "Prefix lost",
                          "Rewritten", "Model", "Project"], ev[:25])

    sess_html = _table(
        ["Cost", "Session", "Project", "Started", "Turns", "Span (min)",
         "Peak context", "Gaps>TTL", "Compactions"],
        [[f"${s['cost_usd']:.2f}", s["session"], s["project"][:44],
          s["started"][:16].replace("T", " "), s["turns"], s["span_minutes"],
          f"{s['peak_context_tokens']:,}", s["gaps_over_ttl"], s["compactions"]]
         for s in sess.get("most_expensive", [])])

    mode_rows = []
    for mode, mm in sorted(models.get("by_permission_mode", {}).items()):
        for model, s in sorted(mm.items(), key=lambda kv: -kv[1]["cost_usd"]):
            mode_rows.append(["planning" if mode == "plan" else f"execution ({mode})",
                              model, s["requests"], f"${s['cost_usd']:.2f}"])
    modes_html = _table(["Phase", "Model", "Requests", "Cost"], mode_rows)

    op = models.get("opusplan_opportunity", {})
    op_html = ""
    if op:
        op_html = (
            f"<div class='panel'><p style='margin:0 0 8px'><strong>opusplan estimate:</strong> "
            f"{op['execution_opus_requests']} Opus turns outside plan mode cost "
            f"<strong>${op['execution_opus_cost_usd']}</strong>. The same tokens on Sonnet 4.6 "
            f"would cost <strong>${op['same_work_on_sonnet_usd']}</strong> — a gross saving of "
            f"${op['gross_saving_usd']}, or <strong>${op['net_saving_usd']}</strong> net of the "
            f"${op['observed_switch_tax_usd']} re-cache tax already observed from model switching."
            f"</p><p class='muted' style='margin:0;font-size:13.5px'>{_esc(op['note'])}</p></div>")

    ttl = totals.get("ttl_detection", {})
    ttl_txt = ", ".join(f"{v} {k}" for k, v in sorted(ttl.items())) or "n/a"

    html = f"""<title>Tokenomics Report — {meta['days']}d</title>
<style>{CSS}</style>
<div class="wrap">
<h1>Claude Code Tokenomics</h1>
<p class="sub">Last {meta['days']} days · {meta['files_scanned']} log files ·
{meta['unique_requests']:,} unique requests ({meta['duplicate_entries']:,} duplicates removed) ·
generated {gen}</p>

<div class="cards">{cards_html}</div>

<h2>What to change</h2>
<div class="panel"><ol class="acts">{acts_html}</ol></div>

<h2>Daily spend</h2>
<div class="panel">{_chart(daily)}
<div class="legend">
<span><i style="background:var(--read)"></i>cache reads</span>
<span><i style="background:var(--write)"></i>cache writes</span>
<span><i style="background:var(--out)"></i>output</span>
</div></div>

<h2>Cache drops by cause</h2>
{drops_html}

<h2>Worst individual events</h2>
{events_html}

<h2>Most expensive sessions</h2>
{sess_html}

<h2>Planning vs execution</h2>
{modes_html}
{op_html}

<footer>
TTL detection: {_esc(ttl_txt)}. Cache writes:
{totals.get('cache_write_1h_tokens', 0):,} tokens at 1h,
{totals.get('cache_write_5m_tokens', 0):,} at 5m; reads
{totals.get('cache_read_tokens', 0):,}.<br>
Rates from <code>pricing.json</code>. Multipliers vs base input: 5m write 1.25x,
1h write 2x, read 0.1x. Cause attribution for <code>invalidation_no_gap</code> is an
inference — the logs do not record tool or system-prompt changes.
</footer>
</div>
<script>{JS}</script>
"""
    path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Tokenomics analyzer for Claude Code logs")
    ap.add_argument("--mode", default="all",
                    choices=["all", "cache", "sessions", "models"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--project", type=str, default=None)
    ap.add_argument("--projects-root", type=str, default=str(DEFAULT_PROJECTS_ROOT))
    ap.add_argument("--output", type=str, default=None, help="Write JSON here")
    ap.add_argument("--html", type=str, default=None,
                    help="HTML report path (default: ~/tokenomics-report-<date>.html)")
    ap.add_argument("--no-html", action="store_true", help="Skip the HTML report")
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

    # The HTML report always renders every section, so compute what it needs
    # even when the caller asked for a single mode.
    if not args.no_html:
        full = dict(out)
        full.setdefault("cache", analyze_cache(data, pricing))
        full.setdefault("sessions", analyze_sessions(data, pricing))
        full.setdefault("models", analyze_models(data, pricing))
        full["daily"] = analyze_daily(data)
        html_path = Path(os.path.expanduser(
            args.html or f"~/tokenomics-report-{datetime.now().date().isoformat()}.html"))
        try:
            html_path.parent.mkdir(parents=True, exist_ok=True)
            render_html(full, html_path)
            out["html_report"] = str(html_path)
        except OSError as e:
            print(f"warning: could not write HTML report ({e})", file=sys.stderr)

    text = json.dumps(out, indent=2 if args.pretty else None, default=str)
    if args.output:
        Path(os.path.expanduser(args.output)).write_text(text)
        print(f"wrote {args.output}")
        if out.get("html_report"):
            print(f"wrote {out['html_report']}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

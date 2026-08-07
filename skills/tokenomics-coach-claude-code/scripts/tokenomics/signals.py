"""Derived signals shared by more than one rule.

Computed once and handed to every rule, so rules stay independent of each other
without each re-walking the log. A rule may ignore anything it does not need.
"""

from __future__ import annotations
from collections import defaultdict

from .pricing import rates_for

TTL_1H_SECONDS = 3600
TTL_5M_SECONDS = 300

# Rewrites below this are ordinary per-turn deltas, not a rebuild.
MIN_MATERIAL_REWRITE = 5_000
# A prefix smaller than this is not worth reporting the loss of.
MIN_ESTABLISHED_PREFIX = 20_000
# Reading less than this share of the established prefix means it was dropped.
CACHE_DROP_RATIO = 0.5


def observed_ttl(rows: list[dict], sidechain: bool) -> tuple[int, str]:
    """The TTL this lane actually used, read off the log rather than assumed.

    Claude Code never records the auth type, but it does record which ephemeral
    bucket each write landed in: subscription auth writes 1h entries, API-key
    auth writes 5m. Subagents are always 5m.
    """
    w1h = sum(r["write_1h"] for r in rows)
    w5m = sum(r["write_5m"] for r in rows)
    if w1h and w1h >= w5m:
        return TTL_1H_SECONDS, "observed_1h"
    if w5m:
        return TTL_5M_SECONDS, "observed_5m"
    return (TTL_5M_SECONDS, "assumed_5m_subagent") if sidechain else \
           (TTL_1H_SECONDS, "assumed_1h_default")


def cache_drops(ds, pricing: dict) -> list[dict]:
    """Every turn that rebuilt a prefix which was warm on the previous turn.

    Cause is attributed here once, in priority order, so the cause-specific rules
    only have to interpret and cost their own bucket:

      compaction          - a compact boundary fell in between. Legitimate.
      ttl_expiry          - the gap exceeded this lane's TTL. Provable.
      model_switch        - the model changed. Provable.
      invalidation_no_gap - none of the above. Inferred, not proven.
    """
    compact_ts: dict[str, list] = defaultdict(list)
    for c in ds.compactions:
        if c["ts"]:
            compact_ts[c["session"]].append(c["ts"])

    human: dict[str, list] = defaultdict(list)
    for h in ds.human_prompts:
        human[h["session"]].append(h["ts"])
    for v in human.values():
        v.sort()

    drops = []
    for (sid, sidechain), rows in ds.lanes().items():
        ttl, ttl_source = observed_ttl(rows, sidechain)
        prev = None
        for r in rows:
            written = r["write_1h"] + r["write_5m"]
            if prev is None or written < MIN_MATERIAL_REWRITE:
                prev = r
                continue

            established = prev["read"] + prev["write_1h"] + prev["write_5m"]
            if not (established >= MIN_ESTABLISHED_PREFIX
                    and r["read"] < established * CACHE_DROP_RATIO):
                prev = r
                continue

            gap = (r["ts"] - prev["ts"]).total_seconds()
            compacted = any(prev["ts"] < t <= r["ts"] for t in compact_ts.get(sid, []))
            if compacted:
                cause = "compaction"
            elif gap > ttl:
                cause = "ttl_expiry"
            elif r["model"] != prev["model"]:
                cause = "model_switch"
            else:
                cause = "invalidation_no_gap"

            # Who was idle: the person, or the machine?
            trigger = next((t for t in human.get(sid, [])
                            if prev["ts"] < t <= r["ts"]), None)
            if trigger:
                human_idle = (trigger - prev["ts"]).total_seconds()
                machine = (r["ts"] - trigger).total_seconds()
            else:
                human_idle, machine = 0.0, gap

            subcause = None
            if cause == "ttl_expiry":
                # 'no_human_input' covers a long tool loop, a session sitting on
                # an unanswered permission approval, and a sleeping machine.
                # The log cannot tell these apart.
                subcause = ("human_away" if human_idle > ttl
                            else "no_human_input" if not trigger
                            else "combined")

            rt = rates_for(r["model"], pricing)
            wr = rt["cache_write_1h"] if r["write_1h"] else rt["cache_write_5m"]
            paid = written / 1e6 * wr
            would_have = written / 1e6 * rt["cache_read"]

            drops.append({
                "cause": cause, "subcause": subcause,
                "session": sid[:8], "session_id": sid,
                "title": ds.label(sid), "path": ds.path(sid, r["project"]),
                "project": r["project"], "at": r["ts"].isoformat(),
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
            prev = r

    drops.sort(key=lambda d: -d["avoidable_usd"])
    return drops


def session_rows(ds, pricing: dict) -> list[dict]:
    """One row per main-thread session, with the facts rules need to judge it."""
    by_session: dict[str, list] = defaultdict(list)
    for r in ds.requests:
        if not r["sidechain"]:
            by_session[r["session"]].append(r)

    rows = []
    for sid, reqs in by_session.items():
        if not reqs:
            continue
        meta = ds.sessions_meta.get(sid, {})
        gaps = [(reqs[i]["ts"] - reqs[i - 1]["ts"]).total_seconds() / 60
                for i in range(1, len(reqs))]
        rows.append({
            "session": sid[:8], "session_id": sid,
            "title": ds.label(sid), "path": ds.path(sid, reqs[0]["project"]),
            "project": reqs[0]["project"],
            "started": reqs[0]["ts"].isoformat(),
            "turns": len(reqs),
            "span_minutes": round((reqs[-1]["ts"] - reqs[0]["ts"]).total_seconds() / 60, 1),
            "cost_usd": round(sum(r["cost"] for r in reqs), 3),
            "peak_context_tokens": max((r["read"] for r in reqs), default=0),
            "gaps_over_ttl": sum(1 for g in gaps if g > 60),
            "longest_gap_minutes": round(max(gaps), 1) if gaps else 0,
            "compactions": meta.get("compactions", 0),
            "models": sorted({r["model"] for r in reqs}),
        })
    rows.sort(key=lambda s: -s["cost_usd"])
    return rows


def daily(ds, pricing: dict) -> list[dict]:
    """Per-day spend split, so rebuild spikes stand out against normal usage."""
    days: dict[str, dict] = defaultdict(
        lambda: {"read_usd": 0.0, "write_usd": 0.0, "output_usd": 0.0,
                 "cost_usd": 0.0, "requests": 0})
    for r in ds.requests:
        d = days[r["ts"].date().isoformat()]
        rt = rates_for(r["model"], pricing)
        d["read_usd"] += r["read"] / 1e6 * rt["cache_read"]
        d["write_usd"] += (r["write_1h"] / 1e6 * rt["cache_write_1h"]
                           + r["write_5m"] / 1e6 * rt["cache_write_5m"])
        d["output_usd"] += r["output"] / 1e6 * rt["output"]
        d["cost_usd"] += r["cost"]
        d["requests"] += 1
    return [{"date": k, **{kk: (round(vv, 4) if isinstance(vv, float) else vv)
                          for kk, vv in v.items()}}
            for k, v in sorted(days.items())]


def totals(ds, drops: list[dict], pricing: dict) -> dict:
    w1h = sum(r["write_1h"] for r in ds.requests)
    w5m = sum(r["write_5m"] for r in ds.requests)
    read = sum(r["read"] for r in ds.requests)
    ttl_modes: dict[str, int] = defaultdict(int)
    for (sid, sidechain), rows in ds.lanes().items():
        ttl_modes[observed_ttl(rows, sidechain)[1]] += 1
    return {
        "cache_write_1h_tokens": w1h,
        "cache_write_5m_tokens": w5m,
        "cache_read_tokens": read,
        "read_to_write_ratio": round(read / max(w1h + w5m, 1), 1),
        "total_cost_usd": round(sum(r["cost"] for r in ds.requests), 2),
        "ttl_detection": dict(ttl_modes),
    }


class Context:
    """Everything a rule may look at. Rules must not mutate it."""

    def __init__(self, ds, pricing: dict):
        self.ds = ds
        self.pricing = pricing
        self.drops = cache_drops(ds, pricing)
        self.sessions = session_rows(ds, pricing)
        self.daily = daily(ds, pricing)
        self.totals = totals(ds, self.drops, pricing)

    def drops_by_cause(self, cause: str) -> list[dict]:
        return [d for d in self.drops if d["cause"] == cause]

    @property
    def total_cost(self) -> float:
        return self.totals["total_cost_usd"]

"""Long-lived cache entries that never got used enough to pay for themselves."""

from __future__ import annotations
from .base import Rule, Finding, Column
from ..pricing import rates_for
from ..signals import MIN_MATERIAL_REWRITE

COLUMNS = [
    Column("premium_paid_usd", "Wasted",
           "The extra paid for a long-lived cache entry that was never reused enough "
           "to earn it back, versus not caching at all.", numeric=True),
    Column("session", "Session",
           "Claude's own title for the chat, or its opening prompt, with the working "
           "directory, session id and time.", wide=True),
    Column("explain", "What happened and what to do", "Why this write did not pay off.",
           wide=True),
    Column("tokens", "Cached", "Tokens written into the cache.", numeric=True),
    Column("subsequent_reads", "Times reused",
           "How many later turns read this cached context back. Two is break-even.",
           numeric=True),
]


class UnamortizedWritesRule(Rule):
    id = "unamortized_1h_writes"
    title = "Long-lived cache entries that never paid for themselves"
    modes = ("cache",)

    def evaluate(self, ctx) -> Finding | None:
        events = []
        for (sid, sidechain), rows in ctx.ds.lanes().items():
            for i, r in enumerate(rows):
                if r["write_1h"] < MIN_MATERIAL_REWRITE:
                    continue
                later = sum(1 for x in rows[i + 1:] if x["read"] > 0)
                if later >= 2:
                    continue
                rt = rates_for(r["model"], ctx.pricing)
                premium = r["write_1h"] / 1e6 * (rt["cache_write_1h"] - rt["input"])
                events.append({
                    "session": sid[:8], "session_id": sid,
                    "title": ctx.ds.label(sid),
                    "path": ctx.ds.path(sid, r["project"]),
                    "at": r["ts"].isoformat(), "model": r["model"],
                    "tokens": r["write_1h"], "subsequent_reads": later,
                    "premium_paid_usd": round(premium, 4),
                    "explain": (
                        f"{r['write_1h']:,} tokens were cached for the long window, which "
                        f"costs twice the normal input price. That only pays off once the "
                        f"same context has been read back at least twice, and this one was "
                        f"read back {later} time(s) before the session ended. The premium "
                        f"of ${premium:.2f} bought nothing."),
                })
        if not events:
            return None
        events.sort(key=lambda e: -e["premium_paid_usd"])
        total = sum(e["premium_paid_usd"] for e in events)

        detail = (
            "Caching is not free to set up. Writing a context into the cache costs more "
            "than sending it normally — twice as much for the long-lived variant Claude "
            "Code uses on subscription plans — and you get that back only through reuse, "
            "at a tenth of normal price per read. Two reads is where the long-lived "
            "option breaks even. "
            f"{len(events)} write(s) here never reached that, wasting ${total:.2f}. "
            "This is the signature of many short sessions: each one pays to set up a "
            "cache and then ends before using it. Grouping related work into one "
            "session, rather than starting fresh for each small task, is what fixes it. "
            "The amounts are individually small and add up.")

        return Finding(
            id=self.id, title=self.title, usd=total, detail=detail,
            confidence="measured", severity="waste", count=len(events),
            events=events[:25], columns=COLUMNS,
            stats={"writes_examined": len(events),
                   "tokens": sum(e["tokens"] for e in events)})


rule = UnamortizedWritesRule()

"""Cache died mid-work, with no gap, no model change and no compaction."""

from __future__ import annotations
from .base import Rule, Finding, Column

COLUMNS = [
    Column("avoidable_usd", "Wasted",
           "Cost of the rebuild above what reading the context back would have cost.",
           numeric=True),
    Column("session", "Session",
           "Claude's own title for the chat, or its opening prompt, with the working "
           "directory, session id, time and model.", wide=True),
    Column("explain", "What happened and what to do",
           "What can and cannot be concluded about this event, and what to try.",
           wide=True),
    Column("idle_minutes", "Idle",
           "Minutes since the previous request — near zero here, which is what makes "
           "these unexplained.", numeric=True),
    Column("prefix_lost_tokens", "Context lost",
           "Size of the cached context thrown away, in tokens.", numeric=True),
    Column("tokens_rewritten", "Rebuilt",
           "Tokens re-sent and re-cached as a result.", numeric=True),
]


class InvalidationRule(Rule):
    id = "invalidation_no_gap"
    title = "Cache discarded while you were still working"
    modes = ("cache",)

    def evaluate(self, ctx) -> Finding | None:
        events = ctx.drops_by_cause("invalidation_no_gap")
        if not events:
            return None
        total = sum(e["avoidable_usd"] for e in events)

        detail = (
            "These rebuilds have no innocent explanation. The conversation was seconds "
            "old, the model had not changed, and nothing was compacted — yet the cached "
            "context was thrown away anyway. Something altered the fixed part of the "
            "conversation, which invalidates everything cached after it. The usual "
            "culprits are a tool list changing (an MCP server connecting part-way "
            "through), an edit to instructions the session already loaded such as "
            "CLAUDE.md, a settings or plugin change, or resuming a session. "
            f"That cost ${total:.2f} across {len(events)} event(s). "
            "The logs do not record configuration changes, so this is the one finding "
            "here that is inferred rather than proven — treat the causes as candidates. "
            "The practical habit is to make configuration and instruction changes "
            "between sessions rather than in the middle of one.")

        rows = []
        for e in events:
            rows.append({**e, "explain": (
                f"After only {e['idle_minutes']:.1f} minutes, with the same model and no "
                f"compaction, the {e['prefix_lost_tokens']:,}-token context was discarded "
                f"and {e['tokens_rewritten']:,} tokens re-sent, costing "
                f"${e['avoidable_usd']:.2f}. Something changed the fixed part of the "
                f"conversation. If you connected an MCP server, edited CLAUDE.md, changed "
                f"settings, or resumed this session around "
                f"{e['at'][11:16]}, that is the likely trigger.")})
        return Finding(
            id=self.id, title=self.title, usd=total, detail=detail,
            confidence="measured", severity="waste", count=len(events),
            events=rows, columns=COLUMNS,
            stats={"tokens_rewritten": sum(e["tokens_rewritten"] for e in events),
                   "cause_confidence": "cost is measured; the trigger is inferred"})


rule = InvalidationRule()

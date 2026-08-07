"""Large sessions carried to the end without ever being summarised."""

from __future__ import annotations
from .base import Rule, Finding, Column

BIG_CONTEXT = 150_000
MIN_TURNS = 10

COLUMNS = [
    Column("cost_usd", "Cost", "Total spend for this session.", numeric=True),
    Column("session", "Session",
           "Claude's own title for the chat, or its opening prompt, with the working "
           "directory, session id and start time.", wide=True),
    Column("explain", "What to look at",
           "Why this session was expensive and what would reduce it.", wide=True),
    Column("turns", "Turns", "Billable requests in this session.", numeric=True),
    Column("span_minutes", "Span",
           "Minutes from first request to last. Long is not itself bad: an actively "
           "used session never loses its cache.", numeric=True),
    Column("peak_context_tokens", "Peak context",
           "Largest cached context carried, in tokens. Every later turn pays to read "
           "this back.", numeric=True),
    Column("gaps_over_ttl", "Long gaps",
           "Times the session sat idle longer than the cache window, each forcing a "
           "full rebuild.", numeric=True),
    Column("compactions", "Compactions",
           "Times the history was summarised to shrink it. Zero on a large session "
           "means the full context was re-read on every single turn.", numeric=True),
]


def explain(s: dict) -> str:
    bits = []
    if s["compactions"] == 0 and s["peak_context_tokens"] > BIG_CONTEXT:
        bits.append(
            f"Grew to {s['peak_context_tokens']:,} tokens across {s['turns']} turns and "
            f"was never summarised, so every turn paid to read the whole thing back. "
            f"Summarising at a natural break — once a task is finished, while the "
            f"session is still warm — shrinks what each later turn carries.")
    if s["gaps_over_ttl"]:
        bits.append(
            f"It also sat idle past the cache window {s['gaps_over_ttl']} time(s), the "
            f"longest being {s['longest_gap_minutes']:.0f} minutes, and each of those "
            f"forced a full rebuild.")
    if len(s["models"]) > 1:
        bits.append(
            f"It ran {len(s['models'])} different models; every switch discards the "
            f"cached context.")
    if not bits:
        bits.append("Nothing structurally wrong — this session is simply large and busy.")
    return " ".join(bits)


class UncompactedSessionsRule(Rule):
    id = "uncompacted_sessions"
    title = "Large sessions that were never summarised"
    modes = ("sessions",)

    def evaluate(self, ctx) -> Finding | None:
        flagged = [s for s in ctx.sessions
                   if s["peak_context_tokens"] > BIG_CONTEXT
                   and s["compactions"] == 0 and s["turns"] >= MIN_TURNS]
        if not flagged:
            return None
        cost = sum(s["cost_usd"] for s in flagged)
        share = round(cost / max(ctx.total_cost, 1e-9) * 100, 1)

        detail = (
            "Once a conversation is cached, continuing it is cheap per turn — but not "
            "free. Every turn still pays to read the whole accumulated context back, so "
            "a very large conversation carries a standing charge that grows as it grows. "
            "Summarising the history collapses that, and Claude Code will do it "
            "automatically when the context nears its limit. "
            f"{len(flagged)} session(s) here passed {BIG_CONTEXT:,} tokens across "
            f"{MIN_TURNS}+ turns without ever being summarised, together accounting for "
            f"${cost:.2f}, or {share}% of total spend. "
            "That figure is what those sessions cost, not what was wasted — the work was "
            "real. Treat it as where your exposure is concentrated. "
            "Timing matters if you do summarise: do it while the session is active and "
            "the context is still cached, because summarising a session that has already "
            "gone cold means paying full price to re-read the history first. Simply "
            "starting a fresh session costs nothing at all.")

        rows = [{**s, "explain": explain(s)} for s in flagged]
        return Finding(
            id=self.id, title=self.title, usd=0.0, detail=detail,
            confidence="measured", severity="info", count=len(flagged),
            events=rows, columns=COLUMNS,
            stats={"cost_usd": round(cost, 2), "pct_of_total_spend": share,
                   "context_threshold_tokens": BIG_CONTEXT})


rule = UncompactedSessionsRule()

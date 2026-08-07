"""Cache expired because too much time passed between requests."""

from __future__ import annotations
from .base import Rule, Finding, Column

COLUMNS = [
    Column("avoidable_usd", "Wasted",
           "What this single event cost above what it should have: the difference "
           "between rebuilding the cached context and simply reading it back.",
           numeric=True),
    Column("who", "Who was idle",
           "Whether the wait was yours or the machine's. The fix differs."),
    Column("session", "Session",
           "Claude's own title for the chat, or its opening prompt, with the "
           "working directory, session id, time and model.", wide=True),
    Column("explain", "What happened and what to do",
           "Plain-English account of this event and the change that would have "
           "avoided it.", wide=True),
    Column("idle_minutes", "Idle", "Minutes since the previous request.", numeric=True),
    Column("prefix_lost_tokens", "Context lost",
           "Size of the cached context thrown away, in tokens.", numeric=True),
    Column("tokens_rewritten", "Rebuilt",
           "Tokens re-sent and re-cached, billed at the write rate rather than the "
           "far cheaper read rate.", numeric=True),
]

WHO = {"human_away": "You were away",
       "no_human_input": "Machine was busy",
       "combined": "Both"}


def explain(e: dict) -> str:
    lost, saved, ttl = e["prefix_lost_tokens"], e["avoidable_usd"], e["ttl_minutes"]
    if e.get("subcause") == "human_away":
        return (f"You were away {e['human_idle_minutes']:.0f} minutes, longer than the "
                f"{ttl}-minute window a cached context survives for. The "
                f"{lost:,}-token context had to be rebuilt from scratch instead of read "
                f"back cheaply. Clearing the session before a break that long, and "
                f"starting a fresh one afterwards, would have avoided ${saved:.2f}.")
    if e.get("subcause") == "no_human_input":
        return (f"You sent no prompt during this gap: {e['machine_busy_minutes']:.0f} "
                f"minutes of machine time outran the {ttl}-minute cache window, so the "
                f"{lost:,}-token context was rebuilt mid-run. That is either a long tool "
                f"loop, a session left waiting on a permission approval, or the machine "
                f"sleeping — the logs cannot tell which. Clearing the session would not "
                f"have helped here; answering approvals promptly and breaking very long "
                f"unattended runs into stages would. Cost: ${saved:.2f}.")
    return (f"A combined {e['idle_minutes']:.0f}-minute gap exceeded the {ttl}-minute "
            f"cache window and the {lost:,}-token context was rebuilt, costing "
            f"${saved:.2f}.")


class TTLExpiryRule(Rule):
    id = "ttl_expiry"
    title = "Cached context expired between requests"
    modes = ("cache",)

    def evaluate(self, ctx) -> Finding | None:
        events = ctx.drops_by_cause("ttl_expiry")
        if not events:
            return None
        total = sum(e["avoidable_usd"] for e in events)
        away = [e for e in events if e.get("subcause") == "human_away"]
        machine = [e for e in events if e.get("subcause") == "no_human_input"]

        detail = (
            "Claude Code keeps the conversation cached so each new turn re-reads it at a "
            "tenth of the normal input price instead of paying to send it again. That "
            "cache lasts a fixed window, and every request refreshes it for free — so a "
            "session you keep working in never expires, however long it runs. Go quiet "
            "for longer than the window and the whole context has to be rebuilt at the "
            f"write price. That happened {len(events)} time(s), costing ${total:.2f}.")
        if away and machine:
            detail += (f" These split into {len(away)} where you were away and "
                       f"{len(machine)} where no prompt of yours was involved at all, "
                       f"which need different fixes — see each row.")
        elif machine:
            detail += (" In every case here no prompt of yours fell in the gap, so this "
                       "is not about taking shorter breaks: the time went on machine "
                       "work or on a session left waiting for you to approve something.")
        elif away:
            detail += (" In every case here you had stepped away. Clearing the session "
                       "before a long break costs nothing; resuming into it does.")

        rows = []
        for e in events:
            rows.append({**e,
                         "who": WHO.get(e.get("subcause"), "—"),
                         "explain": explain(e)})
        return Finding(
            id=self.id, title=self.title, usd=total, detail=detail,
            confidence="measured", severity="waste", count=len(events),
            events=rows, columns=COLUMNS,
            stats={"human_away_events": len(away),
                   "no_human_input_events": len(machine),
                   "tokens_rewritten": sum(e["tokens_rewritten"] for e in events)})


rule = TTLExpiryRule()

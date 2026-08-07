"""Cache thrown away because the model changed mid-session."""

from __future__ import annotations
from .base import Rule, Finding, Column

COLUMNS = [
    Column("avoidable_usd", "Wasted",
           "Cost of rebuilding the context after the switch, above what reading it "
           "back would have cost.", numeric=True),
    Column("switch", "Switch", "Which model handed over to which."),
    Column("session", "Session",
           "Claude's own title for the chat, or its opening prompt, with the working "
           "directory, session id and time.", wide=True),
    Column("explain", "What happened and what to do",
           "Plain-English account of this event and how to avoid it.", wide=True),
    Column("idle_minutes", "Idle",
           "Minutes since the previous request. Usually small here — the switch, not "
           "the wait, is what destroyed the cache.", numeric=True),
    Column("tokens_rewritten", "Rebuilt",
           "Tokens re-sent and re-cached because of the switch.", numeric=True),
]


class ModelSwitchRule(Rule):
    id = "model_switch"
    title = "Changing model mid-session discarded the cache"
    modes = ("cache", "models")

    def evaluate(self, ctx) -> Finding | None:
        events = ctx.drops_by_cause("model_switch")
        if not events:
            return None
        total = sum(e["avoidable_usd"] for e in events)

        detail = (
            "A cached conversation belongs to the model that cached it. Switch models "
            "part-way through and the new one starts cold: the entire context is sent "
            "again and re-cached at the write price. The bigger the conversation when "
            f"you switch, the more that costs. This happened {len(events)} time(s), "
            f"for ${total:.2f}. Deciding which model to use before a long conversation "
            "builds up avoids it. This is also the hidden cost in any setup that swaps "
            "models automatically — worth weighing against what the cheaper model saves.")

        rows = []
        for e in events:
            rows.append({**e,
                         "switch": f"{e.get('model_changed_from')} → {e['model']}",
                         "explain": (
                             f"Switching from {e.get('model_changed_from')} to "
                             f"{e['model']} after only {e['idle_minutes']:.1f} minutes "
                             f"idle meant the {e['prefix_lost_tokens']:,}-token context "
                             f"could not be reused, forcing a "
                             f"{e['tokens_rewritten']:,}-token rebuild. Picking the "
                             f"model before the conversation grew would have saved "
                             f"${e['avoidable_usd']:.2f}.")})
        return Finding(
            id=self.id, title=self.title, usd=total, detail=detail,
            confidence="measured", severity="waste", count=len(events),
            events=rows, columns=COLUMNS,
            stats={"tokens_rewritten": sum(e["tokens_rewritten"] for e in events)})


rule = ModelSwitchRule()

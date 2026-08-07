"""Whether the expensive model is being used where it earns its premium."""

from __future__ import annotations
from collections import defaultdict

from .base import Rule, Finding, Column
from ..pricing import rates_for

CHEAPER_MODEL = "claude-sonnet-4-6"
CHEAPER_LABEL = "Sonnet 4.6"
PREMIUM_FAMILY = "opus"
PREMIUM_LABEL = "Opus"
# Below this share of requests, plan mode is not really in use.
PLAN_MODE_THRESHOLD_PCT = 5.0

COLUMNS = [
    Column("phase", "Phase",
           "Planning turns versus execution turns, taken from the permission mode in "
           "effect at the time."),
    Column("model", "Model", "Which model handled them."),
    Column("requests", "Requests", "Billable requests in this phase on this model.",
           numeric=True),
    Column("output_tokens", "Output tokens",
           "Tokens the model generated. Execution is output-heavy, and output is the "
           "most expensive thing you buy.", numeric=True),
    Column("cost_usd", "Cost", "Total spend for this phase and model.", numeric=True),
]


class ModelSelectionRule(Rule):
    id = "model_selection"
    title = "Premium model used for routine execution"
    modes = ("models",)

    def evaluate(self, ctx) -> Finding | None:
        cheaper = rates_for(CHEAPER_MODEL, ctx.pricing)
        by_phase: dict = defaultdict(lambda: defaultdict(
            lambda: {"requests": 0, "cost_usd": 0.0, "output_tokens": 0}))

        premium_cost = alt_cost = 0.0
        premium_requests = plan_requests = 0

        for r in ctx.ds.requests:
            phase = "planning" if r["mode"] == "plan" else "execution"
            slot = by_phase[phase][r["model"]]
            slot["requests"] += 1
            slot["cost_usd"] += r["cost"]
            slot["output_tokens"] += r["output"]
            if r["mode"] == "plan":
                plan_requests += 1
                continue
            if PREMIUM_FAMILY not in r["model"].lower():
                continue
            premium_requests += 1
            premium_cost += r["cost"]
            alt_cost += (
                r["input"] / 1e6 * cheaper["input"]
                + r["output"] / 1e6 * cheaper["output"]
                + r["write_1h"] / 1e6 * cheaper["cache_write_1h"]
                + r["write_5m"] / 1e6 * cheaper["cache_write_5m"]
                + r["read"] / 1e6 * cheaper["cache_read"])

        if not premium_requests or premium_cost <= alt_cost:
            return None

        switch_tax = sum(d["avoidable_usd"] for d in ctx.drops
                         if d["cause"] == "model_switch")
        net = premium_cost - alt_cost - switch_tax
        if net <= 0:
            return None

        total_requests = len(ctx.ds.requests)
        plan_share = round(plan_requests / max(total_requests, 1) * 100, 1)
        plan_in_use = plan_share >= PLAN_MODE_THRESHOLD_PCT
        ratio = f"{premium_cost / alt_cost:.1f}x" if alt_cost else "several times"

        shared = (
            f"{PREMIUM_LABEL} is priced the same whatever you use it for — there is no "
            f"cheaper rate for thinking than for typing. What differs is volume. Writing "
            f"and editing code consumes far more tokens than deciding what to write, and "
            f"it is also the part where the gap between the premium model and a cheaper "
            f"one is narrowest. So the money is in matching the model to the phase. "
            f"You ran {premium_requests} {PREMIUM_LABEL} turns outside planning, costing "
            f"${premium_cost:.2f}. The same tokens on {CHEAPER_LABEL} would have cost "
            f"${alt_cost:.2f}, about {ratio} less, leaving ${net:.2f} after the "
            f"${switch_tax:.2f} you already spent rebuilding caches when switching "
            f"models. ")

        if plan_in_use:
            detail = shared + (
                f"You already use plan mode for {plan_share}% of requests, so the split "
                f"fits how you work: Claude Code's opusplan setting keeps the premium "
                f"model while you plan and hands execution to the cheaper one "
                f"automatically.")
        else:
            detail = shared + (
                f"Claude Code offers an opusplan setting that automates this, keeping the "
                f"premium model for planning and switching to the cheaper one for "
                f"execution. It will not help you yet: only {plan_share}% of your "
                f"requests run in plan mode, so there is no planning phase for it to "
                f"protect, and the figure above covers essentially all your "
                f"{PREMIUM_LABEL} usage. For you the question is therefore the broader "
                f"one — is premium-level reasoning worth its cost across this whole "
                f"workload? Two ways in: try the cheaper model on routine implementation, "
                f"or start using plan mode so the automatic split has something to split.")

        detail += (
            f" Treat the figure as a ceiling rather than a promise. It assumes "
            f"{CHEAPER_LABEL} would finish the same work in the same number of tokens; if "
            f"it needs more attempts, the saving shrinks. That is why this is reported as "
            f"an estimate while the cache findings are measured.")

        rows = []
        for phase in ("planning", "execution"):
            for model, s in sorted(by_phase.get(phase, {}).items(),
                                   key=lambda kv: -kv[1]["cost_usd"]):
                rows.append({"phase": phase, "model": model,
                             "requests": s["requests"],
                             "output_tokens": s["output_tokens"],
                             "cost_usd": round(s["cost_usd"], 2)})

        return Finding(
            id=self.id, title=self.title, usd=net, detail=detail,
            confidence="estimated", severity="opportunity",
            count=premium_requests, events=rows, columns=COLUMNS,
            stats={"premium_requests": premium_requests,
                   "premium_cost_usd": round(premium_cost, 2),
                   "same_work_cheaper_usd": round(alt_cost, 2),
                   "switch_tax_usd": round(switch_tax, 2),
                   "plan_mode_share_pct": plan_share,
                   "opusplan_applicable": plan_in_use})


rule = ModelSelectionRule()

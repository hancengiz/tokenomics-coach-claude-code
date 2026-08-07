# Model Strategy

Read when handling `--mode models`.

## The planning/execution split

Claude Code exposes `permissionMode` in the logs: `plan` marks plan-mode turns, `default` and `auto`
mark execution. The analyzer attributes every request's cost to the mode in effect at the time, so
"what did planning cost vs execution" is answerable directly rather than inferred.

Official guidance:

- **`opusplan`** is a model alias that runs **Opus in plan mode** and automatically switches to
  **Sonnet for execution** — code generation, edits, file writes, commands.
- **Token consumption is highest during execution.** File generation, tool loops, and iteration
  dominate volume, and that is where the capability gap between Opus and Sonnet is narrowest.
- Sonnet handles the large majority of coding work at a fraction of Opus cost (Opus $5/$25 vs
  Sonnet 4.6 $3/$15 per MTok — and the gap widens on output-heavy execution).
- Haiku suits simple subagent tasks and lookups.

## Check plan mode is actually used first

`opusplan_opportunity.plan_mode_share_pct` reports what fraction of requests ran in plan mode, and
`opusplan_applicable` is false below 5%.

**Opus does not cost less in plan mode.** The rate is identical — $5/$25 either way. The entire
argument rests on *volume*: execution burns far more tokens than planning, and Opus's advantage is
narrowest there. So you pay the premium rate on the small phase and the cheap rate on the large one.

If plan mode is near zero, "adopt opusplan" is the wrong recommendation and you must say so. The
saving estimate covers *all* Opus turns, so it is really "what if Opus work ran on Sonnet" — a
quality decision, not a routing tweak. Adopting opusplan properly would mean the user *starts*
planning, which raises Opus usage in one phase while cutting it in the other. Do not present that as
a pure saving.

Also check *where* the Opus spend sits. Opus concentrated in `auto` permission mode means unattended
execution — the strongest case for the switch. Opus in `default` alongside heavy human interaction is
weaker, since that is where reasoning quality is most visible.

## The catch

Each plan↔execute toggle is a **model switch**, and a model switch invalidates the cached prefix.
`models.model_switches` computes what those re-caches actually cost in the user's data.

So `opusplan` is not free. Quantify both sides before recommending it:

- Savings: `by_permission_mode` execution-mode Opus spend, times the Opus→Sonnet rate delta.
- Cost: `model_switches.recache_cost_usd`, scaled for how often the user would toggle.

If the user toggles frequently on a large context, the re-cache tax can exceed the rate savings. Say
so rather than recommending `opusplan` reflexively.

## Reading `by_permission_mode`

The signal to look for: **Opus dominating `default`/`auto` requests.** That is paying premium rates
for the phase where the premium buys least.

Also watch for the opposite: if `plan` mode barely appears at all, the user is not planning before
executing. That is not directly a cache problem, but unplanned execution tends to produce more
iteration, and iteration is where token volume accumulates. Mention it only if the data supports it —
a near-zero plan-mode count alongside high execution spend.

## Subagents

The analyzer marks `isSidechain` requests and applies the 5m TTL to them.

- A subagent runs its own conversation with its own system prompt, tools, and cache — **separate from
  the parent**. Its first turn has no cache hits.
- Subagent caches are **always 5-minute**, even on subscription auth. An idle subagent re-caches after
  five minutes, not sixty.
- The parent's prefix is *not* invalidated; the call and its result simply append.
- Delegation saves tokens only when the summary returned is much smaller than the output consumed —
  test runs, log processing, broad searches. It costs tokens when the subagent's context duplicates
  work the parent already has.
- Community benchmarks put subagent-heavy workflows at several times the tokens of a single-threaded
  session. Treat that as directional, not as a documented figure.

⚠️ Recursive subagent spawning has been reported to consume millions of tokens in minutes with no
documented cycle detection. If the data shows deep sidechain nesting, flag it prominently.

## Rate reference

| Model | Input | Output |
|---|---|---|
| Opus 5 / 4.8 | $5 | $25 |
| Sonnet 5 (thru 2026-08-31) | $2 | $10 |
| Sonnet 5 (from 2026-09-01) | $3 | $15 |
| Sonnet 4.6 | $3 | $15 |
| Haiku 4.5 | $1 | $5 |

Output rates matter most here: execution is output-heavy, and Opus output is 2.5x Sonnet 4.6 and
5x Haiku. When `by_model` shows high `output_tokens` on Opus during execution, that is the finding.

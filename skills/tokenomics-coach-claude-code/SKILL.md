---
name: tokenomics-coach-claude-code
description: "Audit Claude Code session logs for avoidable token spend — prompt-cache expiry, un-amortized 1h cache writes, uncompacted long sessions, and model choice across planning vs execution. Use when the user asks about token cost, cache efficiency, wasted spend, why a session was expensive, whether to /clear or /compact, or which model to run for planning vs implementation."
license: MIT
compatibility: Requires Python 3.9+ and Claude Code session logs in ~/.claude/projects
allowed-tools: Read Write Bash(python3:*) Bash(ls:*) Bash(find:*) Bash(cat:*) Task AskUserQuestion
metadata:
  version: "1.0.0"
  author: cengizhan
---

# Tokenomics Coach

Find money the user is burning on the **prompt cache**, not on prompt wording. Every claim you
make must come from `scripts/analyze.py` output — never estimate, never read raw `.jsonl`.

**Give hard verdicts with dollar figures.** Name the session, state the avoidable cost, state what
should have happened instead. "Session 0ac0d9f6 burned $2.70 resuming after a 76-minute break —
that should have been `/clear` and a fresh session." Do not hedge into generic advice.

## Step 0 — Locate the script

`scripts/analyze.py` ships with this skill, not in the user's project. Resolve it once:

```bash
SKILL_MD="$(find -L ~/.claude/skills ~/.claude/plugins -maxdepth 6 -type f \
              -path '*/tokenomics-coach-claude-code/SKILL.md' 2>/dev/null | head -1)"
SKILL_DIR="${SKILL_MD%/SKILL.md}"
```

Use `python3 "$SKILL_DIR/scripts/analyze.py"` everywhere below. Never `cd` into the skill directory.

## Routing

| User intent | Flags | Then read |
|---|---|---|
| Cache waste, expiry, "why so expensive" | `--mode cache --days 30` | `references/cache-economics.md` |
| Session hygiene, `/clear` vs `/compact`, long threads | `--mode sessions --days 30` | `references/session-hygiene.md` |
| Model mix, planning vs execution, Opus share | `--mode models --days 30` | `references/model-strategy.md` |
| Full audit / "how bad is it" | `--mode all --days 30` | all three |

Add `--project ~/code/my-app` to scope. Always pass `--pretty`. Rates live in `scripts/pricing.json`.

```bash
python3 "$SKILL_DIR/scripts/analyze.py" --mode all --days 30 --pretty
```

## The HTML report is automatic

Every run writes a self-contained HTML report to `~/tokenomics-report-<date>.html` and returns the
path as `html_report` in the JSON. **You do not generate this** — the script renders it
deterministically, so it costs no tokens and is byte-identical run to run. Never hand-write HTML for
this skill.

- Override the location with `--html <path>`; suppress with `--no-html`.
- The report always contains every section, even when `--mode` asked for one.
- Always tell the user the path in your reply. Do not paste the report's contents back to them.

It contains: headline cards, a dollar-ranked action list, a daily stacked-bar spend chart
(reads/writes/output), cache drops by cause, worst individual events, most expensive sessions, and
the planning-vs-execution split with the `opusplan` estimate.

## Cache drops — the core detector

`cache.cache_drops` finds every turn that **rebuilt a prefix which was warm on the previous turn**,
then attributes a cause. Idle time is only one of them — the cache can collapse with no gap at all.
Report the three avoidable buckets in order of dollars; `compaction` is expected and not waste.

**`ttl_expiry`** — the gap between consecutive requests exceeded the TTL, so the prefix was re-written
at the write rate instead of read at 0.1x. **Always check `subcause` before prescribing** — the gap is
split into `human_idle_minutes` (previous response → next human prompt) and `machine_busy_minutes`:

- `human_away` — the person left. → `/clear` before stepping away, then start fresh. Resuming a 300k
  context after lunch is the single most expensive keystroke available.
- `no_human_input` — no human prompt in the gap at all. A long tool loop, a session parked on a
  **permission approval**, or a sleeping machine. → `/clear` does *not* help. Approve promptly and
  stage very long unattended runs. Do not tell the user to take shorter breaks; they weren't on one.
- `combined` — both contributed.

**`model_switch`** — the model changed mid-session, invalidating the prefix.
→ Verdict: choose the model *before* the expensive context accumulates. Weigh this against the
`opusplan` recommendation (see `references/model-strategy.md`) — toggling has a price.

**`invalidation_no_gap`** — the cache collapsed **inside** the TTL window, same model, no compaction.
Something changed the tools or system layer. Usual suspects: editing `CLAUDE.md`, an MCP server
connecting mid-session, a tool-definition change, or a session resume.
→ Verdict: these are the invisible ones. Say plainly that the cause is *not* provable from the logs,
list the likely triggers, and suggest making `CLAUDE.md` and MCP config changes between sessions
rather than during them.

**`compaction`** — a legitimate drop. Report it for completeness; never bill it as waste.

Plus one non-drop pattern:

**Un-amortized 1h writes** (`cache.unamortized_1h_writes`) — a 1h write costs **2x** base input and
needs **≥2 subsequent reads** to beat not caching. Writes that never got them paid a premium for
nothing. Usually a symptom of many short-lived sessions.

And from `--mode sessions`:

**Never-compacted large sessions** — a 300k-token context re-read every turn at 0.1x is a per-turn
tax that grows with the thread.
→ Verdict: `/compact` at task boundaries **while the cache is still warm**. Compacting cold
reprocesses the full history as uncached input. `/clear` is free; `/compact` is not.

## TTL detection

Claude Code does **not** log the auth type, so never claim to know whether the user is on a
subscription or an API key. You do not need to: `totals.ttl_detection` reports the TTL derived from
which ephemeral bucket each session actually wrote into (`observed_1h`, `observed_5m`, or an
`assumed_*` fallback when nothing was written). The analyzer applies each lane's own observed TTL.

A large `observed_5m` count on main-thread sessions indicates API-key auth, where the 1h TTL is
opt-in via `ENABLE_PROMPT_CACHING_1H=1`. Mention that only as an inference, not a fact.

## Cache economics — the numbers that drive every verdict

Verified against `platform.claude.com` docs (2026-08-07). Do not restate these from memory; they are
also encoded in `scripts/pricing.json`.

| Operation | Multiplier vs base input |
|---|---|
| 5-minute cache write | 1.25x |
| 1-hour cache write | 2x |
| Cache read (hit) | 0.1x |

- **A cache read refreshes the TTL for free.** The clock runs from the last request touching the
  prefix, not from session start. Steady work keeps a cache alive indefinitely at no extra cost.
- **The TTL is measured from the START of the request**, not the end of its response. A long
  streaming turn eats its own window.
- **Break-even:** 1h write pays off after 2 reads; 5m write after 1.
- **Claude Code uses the 1h TTL on subscription auth, 5m on API-key auth.** Subagents are *always*
  5m, even on subscription — so an idle subagent re-caches after 5 minutes, not 60.

## Planning vs execution

`--mode models` splits spend by `permissionMode` (`plan` = planning turns, `default`/`auto` =
execution) and by model. Use it to answer "am I paying Opus rates to write files?"

Official guidance: the **`opusplan`** alias runs Opus for plan mode and switches to Sonnet for
execution. Token consumption is highest during execution — file writes, tool loops, iteration — which
is exactly where Opus is least differentiated. If `models.by_permission_mode` shows Opus dominating
execution turns, that is the recommendation.

Note each plan↔execute toggle is a model switch and invalidates the cache, so `opusplan` trades a
per-toggle re-cache against a much cheaper execution phase. Quantify both sides from the data before
recommending it — if the user toggles constantly on a large context, the trade may not pay.

## Rules

1. **Script only.** Never `Read` a `.jsonl`. Never estimate a cost you did not compute.
2. **Dollars, then tokens.** Lead every finding with the avoidable USD figure.
3. **Name the session and project.** Verdicts are about specific sessions, not habits in general.
4. **Rank by cost.** A 1.2M-token event that cost $0.06 is not the headline.
5. **Progressive disclosure.** Read only the `references/*.md` for the active mode.
6. **Don't coach prompt wording.** Out of scope. Redirect to cost impact if it comes up.
7. **Cite pricing from `scripts/pricing.json`**, and warn if its `version` is more than ~90 days old.
8. **Privacy.** Project directory names and 8-char session prefixes only; never quote user content.

## Reporting shape

Open with the bottom line, then the ranked findings, then the report path. Keep the terminal reply
short — the detail lives in the HTML.

```
14 days · $69.73 total · $9.88 avoidable (14%)

1. $4.96  TTL expiry — 3 drops, 522k tokens re-written
          Worst: 0ac0d9f6 (visa-…-harness) idle 76min on a 293k prefix → $2.70
2. $3.92  Un-amortized 1h writes — 65 writes never reached 2 reads
3. $0.52  Model switch — 1 drop mid-session at a 1.1min gap
4. $0.48  Invalidation with no gap — 2 drops at <20s idle; likely CLAUDE.md/MCP change

Full report: ~/tokenomics-report-2026-08-07.html
```

Close with at most three concrete behaviour changes, each tied to a dollar figure.

## Troubleshooting

- `python3: command not found` → Python 3.9+ is required; there is no shell fallback.
- Empty `$SKILL_DIR` → non-standard install location; ask the user rather than guessing.
- `no billable requests` → the window is too short or `--project` matched the wrong directory.
  List candidates with `ls -1 ~/.claude/projects/`.
- Costs look wrong → check `scripts/pricing.json` `version`; published rates change.

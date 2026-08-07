---
name: tokenomics-coach-claude-code
description: "Audit Claude Code session logs for avoidable token spend — prompt-cache expiry, un-amortized cache writes, uncompacted long sessions, and model choice across planning vs execution. Use when the user asks about token cost, cache efficiency, wasted spend, why a session was expensive, whether to /clear or /compact, or which model to run for planning vs implementation."
license: MIT
compatibility: Requires Python 3.9+ and Claude Code session logs in ~/.claude/projects
allowed-tools: Read Write Bash(python3:*) Bash(ls:*) Bash(find:*) Bash(cat:*) AskUserQuestion
metadata:
  version: "2.0.0"
  author: cengizhan
---

# Tokenomics Coach

Find money the user is burning on the **prompt cache**, not on prompt wording. Every claim must come
from `scripts/analyze.py` — never estimate a figure yourself, never read a raw `.jsonl`.

**Give hard verdicts with dollar figures.** Name the session, state the cost, state what should have
happened instead. But make the verdict fit the evidence: the same re-cache means different things
depending on its `subcause`, and prescribing a shorter coffee break to someone whose agent ran
unattended for 76 minutes is simply wrong. Hard verdicts, not hasty ones.

## Step 0 — Locate the script

`scripts/analyze.py` ships with this skill, not in the user's project. Resolve it once:

```bash
SKILL_MD="$(find -L ~/.claude/skills ~/.claude/plugins -maxdepth 6 -type f \
              -path '*/tokenomics-coach-claude-code/SKILL.md' 2>/dev/null | head -1)"
SKILL_DIR="${SKILL_MD%/SKILL.md}"
```

Use `python3 "$SKILL_DIR/scripts/analyze.py"` everywhere below. Never `cd` into the skill directory.

## Running it

```bash
python3 "$SKILL_DIR/scripts/analyze.py" --days 30
```

**The default window is 30 days.** Change it with `--days N` when the user asks for a different
period ("this week" → `--days 7`, "yesterday" → `--days 2` and filter by date in your reading).
Scope to one project with `--project ~/code/my-app`.

| User intent | Flags | Then read |
|---|---|---|
| Cache waste, expiry, "why so expensive" | `--mode cache` | `references/cache-economics.md` |
| Long threads, `/clear` vs `/compact` | `--mode sessions` | `references/session-hygiene.md` |
| Model mix, planning vs execution | `--mode models` | `references/model-strategy.md` |
| Full audit / "how bad is it" | `--mode all` (default) | all three |

Without `--pretty` the script prints a short ranked summary plus both output paths — usually all you
need. Add `--pretty` to dump the full report JSON when you need the individual events.

## Output: JSON first, HTML rendered from it

Every run writes **two files** side by side:

- `~/tokenomics-report-<date>.json` — the report model, and the source of truth
- `~/tokenomics-report-<date>.html` — rendered from that JSON

**You do not write either.** The script renders deterministically, so it costs no tokens and never
drifts. Never hand-write HTML for this skill. Tell the user both paths; do not paste report contents
back to them.

### Adding your own notes

The JSON carries two note slots, both empty until you fill them, both preserved on re-render:

- top-level `"notes": []` — overall narrative
- per-finding `"notes": []` — commentary on one finding

Edit the JSON, then re-render without touching the logs again:

```bash
python3 "$SKILL_DIR/scripts/analyze.py" --render ~/tokenomics-report-<date>.json
```

Use this when the user gives context the logs cannot know — "that long run was a deliberate overnight
scan", "we're staying on Opus for this client". Notes render clearly marked as commentary.

## Reading the findings

`findings` is a list, already sorted by dollar value. Each has `id`, `title`, `usd`, `confidence`,
`detail`, `events`, and `columns`. The `detail` text is written for a first-time reader — you can
summarise it, but do not contradict it.

| Finding id | What it means |
|---|---|
| `ttl_expiry` | The gap between requests outran the cache lifetime. Check each event's `subcause`. |
| `model_switch` | The model changed mid-session, discarding the cached prefix. |
| `invalidation_no_gap` | Cache died with no gap, same model, no compaction. Cost measured; trigger inferred. |
| `unamortized_1h_writes` | Long-lived cache writes never reused enough to beat not caching. |
| `uncompacted_sessions` | Large sessions never summarised. `usd` is 0 — this is exposure, not waste. |
| `model_selection` | Premium model doing routine execution. **Estimated**, not measured. |

### `ttl_expiry` subcauses — the distinction that changes the advice

- `human_away` — the person left. → `/clear` before stepping away, then start fresh.
- `no_human_input` — no human prompt in the gap at all. A long tool loop, a session parked on an
  unanswered **permission approval**, or a sleeping machine; the logs cannot tell these apart.
  → `/clear` does *not* help. Do not tell the user to take shorter breaks; they weren't on one.
- `combined` — both contributed.

## Rules

1. **Script only.** Never `Read` a `.jsonl`. Never state a cost you did not compute.
2. **Dollars first.** Lead every finding with its figure, and rank by it.
3. **Never merge measured and estimated.** `summary.measured_waste_usd` is arithmetic;
   `summary.estimated_saving_usd` assumes a cheaper model would use the same tokens. Quote
   `total_opportunity_usd` only alongside both parts. "$28 avoidable" unqualified is wrong.
4. **Name the session.** Use its `title` (Claude's own chat title) and full `path`, not just the id.
5. **Say what is inferred.** `invalidation_no_gap` triggers and `model_selection` savings are not
   proven. State that plainly rather than implying certainty.
6. **Progressive disclosure.** Read only the `references/*.md` for the active mode.
7. **Don't coach prompt wording.** Out of scope. Redirect to cost impact if it comes up.
8. **Check `pricing.json` freshness** and warn if its `version` is more than ~90 days old.
9. **Privacy.** The report already limits itself to titles, paths and 8-char session ids. Never quote
   the user's code or prompt text beyond what the script emits.

## Reporting shape

Lead with the bottom line, then the ranked findings, then the paths. Keep it short — detail lives in
the HTML.

```
30 days · $109.86 spent
  $10.79 measured waste + $21.50 estimated savings = 29.4% of spend

1. $21.50  Premium model used for routine execution      [estimated]
2.  $4.96  Cached context expired between requests       [measured]
3.  $4.45  Long-lived cache entries that never paid off  [measured]

json: ~/tokenomics-report-2026-08-07.json
html: ~/tokenomics-report-2026-08-07.html
```

Close with at most three concrete changes, each tied to a figure.

## Extending

One rule per file in `scripts/tokenomics/rules/`. A module exposing `rule = MyRule()` is discovered
automatically; a rule that raises is skipped with a warning rather than breaking the report. If the
user wants a new check, add a file — do not edit existing rules.

## Troubleshooting

- `python3: command not found` → Python 3.9+ required; no shell fallback exists.
- Empty `$SKILL_DIR` → non-standard install; ask the user rather than guessing.
- `no .jsonl logs modified in the last N days` → widen `--days` or check `--project`. List
  candidates with `ls -1 ~/.claude/projects/`.
- `warning: rule 'x' failed` → that one check was skipped; the rest of the report is still valid.
- Costs look wrong → check `pricing.json` `version`; published rates change.

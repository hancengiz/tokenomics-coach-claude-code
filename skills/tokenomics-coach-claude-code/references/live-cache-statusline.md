# Live Cache Status Line

Read when the user wants to **see cache behaviour while they work**, rather than audit it
afterwards: "show cache hit rate in my status line", "warn me when my context gets reprocessed",
"add a cache countdown".

This is the one capability in this skill that reads the transcript directly instead of going
through `analyze.py`. The rule it must still respect: **never state a historical dollar figure
from here.** Live indicators describe the current session's cache state. Cost claims come from the
analyzer.

Your job is to teach the user's status line to compute these values — **not** to install a
particular script. There is no template here on purpose. Read what they already have, and add to
it in its own idiom.

## Step 1 — Find the existing status line

Check in precedence order, last match wins:

1. `~/.claude/settings.json`
2. `<project>/.claude/settings.json`
3. `<project>/.claude/settings.local.json`

Look at `statusLine.command`. Three cases, and they lead to different conversations:

| What you find | What to do |
|---|---|
| No `statusLine` key | Offer to add a small one. Keep it to what they asked for. |
| A script they own (`~/.claude/statusline.sh`, a repo file) | Read it. Add segments in the same language, colour vocabulary and layout. |
| A third-party package (`npx ccstatusline`, a `node_modules` path) | **Say plainly that editing the package is not durable** — it lives in a cache that reinstalls wipe. Offer to either wrap its output or replace it with a script they own. |

## Step 2 — Ask before changing anything

A status line is personal and already working. Use `AskUserQuestion`: extend what is there,
replace it, or leave it alone and print the numbers another way. Never silently rewrite it. If you
replace a third-party line, mimic its existing segments first — losing widgets they liked is worse
than not having the cache readout.

## Step 3 — The data you get

Claude Code pipes one JSON object to the command on **stdin**:

`transcript_path`, `model.display_name`, `model.id`, `workspace.current_dir`, `version`,
`output_style.name`, `cost.total_cost_usd`, `cost.total_duration_ms`, and on newer versions
`context_window`.

Everything about caching comes from reading the JSONL at `transcript_path`.

## Step 4 — The calculations

This section is the substance. Get these five things right and the rest is presentation.

### 1. De-duplicate by `requestId`

Each assistant turn is written to the transcript **several times as it streams**, every copy
carrying the same `usage` block. Sum them naively and every total silently multiplies.

Collapse consecutive entries sharing a `requestId` into one turn. This is the single most common
way a hand-rolled cache meter goes wrong, and it fails quietly — the numbers just look too big.

### 2. The fields on `message.usage`

| Field | Meaning |
|---|---|
| `cache_read_input_tokens` | Served from cache. Cheap (0.1x). |
| `cache_creation_input_tokens` | Written to cache this turn. 1.25x (5m) or 2x (1h). |
| `input_tokens` | Sent uncached at full price. |
| `cache_creation.ephemeral_1h_input_tokens` | Non-zero → this lane is on the 1h TTL. |
| `cache_creation.ephemeral_5m_input_tokens` | Non-zero → 5m TTL. |

### 3. Hit rate — say which definition you used

Two defensible formulas, and they give different numbers:

- **Read share** — `read / (read + creation + input)`. What fraction of the prefix was cheap.
- **Read vs write** — `read / (read + creation)`. Ignores uncached input. This is what
  `ccstatusline` reports.

Neither is wrong. Pick one, label it, and mention the other exists if the user is comparing
against another tool and sees a different figure.

### 4. Anchor the hit rate to the prompt, not the latest turn

**This is the part that is easy to get wrong and that makes the number useless if you do.**

The naive implementation reads the most recent assistant turn. During an agent run, every tool
call adds a turn, and those turns only append a small delta to an already-warm prefix — they sit
at 97-99% almost unconditionally. So the displayed figure climbs while the agent works and ends up
describing the tool loop instead of the user's prompt.

Anchor instead to the **first assistant turn after the last human prompt**. That turn pays for the
whole accumulated prefix, so it is the one that answers *"did my prompt hit cache?"* — and it
stays put for the rest of the run, which is what makes it readable.

A real example, same session, same moment:

| Anchor | Reported | What was true |
|---|---|---|
| Latest turn | 97.4% | the tool loop was warm |
| First response after the prompt | **0.0%** | `read=0, creation=83306` — full rebuild |

The latest-turn number hid an 83k-token re-cache completely.

**Identifying a human prompt.** Same rule as `cache-economics.md` uses for gap attribution:
`type: user` entries whose content is a string, or a block list containing `text` and no
`tool_result`. Entries carrying `tool_result` are the machine loop. Two further exclusions the
logs require:

- `isMeta: true` — slash-command echoes and caveat banners arrive as user entries but were never
  typed as prompts.
- `isSidechain: true` — subagent traffic.

**Bursts.** One user action can emit several consecutive `user` entries (command output, caveat,
then the real prompt). Anchor to the **last** prompt in the run, and when picking an earlier
sample for comparison, skip prompts that never got a response.

### 5. TTL countdown — a different anchor

The countdown must hang off the **most recent turn of any kind**, not the prompt-anchored one.
Cache reads refresh the entry for free, so the tool loop genuinely keeps the cache alive.

- Detect the lane from the `ephemeral_*` split rather than assuming. See `cache-economics.md` for
  which auth types default to which TTL, and note subagents are always 5m.
- The clock starts at **request start, not response end**.
- Only main-chain turns refresh the main conversation's entry; a subagent request does not.

**A countdown needs `refreshInterval`, or it will not tick.** By default the status line is
event-driven — it re-runs at turn boundaries, not on a clock — so a countdown freezes at whatever
it read when the user last sent a prompt. This surprises people; say it before they discover it.

Set the cadence alongside it in `settings.json`, and **match the interval to the unit displayed**:

```json
{ "statusLine": { "type": "command", "command": "...", "refreshInterval": 60 } }
```

The minimum is `1`, and it takes effect without a restart. Do not reach for it: the command re-runs
every N seconds in *every* open session, so a 1-second interval on a large transcript burns real
CPU forever in the background. Prefer 60 with a minutes display. If the user declines the setting
entirely, show an **absolute expiry time** (`expires 16:47`) rather than a relative countdown —
absolute stays true no matter how stale the render is.

### 6. The reprocessing warning

Compare the **last two prompt-anchored samples** — like with like. Adjacent turns will not do; a
prompt-anchored turn next to a tool-loop turn always looks like a cliff.

A drop of ~30 points or more means the prefix was rebuilt. Present it as *"your context was
rebuilt"*, and let the user judge whether that was worth it — the warning is not automatically a
finding of waste:

- **Expected, not a problem:** right after `/clear`, a compaction boundary, or a session resume.
- **Deliberate, with a price tag:** a model switch. The new model cannot read the old model's
  cache, so the whole prefix is rewritten. Worth surfacing precisely because the cost is invisible
  otherwise.
- **Worth investigating:** none of the above — a TTL expiry, or a silent invalidation such as an
  MCP server connecting mid-session or a `CLAUDE.md` edit.

Check for a compaction boundary between the two samples before crying waste.

If the user asks what a specific warning cost them, that is an analyzer question — run
`analyze.py` and read the `model_switch`, `ttl_expiry` or `invalidation_no_gap` finding rather
than computing it inline.

## Step 5 — Constraints on whatever you write

- **It runs on every render.** Read from the tail and bound the work; do not walk a large
  transcript once per metric.
- **Never block.** No network calls, no unbounded subprocesses.
- **Fail silent.** On any error print the rest of the line, or nothing — a status line that throws
  is worse than one missing a segment. Malformed JSONL lines are normal; skip them.
- **Degrade cleanly** when there is no transcript, no assistant turn yet, or no human prompt yet.

## What not to do

- Do not impose a layout, colour scheme, emoji set or language. Match the file already there.
- Do not add segments the user did not ask for.
- Do not quote historical costs from transcript arithmetic. That is what the analyzer is for.

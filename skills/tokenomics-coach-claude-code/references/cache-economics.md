# Cache Economics

Read when handling `--mode cache`. Every number here is from the official docs
(`platform.claude.com/docs/en/build-with-claude/prompt-caching`,
`platform.claude.com/docs/en/about-claude/pricing`), verified 2026-08-07.

## Rates

Multipliers against the model's base input price:

| Operation | Multiplier |
|---|---|
| 5-minute cache write | 1.25x |
| 1-hour cache write | 2x |
| Cache read | 0.1x |

Absolute rates per MTok (see `scripts/pricing.json` for the full table):

| Model | Input | 5m write | 1h write | Read | Output |
|---|---|---|---|---|---|
| Opus 5 / 4.8 | $5 | $6.25 | $10 | $0.50 | $25 |
| Sonnet 5 (intro, thru 2026-08-31) | $2 | $2.50 | $4 | $0.20 | $10 |
| Sonnet 5 (from 2026-09-01) | $3 | $3.75 | $6 | $0.30 | $15 |
| Sonnet 4.6 | $3 | $3.75 | $6 | $0.30 | $15 |
| Haiku 4.5 | $1 | $1.25 | $2 | $0.10 | $5 |

Sonnet 5 introductory pricing ends **2026-08-31**; rates rise 50% on 2026-09-01. Mention this if the
user's spend is Sonnet-5 heavy.

## TTL semantics

- **Reads refresh the TTL for free.** Docs: *"The cache is refreshed for no additional cost each time
  the cached content is used."* A continuously active session never re-caches, regardless of length.
- **The clock starts at request start, not response end.** A four-minute streaming response consumes
  four minutes of the window. This makes the 5m TTL genuinely fragile for long agentic turns.
- **Claude Code defaults:** 1h TTL on Pro/Max/Team/Enterprise subscription auth; 5m on API key,
  Bedrock, and other cloud providers. `ENABLE_PROMPT_CACHING_1H=1` opts up; `FORCE_PROMPT_CACHING_5M=1`
  forces down.
- **Subagents are always 5m**, even on subscription. There is no opt-up. The analyzer applies a 300s
  TTL to `isSidechain` requests for this reason.

## Break-even

From the pricing docs, verbatim: *"caching pays off after one cache read for the 5-minute duration
(1.25x write), or after two cache reads for the 1-hour duration (2x write)."*

Arithmetic:
- 5m: `1.25 + 0.1 = 1.35` vs `2.0` uncached → wins at 2 requests.
- 1h: `2.0 + 0.1 + 0.1 = 2.2` vs `3.0` uncached → wins at 3 requests.

This is what `unamortized_1h_writes` measures: 1h writes that never saw two subsequent reads paid the
2x premium and got nothing for it.

## What invalidates a cached prefix

Hierarchy is `tools` → `system` → `messages`. A change at one level invalidates it and everything below.

| Change | Invalidates |
|---|---|
| Tool definitions added/removed/reordered/edited | Everything |
| System prompt text (any byte) | System + messages |
| Web search / citations toggle, fast mode | System + messages |
| `tool_choice`, images added/removed | Messages |
| Thinking params, `effort` | Messages always; tools/system model-specific |
| Appending new messages at the end | Nothing prior — the normal growth path |

Practical consequences worth telling the user:
- Editing `CLAUDE.md` mid-session invalidates the system layer.
- Connecting an MCP server mid-session changes tool definitions and invalidates **everything**.
- Switching models invalidates the prefix.

## Minimum cacheable prefix

Silently ignored below the threshold — no error is returned.

| Model | Minimum |
|---|---|
| Opus 5, Fable 5, Mythos 5 | 512 tokens |
| Opus 4.8, Sonnet 5, Sonnet 4.6/4.5 | 1,024 |
| Opus 4.7 | 2,048 |
| Opus 4.6 / 4.5, Haiku 4.5 | 4,096 |

## Interpreting `--mode cache`

Findings arrive in `report["findings"]`, already ranked by dollars. The ones this mode produces are
`ttl_expiry`, `model_switch`, `invalidation_no_gap` and `unamortized_1h_writes`.

- `summary.measured_waste_usd` — the number to lead with. Arithmetic, not opinion.
- `totals.read_to_write_ratio` — headline health. High is good: prefixes are being reused rather
  than rebuilt. Below ~5 suggests fragmented sessions or frequent invalidation.
- `totals.ttl_detection` — per-lane cache lifetime derived from observed writes, never assumed.
- Each finding's `events` carry `avoidable_usd`, and an `explain` string already written for a
  first-time reader.

### Cause attribution

A finding fires on a **cache drop**: a turn writing a material prefix when the previous turn had an
established one (>=20k tokens) and this turn reads back less than half of it. `signals.cache_drops`
assigns the cause once, in priority order:

1. `compaction` — a compact boundary fell between the two turns. Legitimate; not reported as waste.
2. `ttl_expiry` — the gap exceeded that lane's observed TTL. Provable.
3. `model_switch` — the model changed. Provable.
4. `invalidation_no_gap` — none of the above: warm window, same model, no compaction.

Bucket 4 is an **inference about the trigger, though the cost is measured exactly**. The logs do not
record tool-definition or system-prompt changes, so the analyzer can only say the prefix died for a
reason it cannot see. Present it that way. Likely triggers, roughly by frequency: a session resume,
an MCP server connecting mid-session, a CLAUDE.md edit, a settings or plugin change altering the
tool list.

Drops at very short gaps (<1 min) are the strongest signal of the reported — but not officially
documented — behaviour where resuming a session invalidates the cache outright.

### Who was idle: `subcause` on `ttl_expiry`

The gap is split using human prompt timestamps. `type: user` entries with string or `text`-block
content are real prompts; entries carrying `tool_result` blocks are the machine loop and are ignored.

- `human_idle_minutes` — previous response to the human prompt that triggered this turn.
- `machine_busy_minutes` — that prompt to the request going out.

| `subcause` | Meaning | Prescription |
|---|---|---|
| `human_away` | A human prompt arrived after a gap longer than the TTL | `/clear` before breaks |
| `no_human_input` | No human prompt in the gap at all | `/clear` is useless here |
| `combined` | Both contributed | Address whichever dominates |

`no_human_input` is deliberately vague because the logs cannot disambiguate it: a long tool
execution, a session sitting on an unanswered **permission approval**, and a sleeping machine all
look identical. Approvals are not logged as user messages, so a prompt left unanswered over lunch is
indistinguishable from a long-running build. Say this plainly rather than guessing.

Never collapse the two. Telling someone to take shorter breaks when their agent was grinding
unattended for 76 minutes is wrong and will read as such.

## What the docs do NOT say

There is **no official break-even for "continue a long thread vs start fresh."** Under automatic
caching, per-turn write cost scales with the appended delta, not total length — so length alone is
not the cost driver. The cost driver is *re-caching*: expiry, invalidation, and cold resumes. Frame
recommendations around those events, not around raw session length, and do not invent a turn-count
threshold.

# Tokenomics Coach for Claude Code

An [Agent Skill](https://agentskills.io/specification) that audits your Claude Code session logs for
**avoidable token spend** — and tells you, in dollars, which sessions burned money and why.

This is not a prompt-quality coach. It does not care whether your prompts were "clear." It cares
about the prompt cache, because that is where the money actually goes.

## What it finds

Everything is computed deterministically from `~/.claude/projects/*.jsonl`. No estimates.

### Cache drops

The core detector finds every turn that **rebuilt a prefix which was warm on the previous turn**,
then attributes a cause:

| Cause | What happened | Avoidable? |
|---|---|---|
| `ttl_expiry` | The gap between requests exceeded the TTL. Sub-classified into `human_away` (you left) vs `no_human_input` (long tool loop, unanswered permission prompt, or sleeping machine) | Yes |
| `model_switch` | Changed model mid-session, invalidating the prefix | Yes |
| `invalidation_no_gap` | Cache died *inside* the TTL window — `CLAUDE.md` edit, MCP server connecting, tool change, or a resume | Yes |
| `compaction` | A `/compact` boundary. Expected and usually worthwhile | No |

Crucially, this is **not** just a "was the user idle >1h" check. A cache can collapse with zero idle
time, and those drops are invisible to gap-based analysis.

### Other patterns

- **Un-amortized 1h cache writes** — a 1h write costs 2× base input and needs ≥2 subsequent reads to
  beat not caching at all. Writes that never got them paid a premium for nothing.
- **Never-compacted large sessions** — a 300k-token context re-read every turn is a per-turn tax
  that grows with the thread.
- **Planning vs execution model spend** — split by `permissionMode`, so you can answer "am I paying
  Opus rates to write files?" Includes an `opusplan` estimate: what execution-phase Opus turns would
  have cost on Sonnet, net of the re-cache tax that toggling models actually incurred.

## HTML report

Every run writes a self-contained HTML report to `~/tokenomics-report-<date>.html`:

- Headline cards — total spend, avoidable spend, share wasted, read/write ratio
- A dollar-ranked **action list** — what to change, worth how much
- Daily stacked-bar spend chart, split into cache reads / cache writes / output
- Cache drops by cause, and the worst individual events
- Sortable per-session drill-down
- Planning vs execution split with the `opusplan` estimate

It is rendered **by `analyze.py`, not by the model** — so it costs zero tokens, never drifts between
runs, and works when you run the script directly. Single file, no external requests, theme-aware.
Override the path with `--html <path>` or skip it with `--no-html`.

## Sample output

The action list, ranked by what each change is worth:

```
30 days · $94.38 total · $24.93 avoidable (26%)

1. $17.10  Run execution on Sonnet
           279 Opus turns outside plan mode cost $44.80; the same tokens on
           Sonnet 4.6 would be $26.88, net of the $0.82 switch tax.
2.  $4.02  Consolidate short sessions
           70 one-hour cache writes never reached the 2 reads needed to break even.
3.  $2.70  Split very long unattended runs
           76 min of machine time on a 293k prefix, no human prompt in the gap.
           A tool loop, an unanswered permission prompt, or a sleeping machine.
4.  $2.26  Clear before long breaks
           2 re-caches after genuinely being away — worst was 1,289 min.
5.  $0.85  Change config between sessions
           4 cache collapses inside the TTL window; likely CLAUDE.md or MCP change.
6.      —  Compact long sessions at task boundaries
           4 sessions over 150k context with zero compaction, totalling $62.44.
```

Note items 3 and 4: both are TTL expiry, but only one is a break you took. Collapsing them would
produce the wrong advice for the more expensive of the two.

## Why the cache is the whole story

Verified against the [official pricing](https://platform.claude.com/docs/en/about-claude/pricing) and
[prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) docs
(2026-08-07):

| Operation | Multiplier vs base input |
|---|---|
| 5-minute cache write | 1.25× |
| 1-hour cache write | 2× |
| Cache read | 0.1× |

- **A cache read refreshes the TTL for free.** A continuously active session never re-caches,
  no matter how long it runs. *Length is not the cost driver — re-caching is.*
- **The TTL clock starts at request start**, not response end. Long streaming turns eat their own window.
- **Break-even:** a 1h write pays off after 2 reads; a 5m write after 1.
- Claude Code uses the **1h TTL on subscription auth, 5m on API-key auth**. Subagents are *always*
  5m, even on subscription.

Reading a warm 300k-token Opus prefix costs $0.15. Rebuilding it costs $3.00. That 20× gap, hit a few
times a week, is the entire thesis of this skill.

### Auth detection

Claude Code does not log whether you are on a subscription or an API key — and it doesn't need to.
The TTL is directly observable from which bucket each request wrote into
(`usage.cache_creation.ephemeral_1h_input_tokens` vs `ephemeral_5m_input_tokens`), so the analyzer
derives the real TTL per session rather than assuming one.

## Installation

### Install script

```bash
git clone https://github.com/hancengiz/tokenomics-coach-claude-code
cd tokenomics-coach-claude-code
./install.sh
```

### As a plugin

```
/plugin marketplace add hancengiz/tokenomics-coach-claude-code
```

### Manual

```bash
cp -R skills/tokenomics-coach-claude-code ~/.claude/skills/
```

Restart Claude Code afterwards.

## Usage

Just ask:

- "Where am I wasting tokens?"
- "Audit my cache efficiency for the last 30 days"
- "Why was yesterday's session so expensive?"
- "Am I paying Opus rates to write files?"
- "Should I be using opusplan?"

Or run the analyzer directly:

```bash
python3 skills/tokenomics-coach-claude-code/scripts/analyze.py --mode all --days 30 --pretty
```

| Mode | Reports |
|---|---|
| `cache` | Cache drops by cause, un-amortized writes, read/write ratio |
| `sessions` | Per-session cost, gaps over TTL, compaction, context growth |
| `models` | Model mix, planning vs execution split, switch costs |
| `all` | Everything |

Scope to one project with `--project ~/code/my-app`.

## Repository layout

Spec-compliant: the skill is the `skills/tokenomics-coach-claude-code/` directory, whose name matches
the `name` in the frontmatter.

```
tokenomics-coach-claude-code/
├── .claude-plugin/plugin.json
├── install.sh
└── skills/
    └── tokenomics-coach-claude-code/
        ├── SKILL.md
        ├── references/
        │   ├── cache-economics.md      # rates, TTL semantics, invalidation rules
        │   ├── session-hygiene.md      # /clear vs /compact, resumption, auto-compact
        │   └── model-strategy.md       # opusplan, plan vs execute, subagent costs
        └── scripts/
            ├── analyze.py
            └── pricing.json            # edit here when rates change
```

Validate changes:

```bash
npx skills-ref validate ./skills/tokenomics-coach-claude-code
```

## Privacy

Everything runs locally. The skill reads only your own `~/.claude/projects/` logs, reports project
directory names and 8-character session prefixes, and never quotes your code or prompts. Nothing
leaves your machine.

## Requirements

- Python 3.9+ (standard library only)
- Claude Code session logs in `~/.claude/projects/`

## Pricing data

`scripts/pricing.json` carries a `version` date. Published rates change — **Sonnet 5 introductory
pricing ends 2026-08-31**, after which rates rise 50%. Update that file rather than the skill body.

## License

MIT

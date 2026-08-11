# Tokenomics Coach for Claude Code

An [Agent Skill](https://agentskills.io/specification) that audits your Claude Code session logs and
tells you, in dollars, where your token spend went and how much of it was avoidable.

It is not a prompt-quality coach. It does not care whether your prompts were "clear." It cares about
the prompt cache and model selection, because that is where the money actually is.

Everything runs locally against `~/.claude/projects/*.jsonl`. Nothing leaves your machine.

## Install

### With the skills CLI

```bash
npx skills add hancengiz/tokenomics-coach-claude-code
```

### As a Claude Code plugin

```
/plugin marketplace add hancengiz/tokenomics-coach-claude-code
```

### With the install script

```bash
git clone https://github.com/hancengiz/tokenomics-coach-claude-code
cd tokenomics-coach-claude-code
./install.sh
```

### Manually

```bash
cp -R skills/tokenomics-coach-claude-code ~/.claude/skills/
```

Restart Claude Code afterwards. Requires Python 3.9+ (standard library only).

## Use

Ask in plain language:

- "Where am I wasting tokens?"
- "Why was yesterday's session so expensive?"
- "Am I paying Opus rates to write files?"
- "Audit my cache efficiency for the last week"
- "Add a live cache-hit indicator to my status line"

Or run it directly:

```bash
python3 skills/tokenomics-coach-claude-code/scripts/analyze.py --days 30
```

```
30 days · $109.86 spent · $10.79 measured waste + $21.50 estimated savings (29.4% of spend)
  1. $   21.50  Premium model used for routine execution                [estimated]
  2. $    4.96  Cached context expired between requests                 [measured]
  3. $    4.45  Long-lived cache entries that never paid for themselves [measured]
  4. $    0.85  Cache discarded while you were still working            [measured]
  5. $    0.52  Changing model mid-session discarded the cache          [measured]
  6. $    0.00  Large sessions that were never summarised               [measured]
json: ~/tokenomics-report-2026-08-07.json
html: ~/tokenomics-report-2026-08-07.html
```

**The default window is 30 days.** Use `--days N` for anything else, `--project ~/code/my-app` to
scope to one project, and `--mode cache|sessions|models` to run one family of checks.

## What it checks

| Finding | What it means | Confidence |
|---|---|---|
| `ttl_expiry` | Too long between requests, so the cached conversation had to be rebuilt | measured |
| `model_switch` | Changing model mid-session discarded the cached context | measured |
| `invalidation_no_gap` | Cache died with no gap, same model, no compaction | cost measured, trigger inferred |
| `unamortized_1h_writes` | Long-lived cache entries never reused enough to break even | measured |
| `uncompacted_sessions` | Large sessions never summarised — where your exposure sits | measured |
| `model_selection` | Premium model doing routine execution work | estimated |

**Measured and estimated are kept apart on purpose.** Measured waste is arithmetic: the exact
difference between rebuilding a cached context and reading it back. Estimated savings reprice your
real token counts on a cheaper model and assume the work would take the same number of tokens — an
assumption that does real work, so the report never merges the two into one number.

### Not just "were you idle for an hour"

The detector fires on a **cache drop**: any turn that rebuilds a context which was warm on the
previous turn. Idle time is only one cause. A cache can collapse with *zero* seconds of idle — an
MCP server connecting, a `CLAUDE.md` edit, a model switch — and gap-based analysis misses all of it.

It also splits idle time into **yours** versus **the machine's**, using your prompt timestamps. A
76-minute gap where you sent no prompt is not a coffee break; it is a long tool loop, an unanswered
permission prompt, or a sleeping laptop. Those need opposite advice, so they are reported separately.

## Why the cache is the whole story

Verified against the official [pricing](https://platform.claude.com/docs/en/about-claude/pricing) and
[prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) docs
(2026-08-07):

| Operation | Multiplier vs base input |
|---|---|
| 5-minute cache write | 1.25× |
| 1-hour cache write | 2× |
| Cache read | 0.1× |

- **A cache read refreshes the lifetime for free.** A continuously active session never re-caches,
  however long it runs. *Length is not the cost driver — rebuilding is.*
- **The clock starts when a request starts**, not when its response finishes. Long streaming turns
  eat their own window.
- **Break-even:** a 1-hour cache entry needs 2 reads to beat not caching; a 5-minute entry needs 1.
- Claude Code uses the **1-hour lifetime on subscription auth, 5 minutes on API keys**. Subagents are
  *always* 5 minutes.

Reading a warm 300k-token Opus context costs about $0.15. Rebuilding it costs about $3.00. That 20×
gap, hit a few times a week, is the entire thesis.

**Auth detection:** Claude Code never logs whether you are on a subscription or an API key, and it
does not need to. Which cache bucket each write landed in
(`ephemeral_1h_input_tokens` vs `ephemeral_5m_input_tokens`) reveals the real lifetime, so the
analyzer derives it per session instead of assuming.

## The report

Each run writes two files side by side:

- **`~/tokenomics-report-<date>.json`** — the report model, and the source of truth
- **`~/tokenomics-report-<date>.html`** — a self-contained page rendered from that JSON

The HTML has headline cards, a ranked action list, a daily spend chart split into cache reads /
writes / output, and a per-finding evidence table with plain-English explanations, real chat titles,
full working-directory paths, and a tooltip on every column heading.

It is rendered **by the script, not by a model** — so it costs no tokens, never drifts between runs,
and works when you run the script directly. Single file, no external requests, light and dark.

### Annotating a report

The JSON has `notes` at the top level and on every finding, both empty by default. Add your own
context — or let Claude add it — then re-render without re-reading any logs:

```bash
python3 scripts/analyze.py --render ~/tokenomics-report-2026-08-07.json
```

Notes appear in the page clearly marked as commentary. This is how you record things the logs cannot
know: *"that overnight run was deliberate"*, *"we're staying on Opus for this client"*.

### Sharing a report

The report names your chats and your directories, so it is not safe to forward as-is. Add
`--anonymize` for a copy that is:

```bash
python3 scripts/analyze.py --render ~/tokenomics-report-2026-08-07.json --anonymize
```

Chat titles become `Session 3`, working directories `~/projects/project-2`, session ids `sess0003` —
consistently, so the same placeholder means the same session on every table and the page still reads
as one story. Costs, token counts, timings, dates and model names are untouched; strip those and
there is nothing left to discuss. Your notes are scrubbed too: real titles, paths, your home
directory and e-mail addresses are substituted wherever they appear in prose.

Three files come out. `…-anonymized.json` and `…-anonymized.html` are the shareable pair, and the
page carries a banner saying so. `…-legend.json` maps the placeholders back to the real thing —
**that one stays on your machine.**

The skill offers this automatically once it has reported its findings. What it cannot catch is
something *you* named in a note that the logs never recorded — a client, an unreleased product — so
skim the copy before sending it.

## Watching it live, in your status line

The report is retrospective. Ask instead for *"a cache hit indicator in my status line"* and the
skill switches to live monitoring:

```
Cache Hit: 0.0% | ⚠ reprocessed! 100%→0% | ⏳ 60m (1h TTL)
```

**It does not install a status line.** It reads whatever you already have — your own script, a
third-party one, or nothing — asks what you want changed, and adds the segments in that file's own
idiom. What the skill actually carries is the arithmetic, because the obvious implementation is
wrong in a way that hides the problem it exists to catch.

### Anchor the hit rate to your prompt, not the latest turn

Every tool call writes another turn, and those turns only append a small delta to an already-warm
context, so they sit at 97–99% almost unconditionally. Read the *latest* turn and the number climbs
while the agent works — describing the tool loop instead of your prompt.

Anchor to the **first response after your last prompt**: the one turn that pays for the whole
accumulated context. Same session, same instant:

| Anchored to | Shows | What was actually true |
|---|---|---|
| Latest turn | 97.4% | the tool loop was warm |
| First response after the prompt | **0.0%** | `read=0, creation=83306` — rebuilt from scratch |

The latest-turn reading hid an 83k-token re-cache completely. The prompt-anchored one also holds
still while the agent works, which is what makes it readable at a glance.

The countdown uses the opposite anchor — the **most recent** turn of any kind — because a cache
read refreshes the lifetime for free, so the tool loop genuinely keeps the context alive.

### The other things that are easy to get wrong

- **De-duplicate by `requestId`.** Each turn is written to the transcript several times as it
  streams, every copy carrying the same `usage`. Sum them naively and every total silently
  multiplies — it never errors, the numbers are just too big.
- **Two hit-rate formulas** are both defensible — `read/(read+creation+input)` and
  `read/(read+creation)` — and give different answers. Label which one you used.
- **Compare like with like.** A reprocessing warning must compare two prompt-anchored turns; put a
  tool-loop turn on either side and everything looks like a cliff.
- **A ⚠ is not automatically waste.** After `/clear`, a compaction or a model switch it is expected
  — the switch above was deliberate, and the rebuild is its entry fee. It is worth investigating
  when *none* of those explain it.
- **A countdown will not tick without `refreshInterval`.** Status lines are event-driven by
  default, so the clock freezes between prompts. Set `"refreshInterval": 60` next to your
  `statusLine` command and display minutes — matching the two. Going down to 1 second re-parses the
  transcript every second in every open session, which costs real CPU for a smoother clock nobody
  reads that closely.

Full method in
[`references/live-cache-statusline.md`](skills/tokenomics-coach-claude-code/references/live-cache-statusline.md).

## Extending it

One rule per file. Each reads a shared read-only context and returns a finding or nothing; rules
never call each other, so changing one cannot break another. A rule that throws is skipped with a
warning instead of taking the report down.

```
scripts/
├── analyze.py              CLI entry point
├── pricing.json            rates — edit here when prices change
└── tokenomics/
    ├── pricing.py          rate lookup and per-request cost
    ├── collect.py          JSONL → Dataset (parsing only, no judgement)
    ├── signals.py          shared facts: cache drops, sessions, daily, totals
    ├── report.py           the JSON report model
    ├── anonymize.py        report JSON → shareable report JSON
    ├── html.py             report JSON → self-contained page
    ├── theme.py            stylesheet
    └── rules/
        ├── base.py                    Rule / Finding / Column contract
        ├── ttl_expiry.py
        ├── model_switch.py
        ├── invalidation.py
        ├── unamortized_writes.py
        ├── uncompacted_sessions.py
        └── model_selection.py
```

To add a check, drop a module in `rules/` exposing `rule = MyRule()`. It is discovered automatically
and appears in both the JSON and the HTML, with its own evidence table.

## Repository layout

Spec-compliant: the skill is the `skills/tokenomics-coach-claude-code/` directory, whose name matches
the `name` in its frontmatter. Validate changes with:

```bash
npx skills-ref validate ./skills/tokenomics-coach-claude-code
```

## Privacy

Local only. The report includes project paths, Claude's own chat titles, and 8-character session ids;
it never includes your code or prompt text. Those titles are generated from your conversations, so to
share a report, produce an anonymized copy with `--anonymize` (see
[Sharing a report](#sharing-a-report)) rather than sending the original.

## Pricing data

`scripts/pricing.json` carries a `version` date. Rates change: **Sonnet 5 introductory pricing ends
2026-08-31**, after which it rises 50%. Update that file rather than the skill body.

## License

MIT

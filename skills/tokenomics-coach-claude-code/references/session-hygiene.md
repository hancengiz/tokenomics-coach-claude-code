# Session Hygiene

Read when handling `--mode sessions`.

## `/clear` vs `/compact` vs doing nothing

| Action | Token cost | Effect on cache |
|---|---|---|
| `/clear` | **Free** | Starts a fresh session; previous one stays resumable |
| `/compact` | Costs a summarization request over the full history | Invalidates the conversation layer; rebuilds a shorter prefix |
| Do nothing | Every turn re-reads the whole prefix at 0.1x | Cache stays warm while active |

The decisive detail: **`/compact` is cheap only while the cache is warm.** Running it within the TTL
means the summarization request mostly reads cached tokens at 0.1x. Running it cold reprocesses the
entire history as uncached input. So compaction is a *warm-cache* operation — the worst time to
compact is exactly when a user comes back from a long break and sees a huge context.

Correct sequence after a long break on a big context: `/clear` and start fresh, or resume and accept
one re-cache. Do not resume-then-compact; that pays the re-cache and the summarization.

## Auto-compaction

- Triggers at roughly **83% of the context window** (~166k on a 200k model, ~830k on a 1M window).
- A buffer of ~13k–34k tokens is reserved for the response.
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` tunes the threshold (e.g. `60`). Some versions have been reported
  to silently cap above ~83% regardless of the override — treat the env var as best-effort.
- Auto-compaction is *necessary*, not wasteful. Never flag it as a problem. Flag its **absence** on
  large long-running sessions.

## Session resumption

`--continue` / `--resume` restore conversation history, model, agent, and permission mode. They do
**not** restore `--mcp-config`, `--settings`, `--plugin-dir`, `--fallback-model`, or `--add-dir` paths.

⚠️ **Community-reported, not officially documented:** resuming a session may invalidate the prompt
cache entirely, even when re-entering within seconds — making the first turn back into a long session
the most expensive request in it. The analyzer does not assume this; it only flags gaps that exceed
the TTL, which is provable from timestamps. If a user's data shows large re-caches at *short* gaps,
this is the likely cause — say so, and label it as unverified.

"Resume from Summary" (Pro/Max) appears when resuming a session idle >1h with >100k tokens. It runs
`/compact` immediately, trading discarded detail for a smaller ongoing prefix.

## Interpreting `--mode sessions`

- `never_compacted_large_sessions` — the structural finding. A 138-turn session at 328k peak context
  with zero compactions pays a large read tax on every single turn. Quote its `cost_usd` against the
  period total; a single session at 40% of two weeks' spend is a headline.
- `gaps_over_ttl` — count of forced re-caches within one session. Cross-reference the specific events
  in `cache.expiry_recache`.
- `longest_gap_minutes` — an overnight gap (>600 min) on a live session means the user resumed
  yesterday's thread this morning. This is the classic, and most expensive, habit.
- `peak_context_tokens` — comes from the largest `cache_read_input_tokens`, a good proxy for how
  large the prefix grew.
- `span_minutes` alone is **not** a cost signal. A 25-hour session with continuous activity re-caches
  never. Length only matters in combination with gaps or missing compaction.

## Recommendations worth making

Ranked by typical impact:

1. **Before stepping away for more than an hour on a large context, `/clear`.** Resuming costs the
   full write rate over the whole prefix; clearing costs nothing.
2. **`/compact` at task boundaries while still working**, not on return.
3. **Split unrelated work into separate sessions.** Carrying a finished task's context into the next
   one means paying to read it on every subsequent turn.
4. **Lower `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`** if sessions routinely run near the window ceiling.

Tie each recommendation to a computed figure from the user's own data. Do not deliver this list
generically.

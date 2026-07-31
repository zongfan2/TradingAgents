# Spec: Macro Brief — Collector

**Builder**: Codex · **Status**: Open · Contract: [macro-brief-data-contract.md](macro-brief-data-contract.md)

## Goal

A daily scheduled job that runs a deep web search for global macro conditions
and writes the brief file per the contract. Runs on the user's machine using
subscription-backed CLIs (`claude -p` with WebSearch, and/or `codex exec`) —
no per-search API billing.

## Bootstrap (works today, manual)

```bash
DATE=$(date +%F); mkdir -p ~/.tradingagents/macro_briefs && \
sed "s/{{DATE}}/$DATE/g; s/{{GENERATOR}}/claude-deep-search/g" prompts/macro_deep_search.md | \
claude -p --allowedTools "WebSearch,WebFetch" > ~/.tradingagents/macro_briefs/$DATE.md
```

The robust version below replaces this one-liner.

## Requirements

- **R1 — Render**: substitute in `prompts/macro_deep_search.md`:
  `{{DATE}}` = the as-of date (default: today, generation-local; `--date`
  override for catch-up) — always the same date used for the filename and
  frontmatter; `{{GENERATOR}}` = the invoked backend's identity
  (`claude-deep-search` for `--backend claude`, `codex-deep-search` or the
  model id for `--backend codex`).
- **R2 — Generate**: invoke the deep-search backend. Primary: `claude -p`
  with WebSearch/WebFetch allowed. Selectable backend (`--backend claude|codex`)
  so collector quality itself can be compared later.
- **R3 — Validate before write** (all checks are hard failures unless marked
  warn): frontmatter parses and fields present; `as_of_date` matches the
  filename; `generator` matches the invoked backend; all 7 sections present in
  order; ≥ 8 distinct citation URLs **counted from the body** (and frontmatter
  `sources_count` equals that body count — never trust the self-reported
  number); `**Impact**:` lines present in sections 1–6; word count within
  500–2500 (hard bounds; additionally warn when outside the 800–1500 target).
  On validation failure: retry once with the validator's error list appended to
  the prompt; then fail.
- **R4 — Atomic write**: temp file + rename into
  `$TRADINGAGENTS_MACRO_BRIEF_DIR` (default `~/.tradingagents/macro_briefs/`).
- **R5 — Idempotent**: if today's brief exists, skip (exit 0) unless `--force`.
- **R6 — Loud failure**: non-zero exit + one-line reason on stderr; never write
  a partial/invalid brief.
- **R7 — Optional S3 sync**: when `MACRO_BRIEF_S3_URI` is set, copy the brief
  (and later its eval json) after write; local remains the source of truth.
- **R8 — Log**: append one line per run (date, backend, sources_count, outcome)
  to `macro_briefs/collector.log`.
- **R9 — Schedule**: provide the schedule hook but do not hardcode a time —
  document `cron` and `launchd` examples; the user picks the slot (suggested:
  pre-US-market, e.g. 17:30 Asia/Shanghai on weekdays).

## Non-goals

- No reading of TradingAgents state, results, or presets.
- No trading signals or recommendations inside the brief — information only.
- Evaluation is the evaluator's job (separate spec); the collector's validation
  is structural only.

## Acceptance criteria

1. Fresh machine + logged-in `claude` CLI → one command produces a
   contract-valid brief file; second run same day exits 0 without rewriting.
2. Killing the process mid-run leaves no partial file.
3. Structural validation rejects a brief missing a section or citations, with a
   clear error, and the retry path fires exactly once.
4. `--date 2026-07-25` renders `{{DATE}}` as 2026-07-25 and back-fills
   `2026-07-25.md` as a best-effort reconstruction as of that date (the
   prompt's published-after-`{{DATE}}` cutoff applies). No in-body marker is
   needed: `generated_at` (actual run time, contract-required) being later than
   `as_of_date` is the backfill marker.

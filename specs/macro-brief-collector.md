# Spec: Macro Brief — Collector

**Builder**: Codex · **Status**: Open ·
Contract: [macro-brief-data-contract.md](macro-brief-data-contract.md) (v2)

## Goal

A scheduled job that runs a deep web search for global macro conditions and
writes one session-scoped brief per invocation (`cn` pre-Asia-open, `us`
pre-US-open). Runs on the user's machine using subscription-backed CLIs
(`claude -p` with WebSearch, and/or `codex exec`) — no per-search API billing.
Invoked by the [orchestrator](orchestrator.md); also runnable standalone.

## Bootstrap (works today, manual)

```bash
DATE=$(date +%F); mkdir -p ~/.tradingagents/macro_briefs && \
sed "s/{{DATE}}/$DATE/g; s/{{SESSION}}/us/g; s/{{GENERATOR}}/claude-deep-search/g" \
  prompts/macro_deep_search.md | \
claude -p --allowedTools "WebSearch,WebFetch" > ~/.tradingagents/macro_briefs/$DATE.us.md
```

The robust version below replaces this one-liner.

## Requirements

- **R1 — Render**: substitute in `prompts/macro_deep_search.md`:
  `{{DATE}}` = the as-of date in the session's local calendar (default: today;
  `--date` override for catch-up) — always the same date used for the filename
  and frontmatter; `{{SESSION}}` = `cn` | `us` (required `--session` flag);
  `{{GENERATOR}}` = the invoked backend's identity (`claude-deep-search` for
  `--backend claude`, `codex-deep-search` or the model id for `--backend codex`).
  The prompt template gains a session block: the `cn` render emphasizes the
  Asia session setup (overnight US close, Asia calendars); the `us` render
  recaps the Asia session and US pre-market data (releases land 08:30 ET).
- **R2 — Generate**: invoke the deep-search backend. The primary backend is
  selectable via config `collect_backend` (env `TRADINGAGENTS_COLLECT_BACKEND`),
  default `codex` per D19 — `codex exec` with web search enabled
  (`--skip-git-repo-check`: components inherit an arbitrary cwd). The codex
  invocation deliberately passes no `--model` (unlike the evaluator's pinned
  `gpt-5.6-terra`): collection rides the codex CLI's user-configured default
  model, and the stamped generator id stays the CLI-level `codex-deep-search`
  per R1. The other
  backend is `claude -p` with WebSearch/WebFetch allowed; an explicit
  `--backend claude|codex` overrides the config default per invocation so
  collector quality itself can be compared later.
- **R3 — Validate before write** (all checks are hard failures unless marked
  warn): frontmatter parses and fields present; `as_of_date` and `session`
  match the filename; `generator` matches the invoked backend; all 7 sections
  present in order; ≥ 8 distinct citation URLs **counted from the body** (and
  frontmatter `sources_count` equals that body count — never trust the
  self-reported number); `**Impact**:` lines present in sections 1–6; word
  count within 500–2500 (hard bounds; additionally warn when outside the
  800–1500 target). On validation failure: retry once with the validator's
  error list appended to the prompt; then fail.
- **R4 — Atomic write**: temp file + rename into
  `$TRADINGAGENTS_MACRO_BRIEF_DIR` (default `~/.tradingagents/macro_briefs/`).
- **R5 — Idempotent**: if this session's brief exists for the date, skip
  (exit 0) unless `--force`.
- **R6 — Loud failure**: non-zero exit + one-line reason on stderr; never write
  a partial/invalid brief. The orchestrator turns non-zero exits into status
  entries and notifications — the collector itself does not notify.
- **R7 — Optional S3 sync**: when `MACRO_BRIEF_S3_URI` is set, copy the brief
  (and later its eval json) after write; local remains the source of truth.
- **R8 — Log**: append one line per run (date, session, backend,
  sources_count, outcome) to `macro_briefs/collector.log`.
- **R9 — Scheduling**: the collector owns no schedule. The orchestrator invokes
  it once per session slot (see [orchestrator.md](orchestrator.md)); `--date`
  backfill remains available for catch-up.

### Runtime deadline policy (P0)

The first attempt receives the component budget minus headroom (the current
headroom is 120 seconds). Retry does not receive a speculative half-budget:
it may consume only the remaining time before the same absolute component
deadline. A retry that has no positive remainder fails immediately.

## Non-goals

- No reading of TradingAgents state, results, pools, or presets.
- No trading signals or recommendations inside the brief — information only.
- Evaluation is the evaluator's job (separate spec); the collector's validation
  is structural only.

## Acceptance criteria

1. Fresh machine + logged-in `codex` CLI (the D19 default backend; a logged-in
   `claude` CLI for `--backend claude`) → one command per session produces a
   contract-valid `YYYY-MM-DD.<session>.md`; a second run for the same session
   and day exits 0 without rewriting.
2. Killing the process mid-run leaves no partial file.
3. Structural validation rejects a brief missing a section, the `session`
   field, or citations, with a clear error; the retry path fires exactly once.
4. `--date 2026-07-25 --session us` back-fills `2026-07-25.us.md` as a
   best-effort reconstruction as of that date (the prompt's
   published-after-`{{DATE}}` cutoff applies). `generated_at` (actual run time)
   being later than `as_of_date` is the backfill marker.
5. The `cn` and `us` renders of the same date produce prompts that differ in
   the session block (covered by a unit test on the renderer).

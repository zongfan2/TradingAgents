# Pipeline v2 Operational Runbook

How to stand up, babysit, and tune the scheduled deep-search + paper-execution
pipeline on this machine. This is the *how*; the *why* lives in
[`specs/2026-08-03-pipeline-v2-design.md`](specs/2026-08-03-pipeline-v2-design.md)
and the per-component specs indexed in [`specs/README.md`](specs/README.md).
All commands run from the repo root with the project venv (`.venv/bin/python`).

## 1. Prerequisites

- **Python env**: `.venv` with the package installed editable, including the
  Alpaca SDK extra the execution adapter lazy-imports:

  ```bash
  uv pip install -e ".[execution]"
  ```

- **`.env` at the repo root** (never committed). Required keys:
  - Alpaca **paper** credentials. Canonical names `ALPACA_API_KEY` /
    `ALPACA_SECRET_KEY`; the alternate names `ALPACA_API_KEY_ID` /
    `ALPACA_API_SECRET_KEY` are accepted as fallbacks — this machine's `.env`
    uses the alternate pair. Use paper keys only: the adapter refuses any
    account whose number does not start with `PA` and pins the endpoint to
    `https://paper-api.alpaca.markets` (no override exists).
  - `FRED_API_KEY` — needed **only for the `feeds` arm** (macro data at
    analysis time). The `brief` arm is offline on the news side and needs no
    data-API keys.
  - LLM API keys only if you run per-token presets (`luna`/`deepseek`); the
    default pipeline runs on subscription CLIs.
- **Logged-in CLIs** (D19 role split, defaults from `pipeline/config.py`):
  - `codex` CLI — default **collection** backend (`collect_backend=codex`,
    env `TRADINGAGENTS_COLLECT_BACKEND`): macro briefs, pool nomination,
    ticker briefs.
  - `claude` CLI — default **evaluation** backend (`eval_backend=claude`,
    identity `claude-eval`, env `TRADINGAGENTS_EVAL_BACKEND`).
  - The two roles must use different backends (a match warns loudly —
    collector/evaluator independence).
- **Runtime home**: everything lives under `~/.tradingagents/`
  (`TRADINGAGENTS_STATE_DIR`) — briefs, pools, ledger, status files, logs.
  Never inside the repo.

## 2. Watchlists (core layer)

The user-maintained core layer is one YAML file per session, read verbatim by
the pool builder (`pool_builder.load_core` — read-only truth):

- `~/.tradingagents/pools/core.cn.yaml` — A-shares/HK (`.SS`/`.SZ`/`.HK` suffixes)
- `~/.tradingagents/pools/core.us.yaml` — everything else

Format: a YAML list of `{ticker, note?}` mappings (bare ticker strings also
accepted):

```yaml
- ticker: NVDA
  note: 英伟达 半导体
- ticker: MSFT
```

Editing rules:

- **Session suffix rule**: a ticker must belong to the file's market —
  `.SS`/`.SZ`/`.HK` ⇒ `cn`, anything else ⇒ `us`. A wrong-session ticker makes
  the pool build fail hard (holdings coverage must never silently shrink).
- **Soft cap ~10 per market** (both files currently hold 10). Core tickers are
  analyzed **every slot**, so each addition costs a daily ticker deep-search +
  paired analyses; the fan-out and run budgets absorb small growth but quota
  does not.
- A missing core file is only a warning (empty core); a malformed one is a
  hard failure. Dropped tickers simply stop being collected next slot — no
  scheduler surgery needed (design D4/D5).

## 3. Schedule install (launchd)

Preview what will fire, then install:

```bash
.venv/bin/python -m pipeline.install_schedule --print-schedule
.venv/bin/python -m pipeline.install_schedule
```

The installer writes three plists to `~/Library/LaunchAgents`
(`com.tradingagents.pipeline.cn`, `.us`, `.watchdog`) and **never runs
`launchctl` itself** — it prints the load commands for you to run:

```bash
launchctl load ~/Library/LaunchAgents/com.tradingagents.pipeline.cn.plist
launchctl load ~/Library/LaunchAgents/com.tradingagents.pipeline.us.plist
launchctl load ~/Library/LaunchAgents/com.tradingagents.pipeline.watchdog.plist
```

What the fire times look like on this host (US Eastern —
`/etc/localtime` → `America/Indiana/Indianapolis`), from `--print-schedule`:

- `cn`: slot_time 08:30 Asia/Shanghai → host **19:30 and 20:30 the previous
  local evening** (both DST renderings are installed; the wrapper gate makes
  the extra one a no-op).
- `us`: slot_time 08:30 America/New_York → host **08:30**.
- watchdog: every 3600 s.

Each fire runs `pipeline.orchestrator slot`, which only proceeds within
slot_time ± 15 min **in the session timezone**, on session-local weekdays;
any other fire exits 0. **Slot dates are session-local**: the cn slot that
fires Sunday evening host-time runs Monday's cn slot. The hourly watchdog
notifies (once per session/day) if a weekday slot has not started by
slot_time + 1 h. Changing `TRADINGAGENTS_SLOT_TIME_CN/US` requires re-running
the installer — plists and gate are both generated from config.

## 4. First manual slot

Run one slot by hand (no window gate) and watch it:

```bash
.venv/bin/python -m pipeline.orchestrator run --session us
.venv/bin/python -m pipeline.orchestrator run --session cn --date 2026-08-10
```

Recovery flags (mutually exclusive):

- `--only COMPONENT` — one component, by status-file name: `settle`,
  `macro_collector`, `macro_evaluator`, `pool_builder`, `ticker_collectors`,
  `ticker_evaluators`, `analysis_runner`, `execution_adapter`.
- `--from N` — resume from step N (0–7, same order as above).

Where to look:

| What | Path |
|---|---|
| Per-session status (rewritten after every state change) | `~/.tradingagents/pipeline_status.<session>.json` |
| Orchestrator log (size-rotated) | `~/.tradingagents/orchestrator.log` |
| launchd stdout/stderr | `~/.tradingagents/launchd.<session>.{out,err}.log` (+ `launchd.watchdog.*`) |
| Component logs | `~/.tradingagents/collector.log`, `analysis_runner.log`, `execution_adapter.log` |
| Slot lock (double-fire dedupe) | `~/.tradingagents/locks/<session>-<date>.lock` |
| Outputs | `macro_briefs/`, `ticker_briefs/`, `pools/`, `ledger/` under `~/.tradingagents/` |

Web UI (localhost-only, no auth — see [`specs/webui-pages.md`](specs/webui-pages.md)):

```bash
.venv/bin/uvicorn webui.server:app --port 8321
```

Open http://127.0.0.1:8321. Pipeline pages: **status banner** (per-component
red/green for both sessions; a session goes red if no slot started by
slot_time + 1 h), **pool view** (all four lists, gate verdicts, a
`carried_forward` warning badge), **briefs view** (macro + ticker briefs with
eval verdicts and flagged claims), **A/B aggregates** (computed on demand from
the ledger, never persisted), **decisions & orders** (decision rows joined
with order state and outcomes). Its only two write actions: a manual analysis
trigger (`trigger: manual` — never auto-executed) and the `EXECUTION_HALT`
toggle.

## 5. Dry-run ramp to paper execution

Execution is **off by default**: `execution_enabled=false`
(env `TRADINGAGENTS_EXECUTION_ENABLED`). The adapter still runs every us slot
and appends the orders it *would* place to `ledger/orders.jsonl` with
`dry_run: true` — so you review real order construction risk-free.

Quality checklist before flipping the switch (review over ≥ 1–2 weeks of
dry-run slots, in the webui decisions/orders and briefs pages):

- **plan_valid rate**: invalid TradePlans should be rare; a high invalid rate
  means the trader prompt or levels are broken — fix before executing.
- **Eval verdicts**: macro/ticker evals mostly `pass`/`warn`; recurring `fail`
  means collection quality problems (see §8) — `fail` rows never execute.
- **Trigger volume**: catalyst triggers per slot should be a handful, not the
  whole pool; a flood means the 7.0 threshold is too low for current briefs.
- **Order reasonableness**: dry-run qty/limit/stop/target consistent with the
  caps (per-order ≤ 15% equity, risk ≈ 1%/trade); no perverse sizes.

Enable (the env must be visible to the process running the slot — export it
in your shell for manual runs; for launchd jobs, `launchctl setenv
TRADINGAGENTS_EXECUTION_ENABLED true` and reload the jobs):

```bash
export TRADINGAGENTS_EXECUTION_ENABLED=true
```

Controls and guarantees:

- **Kill switch**: `touch ~/.tradingagents/EXECUTION_HALT` (or the webui
  toggle) ⇒ dry-run behavior; checked at startup **and** before every
  individual submission, so a mid-slot halt stops the remaining queue.
  Remove the file to resume.
- **Paper-only, fail-closed**: endpoint pinned to
  `https://paper-api.alpaca.markets` (the only endpoint literal in the
  codebase); both entrypoints fetch the account first and refuse to run unless
  the account number starts with `PA`. Live trading is a structural non-goal.
- Scope: us session only, `brief` arm only, BUY/SELL of longs only (no short
  opening), one entry per ticker/day, market-state guards (calendar, halt,
  quote age ≤ 15 min) skip rather than submit on any doubt.
- Manual-trigger records never auto-execute; the only manual path is
  `python -m pipeline.execution_adapter submit --run-id <id> --execute`
  (execution_enabled, the halt file, and every guard still apply).
  `submit --dry-run` forces dry-run regardless of config.

## 6. A/B campaign (feeds vs brief)

Pairing runs automatically once slots are scheduled — defaults (analysis
runner, env-overridable): `ab_pairing=paired` (`TRADINGAGENTS_AB_PAIRING`,
`off|paired`), 3 rotating core pairs per slot
(`TRADINGAGENTS_AB_CORE_PAIRS_PER_SLOT`) + every catalyst-triggered ticker,
budget `max_runs_per_slot=30` (`TRADINGAGENTS_MAX_RUNS_PER_SLOT`), preset
recorded per row (`--preset` / `TRADINGAGENTS_ANALYSIS_PRESET`). Only the
brief arm executes; feeds is shadow. Analysis jobs default to
`analysis_job_concurrency=2` (`TRADINGAGENTS_ANALYSIS_JOB_CONCURRENCY`): this
changes peak analysis load from one to two children, but does not change the
selected runs or theoretical total model spend. Arms within one ticker job
remain sequential, and ledger rows are written immediately.

Campaign discipline (design-doc A/B protocol + ledger contract caveats):

- **Read no conclusions before ≥ 100 pairs**, where a pair counts
  deduplicated `(date, session, ticker)` (latest complete attempt only) —
  roughly **2–4 weeks** at the default cadence across both slots.
- Read aggregates in the webui A/B page **with the caveats it renders**:
  catalyst-triggered pairs are a *conditional* comparison (selection decided
  by the brief arm's catalyst score — only core-rotation pairs approximate an
  unconditional one); same-ticker daily rows are not independent (see the
  per-ticker clustered / per-date stratified views); use eval-weighted
  variants (excluding `fail`-brief rows) alongside the raw ones; fills, not
  submissions, where execution matters. This is sequential monitoring, not a
  one-shot significance test.
- **Early stop**: brief eval fail-rate > 20% or anomalous direction
  disagreement ⇒ pause the campaign, fix collection quality (prompts,
  backend), and **restart the pair count** — don't average bad briefs in.
- **Decision tree after ≥ 100 pairs**:
  1. Brief arm no worse (direction agreement sane, hit-rate/excess return not
     inferior, plan quality comparable) → keep brief as the executing arm at
     near-zero info cost; move on to alpha attribution / backend comparison.
  2. Brief worse only in the unweighted view but fine eval-weighted → the
     collector is the problem: improve collection, resume the count.
  3. Brief genuinely worse → keep feeds as the information source; the brief
     subsystem stays as a shadow until collection improves.
- **One axis at a time**: an info-source A/B fixes the LLM preset; a backend
  comparison fixes the info source. Never tune both mid-campaign.

## 7. Threshold tuning

Strategy knobs, their verified defaults, and where they live. Change **one
axis at a time**, and let ≥ 2 weeks of slots accumulate before judging.

| Knob | Default | Override |
|---|---|---|
| Analysis trigger (`catalyst_score ≥`) | 7.0 | `TRADINGAGENTS_TRIGGER_THRESHOLD` |
| Pool entry / exit score | 6.0 / 4.0 | code default (`pool_builder.LifecycleRules`) — no env |
| Bollinger gate | 20-day window, 2.0σ, daily close ≤ upper band × 1.02, ≥ 60 daily bars (weekly leg effectively needs ~100) | code defaults (`pool_builder.GateRules`) |
| Liquidity floor (20d avg dollar volume) | us $20M / cn 100M (listing currency) | `TRADINGAGENTS_MIN_AVG_DOLLAR_VOLUME_US` / `_CN` |
| Volume confirmation (5d/20d ratio, else demote to watch) | 1.2 | `TRADINGAGENTS_VOLUME_CONFIRM_RATIO` |
| Plan sizing risk | `risk_pct` ~1%, validator bounds 0.1–2.0 | validator constant (ledger contract) |
| Adapter caps | per-order 15% equity, gross 100%, total risk 5%, ≤ 10 positions, ≤ 10 orders/slot, ≤ 2 tranches, ≥ 3 days apart, per-ticker 20%, pyramid-up only | code defaults (`execution_adapter.AdapterSettings`) — no env |
| Backend split (D19) | collect=`codex`, eval=`claude` | `TRADINGAGENTS_COLLECT_BACKEND` / `TRADINGAGENTS_EVAL_BACKEND` (must differ; per-run `--backend` overrides) |

Raising the trigger threshold cuts quota spend and sample size; lowering it
floods slots (the 30-run budget then drops catalyst runs). Entry/exit
hysteresis (6.0 in / 4.0 out) is deliberately asymmetric — enter fast, exit
slow; narrow it and the opportunity layer churns.

## 8. Troubleshooting

**Missed slot.** The watchdog notifies once per session/day when a weekday
slot hasn't started by slot_time + 1 h; check
`~/.tradingagents/launchd.<session>.err.log` and `orchestrator.log`. A held
slot lock (`~/.tradingagents/locks/<session>-<date>.lock`) makes double fires
exit 0 — normal; locks older than 12 h are auto-broken with a warning. To
recover a missed slot: `orchestrator run --session <s> --date <d>` (or
`--from N` if it died mid-slot — the status file names the failed step).
No catch-up slot is run without explicit authorization.

**Component failure ≠ slot failure.** Every component failure/timeout is
recorded in the status file and the slot continues, degrading per D12:
macro eval `fail` ⇒ runs forced to feeds + flagged (no auto-execution);
ticker eval `fail` ⇒ auto-trigger suppressed, core runs proceed with the
brief withheld; eval `missing` ⇒ proceed flagged, execution blocked unless
`TRADINGAGENTS_EXECUTE_ON_MISSING_EVAL`. Pool nomination failure degrades to
a **carried-forward pool** (webui badge), and a pool older than
`pool_max_staleness_days` (3) drops the opportunity layer — the ticker
collector falls back to core-only.

**Exit codes** (verified constants):

- macro_collector: 0 ok/already-collected, 1 any failure, 130 interrupted.
- ticker_collector: 0 ok, 1 failure or all tickers failed, 3 no tickers to
  collect (broken pool/core config), 130 interrupted.
- evaluator: 0 completed **regardless of verdict** (or skipped — hash match),
  1 unreadable input, 2 argparse usage, 3 backend failure (CLI failed or JSON
  invalid twice), 4 structural refusal — the *brief* violates its contract,
  i.e. a collector bug, not an evaluation result.
- analysis_runner: 0 slot ran (per-run failures become `decision: "ERROR"`
  rows), 1 hard failure, 2 usage, 130 interrupted.
- pool_builder: non-zero only on hard failures (unreadable core, contract or
  write errors); nomination trouble is the exit-0 carried-forward path.

**Eval `fail` verdicts.** Open the brief in the webui briefs page and read the
flagged claims. Verdicts are revision-bound (`brief_sha256`): re-collecting a
brief invalidates its old eval, and re-evaluation happens automatically next
slot (or manually: `python -m pipeline.evaluator <brief-path>`; `--force` to
override a hash match).

**Subscription quota exhaustion.** Collection burns the Codex quota, evals
the Claude quota (that split *is* D19 — Claude's monthly cap was hit once).
Symptoms: evaluator exit 3 / collector exit 1 with a backend CLI error.
Options: wait it out and backfill with `--date` (collectors skip
already-collected dates; `--force` to redo); or temporarily swap roles via
`TRADINGAGENTS_COLLECT_BACKEND` / `TRADINGAGENTS_EVAL_BACKEND` — keep them
different, a match warns loudly.

**Stale briefs.** The webui banner flags sessions with no slot by
slot_time + 1 h. Backfill any gap with
`python -m pipeline.macro_collector --session <s> --date <d>` (and the
ticker collector likewise, `--tickers CSV` for a subset). Same-day rewrites
archive the displaced revision (`archive/<name>.<generated_at>`), so ledger
hashes keep resolving.

**Timezone notes.** Slot dates and weekday gates resolve in the **session**
timezone, never host time: the cn slot for Monday fires Sunday evening
host-local, and its files are dated Monday. The us watchdog deadline is
09:30 America/New_York. When backfilling, `--date` means the session-local
calendar date.

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
- **Logged-in CLIs** (D20 roles, defaults from `pipeline/config.py`):
  - `claude` CLI — default backend for **both** roles, on different pinned
    models: collection (macro briefs, pool nomination, ticker briefs) on
    `claude-sonnet-5-5` / effort `medium`, evaluation (identity
    `claude-eval`) on `claude-opus-5-5` / effort `medium`. Every call is a
    hermetic web-only `claude -p` (no hooks, plugins, MCP servers or
    CLAUDE.md from your interactive setup) and appends its token usage to
    `~/.tradingagents/claude_usage.jsonl`.
  - `codex` CLI — optional; selectable per role via
    `TRADINGAGENTS_COLLECT_BACKEND` / `TRADINGAGENTS_EVAL_BACKEND`.
  - Collector and evaluator must not run on the same model (same backend
    and, for claude, the same pinned model warns loudly — independence).
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

### 3a. Schedule install on the Windows server (WSL)

Since 2026-10-15 production runs on the Windows server's WSL distro
(`Ubuntu-24.04`, checkout `/mnt/d/Projects/TradingAgents`). Under WSL the
installer defaults to `--target windows-tasks`: WSL only runs while something
starts it, so Windows Task Scheduler is the trigger. From a WSL shell in the
repo root:

```bash
.venv/bin/python -m pipeline.install_schedule --print-schedule
.venv/bin/python -m pipeline.install_schedule
```

It writes `pipeline.{cn,us,watchdog}.xml` — plus `pipeline.us-execution.xml`
when `TRADINGAGENTS_EXECUTION_TIME_US` is set — (UTF-16, as `schtasks` requires) to
`~/.tradingagents/windows-tasks/` and prints the registration commands, which
it never runs itself:

```bash
schtasks.exe /Create /TN '\TradingAgents\pipeline.cn' /XML "$(wslpath -w ~/.tradingagents/windows-tasks/pipeline.cn.xml)" /F
```

On this host (`Asia/Shanghai`, which the Windows timezone must match —
`tzutil.exe /g` shows `China Standard Time`):

- `cn`: **08:30**.
- `us`: `.env` sets `TRADINGAGENTS_SLOT_TIME_US=05:30` (New York), so
  **17:30 and 18:30** (EDT / EST renderings; the wrapper gate no-ops the
  wrong one). The default 08:30 would give 20:30 and 21:30.
- `us-execution`: `.env` sets `TRADINGAGENTS_EXECUTION_TIME_US=09:45` (New
  York), so **21:45 and 22:45**. The 05:30 us slot records step 7 as
  `skipped — deferred to the execution task`; this task runs
  `orchestrator execute`, which first runs a full `execution_adapter
  refresh` (re-submitting yesterday's unfilled closes with fresh quotes —
  settle's pre-market refresh skips that duty) and then submits in the
  finished slot once IEX quotes are fresh. Alpaca's default IEX feed has no quotes before ~09:20 New York,
  so an inline step 7 at ~07:00 skipped every order as `stale-quote`
  (specs/orchestrator.md "Deferred us execution"). If the slot is still
  running or never ran, execute skips and notifies; the watchdog alerts once
  if step 7 is still deferred (or the slot is stuck re-opened) at 10:45. Manual run:
  `.venv/bin/python -m pipeline.orchestrator execute --now [--date YYYY-MM-DD]`.
  If an execute was killed (sleep, shutdown, `wsl --shutdown`), its lock
  `~/.tradingagents/locks/us-<date>.lock` stays for up to 12 h and
  `execute --now` exits 1 naming it: confirm no `pipeline.orchestrator
  execute` process is alive, delete the lock, and run `execute --now` again
  (S4 dedupe keeps the re-run from double-submitting).
- watchdog: hourly on the hour.

Run the installer from a login shell (`ssh dev`, or `bash -l`). A non-login
shell has no `~/.local/bin` on PATH, so the installer warns `'claude' not
found` and bakes a PATH under which every scheduled collection fails.

Each task runs `conhost.exe --headless wsl.exe … /bin/sh -c "<job>"`, so no
console window appears, and appends to
`~/.tradingagents/scheduler.<job>.{out,err}.log`. Tasks run only while the
Windows user `Zzz` is logged on (a locked screen is fine) and wake the machine
from sleep. Notifications (failures, the watchdog's missed-slot alert, the
slot summary) appear as Windows toasts. They need WSL→Windows interop, and on
this systemd distro that only survives boots because of
`/etc/binfmt.d/WSLInterop.conf` (`:WSLInterop:M::MZ::/init:PF`; see
microsoft/WSL#8843). Without it, every `.exe` fails with "Exec format error"
and notifications degrade to log-only.

Toasts only reach someone sitting at the server. To also receive every
notification in Lark/Feishu, add a custom bot to a group (group settings →
Bots → Add bot → Custom bot), turn on **signature verification**, and put
both values in `.env` — they are credentials, never commit or paste them
elsewhere:

```bash
TRADINGAGENTS_LARK_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/<token>
TRADINGAGENTS_LARK_WEBHOOK_SECRET=<signing secret>
```

Write the values unquoted, with no trailing comment. Every job reads `.env`
at start, so no re-install is needed. A Lark failure (bad token, `code 19021`
sign mismatch, network) is logged as `lark notifier failed (...)` in
`scheduler.<job>.err.log` and never affects the slot or the toast. A
malformed value (quoted, commented, `http://`, a secret without a URL)
disables only Lark: each job logs `Lark notifications disabled: …` and runs
on with toasts. If the bot uses keyword security instead, the keyword
`TradingAgents` matches every message (it is the first line).

Inspect or remove the tasks with:

```bash
schtasks.exe /Query /TN '\TradingAgents\pipeline.us' /V /FO LIST
schtasks.exe /Delete /TN '\TradingAgents\pipeline.us' /F
```

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
| Windows-task stdout/stderr (WSL server) | `~/.tradingagents/scheduler.<session>.{out,err}.log` (+ `scheduler.watchdog.*`) |
| Component logs | `~/.tradingagents/collector.log`, `analysis_runner.log`, `execution_adapter.log` |
| Slot lock (double-fire dedupe) | `~/.tradingagents/locks/<session>-<date>.lock` |
| Outputs | `macro_briefs/`, `ticker_briefs/`, `pools/`, `ledger/` under `~/.tradingagents/` |

Web UI (localhost-only, no auth — see [`specs/webui-pages.md`](specs/webui-pages.md)):

```bash
.venv/bin/uvicorn webui.server:app --port 8321
```

On the Windows server, set `TRADINGAGENTS_WEBUI_PORT=8321` in `.env` and
re-run the installer: it adds `\TradingAgents\pipeline.webui`, which keeps
the server up on `127.0.0.1` (re-fired every 10 min; a running instance is
left alone; logs in `scheduler.webui.{out,err}.log`). It is never bound to
a network interface — view it from another machine through an SSH tunnel:

```bash
ssh -N -L 8321:localhost:8321 dev   # then open http://localhost:8321 locally
```

Open http://127.0.0.1:8321. Pipeline pages: **status banner** (per-component
red/green for both sessions; a session goes red if no slot started by
slot_time + 1 h), **pool view** (all four lists, gate verdicts, a
`carried_forward` warning badge), **briefs view** (macro + ticker briefs with
eval verdicts and flagged claims), **A/B aggregates** (computed on demand from
the ledger, never persisted), **decisions & orders** (decision rows joined
with order state and outcomes), **决策漏斗** (decision funnel: where every
considered ticker stopped and why, per slot, plus cross-date statistics and a
CSV export — specs/decision-funnel.md). Its only two write actions: a manual analysis
trigger (`trigger: manual` — never auto-executed) and the `EXECUTION_HALT`
toggle.

**Read-only viewing over Tailscale** (phone / another machine, no SSH;
decision-funnel R5). The tailnet URL can read every page but every write is
refused (403), and Tailscale **Funnel** (public internet) traffic is refused
outright — never run `tailscale funnel` for this port. One-time setup:

1. Tailscale admin console → DNS: MagicDNS on, **HTTPS Certificates
   enabled** (serve needs them).
2. Add the server's tailnet name to the Host allowlist in `.env` (plain
   hostname only — wildcards/ports are ignored):
   `TRADINGAGENTS_WEBUI_ALLOWED_HOSTS=desktop-p5u8dva.tailc36a4b.ts.net`,
   then restart the webui (it reads the value at start): stop the running
   `uvicorn webui.server:app` process; the `pipeline.webui` task starts it
   again within 10 minutes (or run the task by hand).
3. On the Windows host (PowerShell, or from WSL via
   `"/mnt/c/Program Files/Tailscale/tailscale.exe"`):
   `tailscale serve --bg 8321` — proxies `https://desktop-p5u8dva.tailc36a4b.ts.net`
   to `127.0.0.1:8321` (WSL localhost forwarding). Check with
   `tailscale serve status`; remove with `tailscale serve reset`.
4. Open `https://desktop-p5u8dva.tailc36a4b.ts.net/pipeline.html` on a device
   in the tailnet: the header shows **只读** and the halt toggle and manual
   trigger are hidden. Change state only through the SSH tunnel
   (`http://localhost:8321`) or `touch ~/.tradingagents/EXECUTION_HALT`.

Every device that can reach this node in the tailnet can read the pages
(positions, decisions, briefs) — keep the tailnet ACLs to your own devices.

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
| Backend roles (D20) | collect=`claude` on `claude-sonnet-5-5`/`medium`, eval=`claude` on `claude-opus-5-5`/`medium` | `TRADINGAGENTS_COLLECT_BACKEND` / `TRADINGAGENTS_EVAL_BACKEND`; `TRADINGAGENTS_CLAUDE_{COLLECT,EVAL}_{MODEL,EFFORT}` (roles must not share a model; per-run `--backend` overrides) |

Raising the trigger threshold cuts quota spend and sample size; lowering it
floods slots (the 30-run budget then drops catalyst runs). Entry/exit
hysteresis (6.0 in / 4.0 out) is deliberately asymmetric — enter fast, exit
slow; narrow it and the opportunity layer churns.

**Judge threshold changes on the decision funnel** (specs/decision-funnel.md).
Each slot writes `~/.tradingagents/funnel/<session>/<date>.json`, which records
where every considered ticker stopped and why: `stop.code` (e.g.
`below-threshold`, `watch`, `stale-quote`) and per-stage `provenance`
(`recorded` / `reconstructed` / `unknown`). The orchestrator builds it at slot
end and again after the us execute job; a build failure is only a `warn` on
the `funnel` component. To rebuild by hand:

```bash
.venv/bin/python -m pipeline.funnel build --session us --date 2026-10-26
.venv/bin/python -m pipeline.funnel backfill --since 2026-10-14   # every slot with inputs
```

**Analysis LLM routing and cost** (specs/llm-cost.md). `TRADINGAGENTS_LLM_ROUTING=claude-first`
runs each analysis on Claude (quick roles `claude-haiku-5-5` effort low,
judges `claude-opus-5-5` effort medium) while this calendar month's (UTC)
Anthropic spend is below `TRADINGAGENTS_ANTHROPIC_MONTHLY_BUDGET_USD` (190),
then on DeepSeek (`deepseek-v4-pro` / `deepseek-flash`, quick role without
thinking). `claude` / `deepseek` pin one profile; unset keeps the legacy
single-provider env. Every run's tokens and cost land in
`~/.tradingagents/llm_usage.jsonl`; the end-of-slot notification shows the
slot's LLM cost and the month's Anthropic spend. If Anthropic answers
"credit balance is too low" or "You have reached your specified API usage
limits" (the console's own spend limit), the runner writes
`~/.tradingagents/llm_budget/<YYYY-MM>.exhausted` and reruns that run on
the next rung — delete the marker to go back to Claude after topping up or
raising the console limit (otherwise Claude stays off until the next UTC
month). `python -m pipeline.llm_budget status` shows the marker's reason.
Run-volume cuts (same spec, R6): `TRADINGAGENTS_CORE_ANALYSIS_SESSIONS=us`
makes cn slots catalyst-only (core tickers carry funnel code
`core-session-off`); `TRADINGAGENTS_CORE_SKIP_UNCHANGED=on` skips a core
ticker whose last HOLD is ≤ 4 trading days old when nothing changed (no
position, no catalyst ≥ trigger, and no price move since that HOLD: no
session ≥ 5 %, total < 8 %) — funnel code `core-unchanged`; every core
ticker still gets a full run at least weekly. Unknown price data (a
yfinance failure, a stale tape, a gap, a split) runs the ticker, so a
data outage or a holiday raises core run volume. A session outside
`TRADINGAGENTS_CORE_ANALYSIS_SESSIONS` also collects and evaluates no core
briefs (R8). The Claude CLI steps (collection, evaluation) bill the same
Anthropic key and count toward the same monthly budget; once it is spent
they run on DeepSeek Flash through DeepSeek's Anthropic-compatible endpoint
(R7). `claude_usage.jsonl` lines carry `billing` and `billed_usd`.

## 7a. Trade notifications (separate Lark group)

Trades go to their own Lark group (specs/trade-notifications.md); the
existing bot keeps the ops messages. Setup, once:

1. In Lark create a group, add a **custom bot** with signature verification,
   and put its webhook and secret in `.env`:
   `TRADINGAGENTS_LARK_TRADES_WEBHOOK_URL=…` and
   `TRADINGAGENTS_LARK_TRADES_WEBHOOK_SECRET=…`.
2. Re-run the schedule installer, which now also generates
   `pipeline.trade-monitor.xml` (always-on listener), and register that one
   task from an **elevated** Windows PowerShell (re-registering tasks from
   WSL is refused: "Access is denied"):
   `schtasks /Create /TN "\TradingAgents\pipeline.trade-monitor" /XML <path> /F`.

The listener sends each order placed, fill, stop/target exit (with P&L) and
cancel/expiry/reject within seconds; the hourly watchdog re-sends anything
it missed and, after 16:15 ET on trading days, the daily report. It only
reads the paper account. Check it: `~/.tradingagents/scheduler.trade-monitor.err.log`.

The trades group also gets one **research digest** per slot (cn and us,
right after the end-of-slot summary), in Chinese: positions and planned
orders, the macro read, and every ticker grouped as analysed (the action in
plain words), blocked, triggered but not analysed, or not triggered (with
the reason)
(specs/research-digest.md). It is built from the slot's files plus one
small summary call (Haiku, ≈ $0.002) and sent once per slot day, only by a
full slot run (never by
`--only`). Where a missing one shows up: the slot's own attempt logs to
`scheduler.<session>.err.log` and the hourly retry to
`scheduler.watchdog.err.log`. Both run the same build-and-send path, so
either log can show any of these lines:

- `research digest for <session> <date> not sent` — a local failure; the
  digest stays pending and is retried.
- `research digest not queued within 90s — the hourly watchdog retries it`
  / `research digest not started` — slot end only; retried.
- `research digest not marked pending … — sending without a retry` — slot
  end only; sent at once, but not retried if that send fails.
- `research digest failed` — slot end only (the module could not load);
  **not retried**: re-run the slot once fixed.
- `research digest: macro brief unreadable` / `research digest: skipped a
  malformed funnel row` — that line or row only; the digest still goes out.
- `research digest: Chinese summaries unavailable` — the one small model
  call failed or timed out (45 s); the digest goes out without the 要点 /
  per-name summary lines.
- `research digest retry failed` — watchdog only.
- `research digest for <session> <date> dropped: not queued within 3 days`
  — watchdog only; given up.
- `trade notification research:<session>:<date> not sent` (Lark refused it,
  it stays queued) / `… dropped after 10 failed sends` (given up).

`scheduler.trade-monitor.err.log` (the listener flushes the same queue)
can show only the last pair.

## 7c. Daily review and data archive

After every US trading day the hourly watchdog starts the day's review
once the New York clock is past 17:00 (specs/daily-review.md), inline (its
log is `scheduler.watchdog.err.log`): it archives
the day under `/mnt/d/massive/stock/<category>/<YYYY-MM-DD>/` (override
`TRADINGAGENTS_ARCHIVE_DIR`) — macro and ticker briefs with their evals,
funnel snapshots, ledger decisions and orders, research digests, closes,
LLM usage, a read-only broker snapshot — and sends one Chinese review to
the Lark trades group: summary, issues with evidence, a strategy verdict
(需要优化 / 继续观察 / 暂不调整) and proposals. **It never changes the
strategy**: act on a proposal yourself. Copies land in
`reviews/<date>/review.md` and `review.json`. Marks:
`~/.tradingagents/daily_review/<date>.started|done` (a run stuck 2 h is
retried). By hand: `python -m pipeline.daily_review run --date D`;
backfill the archive only: `python -m pipeline.daily_review archive --since D`.

## 7b. LLM provider ladder (Claude → OpenAI → DeepSeek)

With `TRADINGAGENTS_LLM_ROUTING=ladder` (specs/llm-provider-ladder.md) the
pipeline spends Anthropic credits first (budget $190 per UTC month), then
OpenAI, then DeepSeek. The OpenAI API key cannot read the account balance,
so **confirm it once a month** from the OpenAI console before the rung is
used; until then it is skipped (Lark: "OpenAI balance unconfirmed"):

```bash
.venv/bin/python -m pipeline.llm_budget confirm openai 71   # USD left this month
.venv/bin/python -m pipeline.llm_budget status
```

Spend recorded after the confirmation is deducted; the rung stops $5
before zero (`TRADINGAGENTS_OPENAI_BALANCE_MARGIN_USD`), and OpenAI's own
`insufficient_quota` error writes `llm_budget/openai/<YYYY-MM>.exhausted`
(delete it after a top-up, then confirm again). Collection and evaluation
on this rung run `codex exec` hermetically on the API key
(`~/.tradingagents/codex_home`, never `~/.codex`), launching codex's
native binary directly (found behind the npm `codex` launcher, so a
timeout kills it). The scheduled jobs' PATH must find `codex` — without
it the CLI steps skip to DeepSeek. On the Windows server this is a
symlink in a directory the tasks' PATH already has (re-registering the
tasks needs an elevated Windows shell):
`ln -s ~/.nvm/versions/node/<version>/bin/codex ~/.local/bin/codex`
(re-point it after an nvm Node upgrade).

## 8. Troubleshooting

**Missed slot.** The watchdog notifies once per session/day when a weekday
slot hasn't started by slot_time + 1 h; check
`~/.tradingagents/launchd.<session>.err.log` and `orchestrator.log`. A held
slot lock (`~/.tradingagents/locks/<session>-<date>.lock`) makes double fires
exit 0 — normal; locks older than 12 h are auto-broken with a warning. To
recover a missed slot: `orchestrator run --session <s> --date <d>` (or
`--from N` if it died mid-slot — the status file names the failed step).
No catch-up slot is run without explicit authorization.

**"⚠ analysis …: N/M runs failed".** Most analysis runs of a slot ended in
ERROR. The alert names the kinds: `API key refused` means a provider
answered 401 — the ladder already stepped down for that slot (Anthropic →
OpenAI → DeepSeek), but every rung refusing (or a fixed routing) leaves the
slot without decisions: fix the key in `.env`, then re-run the slot from
step 6 (`orchestrator run --session <s> --date <d> --from 6`, the session
and date the alert names) — `--from 6` also reaches step 7: inline it
submits, deferred it records the skip that re-arms the missed-execute
watchdog (`--only analysis_runner` would leave recovered plans unexecuted
with no signal). Past the us execution time, submit with
`orchestrator execute --now --date <d>` afterwards (without `--date` it
targets today's New York slot, not the recovered one). `credits
exhausted` / `timed out` point at the budget and the run timeout.

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

**Subscription quota exhaustion.** Under D20 collection *and* evals burn
the Claude quota (D19 had moved collection to Codex after Claude's monthly
cap was hit once). Watch `~/.tradingagents/claude_usage.jsonl` (one line per
call: component, model, effort, `outcome` ok/error, tokens, `total_cost_usd`)
for the trend — a run of `error` lines is the earliest sign of a limit.
Symptoms: evaluator exit 3 / collector exit 1 with a backend CLI error.
Options: wait it out and backfill with `--date` (collectors skip
already-collected dates; `--force` to redo); move collection back to Codex
with `TRADINGAGENTS_COLLECT_BACKEND=codex`; or lower
`TRADINGAGENTS_CLAUDE_COLLECT_EFFORT` / `TRADINGAGENTS_CLAUDE_EVAL_EFFORT`.

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

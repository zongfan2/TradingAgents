# Spec: Execution Adapter (Alpaca paper)

**Builder**: Claude Code · **Status**: Open · Contract:
[trade-plan-ledger-contract.md](trade-plan-ledger-contract.md)
(plan reader, sole `orders.jsonl` writer)

## Goal

Turn validated `brief`-arm TradePlans from the `us` session into Alpaca
**paper** orders, with structural guarantees that this code can never touch a
live account. The adapter is the **only broker gateway in the system**: it has
two entrypoints, `submit` (slot step 7) and `refresh` (invoked by the settle
step for order-status refresh and stale-order hygiene). Nothing else — settle
included — constructs a broker client.

## Safety invariants (non-negotiable, tested)

- **S1 — Paper-only, fail-closed**: the endpoint is hard-pinned to
  `https://paper-api.alpaca.markets`; no env/config override exists for it. At
  startup (both entrypoints) the adapter fetches the account and **refuses to
  run** unless `account_number` starts with `PA`. Any violation ⇒ abort before
  any order.
- **S2 — Opt-in**: config `execution_enabled` (env
  `TRADINGAGENTS_EXECUTION_ENABLED`), default **false**. When false, `submit`
  runs dry: it appends the `orders.jsonl` rows it *would* place with
  `"dry_run": true` and submits nothing. (`refresh` is read-mostly and always
  allowed; its cancels are hygiene, see below.)
- **S3 — Kill switch**: the file `~/.tradingagents/EXECUTION_HALT` existing ⇒
  submit behaves as if `execution_enabled=false`. Checked at startup **and
  immediately before every individual submission**, so a mid-slot halt stops
  the remaining queue. Touchable by hand or from the webui.
- **S4 — One entry per ticker per day**: before submitting an entry, the
  adapter must find no prior `submitted` row with `dry_run: false` and
  `order_kind: bracket` for the same `(date, session, ticker)` — the dedupe
  key is the ticker/day, NOT
  `run_id`, so reruns, `--force` slots, and recovery passes cannot double-buy.
  The deterministic `client_order_id` (`<date>-<session>-<ticker>-entry`)
  makes the broker a second line of defense. Crash recovery: an `intent` row
  without a result row ⇒ query the broker by `client_order_id` before
  deciding.
- **S5 — Eligibility**: only records with `session: us`, `arm: brief`,
  `plan_valid: true`, `action: BUY | SELL`, and `trigger: core | catalyst`.
  Manual-trigger records are never auto-executed; a human runs
  `execution_adapter submit --run-id <id> --execute` to execute one
  deliberately. No short opening: a SELL with no long position at the broker
  is logged (`skip`, reason `no-position`) — and close qty never exceeds the
  broker's reported available+held qty. No market orders.
- **S6 — Caps** (config, defaults): per-order notional ≤ 15% of equity (BUY
  only — closes are risk-reducing and exempt); gross exposure after the order
  (positions market value + open entry-order notional) ≤ 100% of equity
  (`max_gross_exposure`, no margin use); open positions + pending entry
  orders ≤ 10; ≤ 10 live submissions per slot. The 15% notional cap is sized
  so the 1% risk budget binds first whenever the stop distance ≥ ~6.7% — the
  cap is a backstop, not the effective sizer.
- **S7 — Secrets**: `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` read from `.env`
  (canonical names win; the alternate names `ALPACA_API_KEY_ID` /
  `ALPACA_API_SECRET_KEY` are accepted as fallbacks); never logged, never
  echoed into ledger or status files.
- **S8 — Market-state guards** (checked before any live submit): the broker
  calendar/clock must say today is a trading session (holiday/weekend ⇒ skip
  all, reason `market-closed`; half-days are fine — DAY orders respect the
  early close); the asset's broker status must be tradable (suspensions ⇒
  skip, `not-tradable`); the latest trade/quote timestamp must be within
  `max_quote_age` (default 15 min; else skip, `stale-quote`). Alpaca exposes
  no authoritative real-time exchange-halt feed — the quote-freshness guard
  is the operational halt proxy (a halted name goes stale within minutes),
  which is why `max_quote_age` must stay small. **Fail-closed**: if any guard
  source (clock, calendar, asset, quote) errors or is unreachable, skip with
  reason `guard-unavailable` — never submit on missing information.
- **S9 — Portfolio constraints**:
  - A BUY for a held ticker without `add_intent` ⇒ skip (`maintain`) — not an
    error; the plan is a view refresh, not an order.
  - With `add_intent`, an **add-on tranche** is allowed only when ALL hold:
    existing tranches for the ticker < `max_tranches_per_ticker` (default 2);
    the new `entry_zone[0]` is above the ticker's last fill price
    (pyramid into strength — `pyramid_up_only`, default true; averaging down
    stays excluded); ≥ `add_spacing_days` (default 3) trading days since the
    last fill; position market value + new order notional ≤
    `per_ticker_notional_cap` (default 20% of equity). Each violated
    condition skips with its specific reason. Every tranche is its own GTC
    bracket; a SELL close cancels all open orders for the ticker and closes
    the **entire** position, all tranches.
  - Required buying power is checked before submit (insufficient ⇒ skip,
    `buying-power`); total open risk — Σ over open positions and pending
    entries of (entry − stop) × qty — must stay ≤ `max_total_risk` (default
    5% of equity) after the order.

## Order mapping

- **BUY** ⇒ bracket order, **TIF GTC**: limit at `entry_zone[1]` (the worst
  acceptable fill), `qty` from sizing, stop-loss leg at `stop`, take-profit
  leg at `targets[0]`. GTC keeps the protective legs alive for the plan's
  multi-day horizon — a DAY bracket would silently drop the stop at the close
  of entry day.
  - Entry-order lifecycle: `refresh` cancels any **unfilled** entry order older
    than 1 trading day (the next slot's plan decides re-entry). Filled
    positions keep their GTC stop/TP legs until a leg fills or a SELL close
    cancels them.
- `qty = floor((equity × sizing.risk_pct/100) / (entry_zone[1] − stop))`,
  reduced to honor the notional cap; qty 0 after caps ⇒ `skip` row, reason.
- **SELL (close)** ⇒ a two-step sequence: (1) cancel **all open orders** for
  the ticker (bracket legs included — otherwise the close is rejected for held
  qty, or a leg plus the close double-sells into a short); (2) submit a DAY
  limit for the full available qty at `entry_zone[0]` when present, else at
  the broker's current bid − 0.2% (marketable limit from a live quote — never
  from a stale close). An unfilled close at day end is re-submitted **once**
  by the next `refresh` as a fresh marketable limit under the next attempt's
  `client_order_id` (`-close-2` — Alpaca rejects reused client ids), then
  escalated to a notification if still unfilled.

## Requirements

- **R1**: `submit` is invoked as a subprocess by the orchestrator and reads
  the slot's `decisions.jsonl` rows itself (no in-process handoff), applies
  S1–S9 in order, and appends one `orders.jsonl` row per attempt/outcome —
  `intent` before each live submit, then `submitted`, or `skip` with a reason.
- **R2**: broker interaction via `alpaca-py` pinned in requirements.
- **R3**: `refresh` appends `refresh` rows with current broker status/fills
  for every non-terminal order (partial fills record `filled_qty` — protective
  legs follow the broker's own bracket qty adjustment; a `rejected`/`canceled`
  status triggers a notification), performs the two hygiene duties above
  (stale entry cancels, close re-submission), rewrites the `positions.json`
  snapshot from live broker state (ledger contract), and never submits new
  entries. The broker is the source of truth for positions and order states;
  the ledger mirrors it, never the reverse.
- **R4**: any broker API error on one order logs and continues to the next
  (never aborts the slot); summary line to stdout for the orchestrator.
- **R5**: `--dry-run` flag forces S2 behavior regardless of config.

## Non-goals

- Live trading (S1 makes this structural). Options, shorts, crypto. Trailing
  stops, averaging **down**, partial closes, portfolio rebalancing,
  sector/correlation concentration limits (post-v2). Free-form manual orders — the only manual path is
  `submit --run-id <id> --execute` against an existing decision record. CN/HK
  brokers (future futu OpenD adapter).

## Acceptance criteria

1. Tests with a faked broker client: S1 refusal on non-`PA` account (both
   entrypoints), S2/S3 dry-run incl. mid-queue halt, S4 ticker/day dedupe
   across distinct run_ids + intent-row recovery path, S5 filters
   (manual-trigger exclusion, short-block, close-qty clamp), S6 caps counting
   pending entries incl. the gross-exposure ceiling, S8 market-state guards
   (closed day, non-tradable asset, stale quote, guard-unavailable each ⇒ the
   right skip reason), S9 add-on rules (maintain skip without add_intent;
   tranche cap, pyramid-up, spacing, and per-ticker notional each
   individually violated ⇒ its specific skip) plus buying-power / total-risk
   skips, sizing math incl. cap reduction and qty-0 skip.
2. Order-mapping tests: GTC bracket construction; refresh cancels a 2-day-old
   unfilled entry but not a filled position's legs; SELL cancels legs before
   closing and re-submits an unfilled close exactly once.
3. A fixture slot of mixed plans produces the exact expected `orders.jsonl`
   (dry-run) with reasons on every skip.
4. Grep-level check: the paper URL literal appears exactly once (the pin) and
   no live-endpoint literal exists in the codebase.

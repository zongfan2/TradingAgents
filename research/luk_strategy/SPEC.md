# Luk-style momentum swing strategy — isolated research backtest

Status: research v1 · Builder: Claude Code (user-requested 2026-10-13) · Branch
`claude/luk-strategy-backtest`.

This package approximates the publicly described method of Martin Luk (2025
USIC winner, Qullamaggie school): leaders-only momentum swing trading with
three setups (episodic pivot, consolidation breakout, pullback), opening-range
entries, low-of-day stops, ~0.5% risk per trade, partial profit after a few
days and a moving-average trail on the rest. Sources are second-hand
summaries; every rule below is an explicit, a-priori approximation — none of
the parameters are fitted to the backtest.

## 1. Isolation rules (hard)

- Lives only under `research/luk_strategy/`. It must not import `pipeline`,
  `tradingagents`, `cli`, `webui`, `compare`, or `devtools`, and nothing there
  may import it. It does not touch `specs/`, `pyproject.toml`,
  `requirements.txt`, or any existing file.
- Dependencies: stdlib, numpy, pandas, alpaca-py, python-dotenv only (all
  already in `.venv`). No pyarrow / matplotlib / scipy. Caches are pickles.
- Runtime data, caches, and results go under `~/.tradingagents/research/luk/`
  (override with env `LUK_DATA_DIR`). Tests must use `tmp_path` and must never
  read or write the real home directory or hit the network.
- Credentials: `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` from the
  environment; if absent, load the primary checkout's `.env`, found as the
  parent of `git rev-parse --git-common-dir` (override with env `LUK_DOTENV`).
  Never print or log key values.
- Run from the worktree root:
  `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m research.luk_strategy.cli <cmd>`.
  Tests: `.venv/bin/python -m pytest research/luk_strategy/tests -q`.
  `ruff check research/` must pass (repo ruff config).

## 2. Time discipline (no look-ahead)

For session day `t`:

- **Pre-open info** = daily bars `≤ t-1` only. All features in `features.py`
  are computed "as of the close" of their row; anything used for day `t`
  before the open reads row `t-1` (`iloc[i-1]`). There is exactly one place
  that performs this shift: `signals.build_watchlists`.
- **At-open info** adds `open_t` (daily bar open, and its unadjusted value) —
  used only for EP gap detection, gap/chase checks and the daily-mode stop cap.
- **Intraday info** comes only from 5-min bars up to and including the bar
  being processed.
- **At-close info** (`close_t`, features row `t`) may be used only for exits
  executed at the close of `t`.
- The daily `high_t`, `low_t`, `close_t`, `volume_t` must never influence the
  candidate list or entries of day `t` in intraday mode.

## 3. Universe (per day, from data ≤ t-1)

- Symbol source: Alpaca `us_equity` assets, active + inactive, exchange in
  {NYSE, NASDAQ, AMEX, ARCA, BATS}; symbol matches `^[A-Z]{1,5}$`; name does
  not match (case-insensitive regex) any of: ` ETF`, `\bETNs?\b`, ` Funds?\b`,
  `\bWarrants?\b`, `\bUnits?\b`, `\bRights\b`, `Preferred`, ` Notes`, `%`,
  `ProShares`, `Direxion`, `iShares`, `SPDR`, `Invesco`, `Leveraged`, `Inverse`,
  ` 2X`, ` 3X`; and does not contain ` Trust` unless it also contains
  `Corporation|Corp|Bancorp|Bancshares` as a word (Northern Trust Corporation,
  Washington Trust Bancorp stay; grantor trusts and trust-named funds go). SPY
  and QQQ are always fetched as benchmarks/regime inputs but are never
  tradable candidates.
- Cache consistency: when a re-fetch of the split-adjusted daily cache changes
  a symbol's adjusted close on a date by more than 0.5% (a split/adjustment
  booked since the last fetch, or a corrected bar), that symbol-day is dropped
  from the intraday cache so the next `fetch-intraday` re-downloads it on the
  same adjustment basis. (Data guard, not a strategy rule.)
- Point-in-time tradable universe (`universe_mask[t]`, computed as of close
  `t`): close ≥ `min_price` (5), 20-day average dollar volume ≥ `min_adv`
  ($10M), ADR20 ≥ `min_adr` (3.5%), ≥ `min_history` (150) bars, counted since
  the last run of more than 20 consecutive missing sessions (Alpaca keys history
  by today's ticker, so a reused ticker would otherwise inherit the previous
  issuer's bars; `hist_len`).
- EP universe (`ep_universe_mask[t]`): close ≥ 5, ADV20 ≥ `ep_min_adv` ($5M),
  ≥ 150 bars (no ADR requirement — EP names are often quiet before the gap).

## 4. Features (as of close of each row)

SMA10/20/50, EMA9/10/21 (on close, `adjust=False`), ADR20 =
mean(high/low − 1, 20), ATR14 (Wilder), ADV20 dollars = mean(close×volume, 20),
ADV20 shares, ret21/63/126 = close/close.shift(n) − 1, high63 = rolling max of
high over 63 incl. the row, high10 = rolling max high over 10,
range5 = (max high − min low over 5)/close, bars-since-high10 (sessions since
the high10 bar), hist_len (bars since the last > 20-session gap, §3). RS: per row, percentile rank (0–1, `pct=True`) of
ret21, ret63, ret126 among `universe_mask` stocks; `rs_score` = max of the
three; NaN outside the universe.

Regime (as of close, read at `t-1`):
- `risk_on_index`: QQQ close > SMA20 and SMA10 ≥ SMA20.
- `leaders_count`: universe stocks with close ≥ 0.9×high63 and ret63 ≥ 0.30;
  `risk_on_breadth`: leaders_count ≥ its own 20-day SMA.

## 5. Setups

Watchlists are built for every session without applying the regime filter or
the per-setup candidate cap order dependence: the cap is applied first (by the
ranking below) and the regime flags are stored on each candidate, so the
engine can apply any regime mode without needing different intraday data.

**Tick.** Prices are split-adjusted, so every "+0.01" / "−0.01" below is one
*tick* = one as-traded cent in adjusted units at `t-1`:
`tick = 0.01 × close_{t-1} / raw_close_{t-1}` (0.01 when the raw close is
unknown). A fixed adjusted 0.01 would encode later splits (NVDA 2017: $0.40).

**Leader** (BO and PB): `rs_score ≥ 0.95` and `max(ret21, ret63, ret126) ≥ 0.30`
and in `universe_mask` — all at `t-1`.

### BO — consolidation breakout
At `t-1`: leader; close ≥ SMA20 and SMA10 ≥ SMA20 ≥ SMA50; close ≥ 0.85×high63;
pivot = high10; bars-since-high10 ≥ 2 (pivot set at `t-3` or earlier, i.e. ≥ 2
quieter sessions after it); range5 ≤ 2.0×ADR20; close ≥ pivot×(1 − 1.5×ADR20).
Rank by rs_score desc, keep top `bo_max_candidates` (25).

Intraday trigger: from the 09:35 bar on, level `L = max(pivot, ORH) + tick`
where ORH = high of the 09:30 bar. First bar with high ≥ L and bar start <
`bo_cutoff` (11:00 ET) triggers; raw fill = max(L, bar open). Chase guard:
skip if raw fill > pivot×(1 + `bo_max_extension`×ADR20) (1.0).

### EP — episodic pivot
At open: in `ep_universe_mask[t-1]`; gap = open_t/close_{t-1} − 1 ≥ `ep_min_gap`
(0.10). Data guard: if the raw/adjusted factor at open_t differs from the one at
close_{t-1} by more than 1% (Alpaca books some spin-offs as splits), the raw gap
raw_open_t/raw_close_{t-1} − 1 must also be ≥ `ep_min_gap`. Optional neglect
filter (`ep_require_neglect`, default False): ret63_{t-1} ≤ 0.30. Rank by gap desc, keep top `ep_max_candidates` (15).

Intraday trigger: requires 09:30-bar volume ≥ `ep_min_first_bar_vol_frac`
(0.10) × ADV20 shares_{t-1}; from the 09:35 bar on, level `L = ORH + tick`,
cutoff `ep_cutoff` (11:00 ET); raw fill = max(L, bar open).

### PB — pullback to rising EMAs (Luk's 2025 emphasis)
At `t-1`: leader; SMA20 > SMA50; close > SMA50; EMA21 > EMA21 five sessions
earlier; high63 bar within the last 15 sessions (fresh leader);
close < max(close over the 5 sessions before `t-1`); low ≤ EMA9×1.01;
close ≥ EMA21×0.97; volume < ADV20 shares. Rank by rs_score desc, keep top
`pb_max_candidates` (25).

Intraday trigger: requires open_t < high_{t-1}; from the 09:35 bar on, level
`L = high_{t-1} + tick`, cutoff `pb_cutoff` (15:30 ET); raw fill = max(L, bar open).

### Common entry rules (intraday mode)
- Fill = raw fill × (1 + slip_bps/1e4).
- Initial stop = (low of day known at the fill) − tick, where that low is the
  min of the lows of all bars from 09:30 up to, not including, the trigger bar
  and the trigger bar's open (its first trade, at or before the fill). The
  trigger bar's low is not used: it may print after the fill, so it must not
  widen the stop or decide the width filter; it is checked as an entry-day stop
  instead (§8.1). Floor: if (fill − stop)/fill < `min_stop_pct` (1%), set
  stop = fill×(1 − min_stop_pct).
- Width filter: skip if stop_pct > min(`max_stop_adr_mult`×ADR20, `max_stop_pct`)
  (1.0×ADR, 8%). For EP use `ep_max_stop_pct` (8%) without the ADR term.
- One position per symbol at a time. Same symbol in several setups on one day:
  earliest trigger wins; ties → EP > BO > PB.

### Daily entry mode (ablation `entry_mode="daily"`, no intraday data)
BO: L = pivot + tick; PB: L = high_{t-1} + tick (requires open_t < high_{t-1});
EP: buy at open_t (raw fill = open_t). Triggered if high_t ≥ L (EP: always,
plus the gap test); raw fill = max(L, open_t). Stop = min(max(low_{t-1}, fill×(1 −
min(ADR20, max_stop_pct))), open_t) − tick for BO/PB (never above open_t: the open
traded before the buy-stop at L > open_t could fill, so a stop above it would make
`low_t ≤ stop` a certainty created by pre-fill trading); fill×(1 − min(ADR20,
ep_max_stop_pct)) for EP; same floor/width rules (width re-applied to the capped
stop, tolerance one tick). Same-day exit: if low_t ≤ stop → stopped at stop
(conservative: sequence unknown; reported per setup as `entry_day_stops`). No
cutoffs, no EP volume check.

## 6. Regime application (engine)
`regime_mode` ∈ {"index" (default), "breadth", "both", "none"}. Risk-off day:
no new BO/PB; EP allowed if `ep_in_risk_off` (True) with risk × 0.5.

## 7. Sizing & portfolio
- `initial_equity` 100,000; long-only; no margin by default.
- risk$ = `risk_per_trade` (0.5%) × E_{t-1} × candidate risk multiplier, where
  E_{t-1} = mark-to-market equity at the prior close.
- qty = floor(risk$ / (fill − stop)), then reduce so qty×fill ≤
  `max_position_pct` (25%) × E_{t-1}, and so that after the buy the gross
  exposure (Σ qty×last mark incl. this buy) ≤ `max_gross` (1.0) × E_{t-1} and
  cash ≥ 0. Skip if qty < 1.
- Caps: `max_new_per_day` (6), `max_open_positions` (12).
- Day-t triggers are processed in timestamp order (ties: EP > BO > PB, then
  rs_score desc / gap desc). Capital released by intraday stop-outs on day t
  is not reused on day t (conservative); capital released at the open of t
  (gap through stop) is.
- Slippage `slip_bps` (10) on every fill, adverse direction. Commission 0.

## 8. Exits
Luk exits (`exit_mode="luk"`, default):
1. Entry day (intraday mode): the trigger bar first: if its low ≤ stop →
   exit at the stop (certain when the fill was the bar's open; otherwise the
   order inside the bar is unknown and the stop-out is the conservative
   assumption; the trigger bar's high is never used for a contract target).
   Then from the bar after the trigger bar through the last bar of the session,
   if bar low ≤ stop → exit at min(stop, bar open).
2. Later days (daily bars): if open ≤ stop → exit at open; elif low ≤ stop →
   exit at stop. Stop checks precede every close-based rule.
3. Partial: at the close of the `partial_day`-th session after the entry day
   (3) — or of the first later session with a bar if that session has none —
   if close > entry fill, sell `partial_frac` (1/3, floor, ≥1 share) and raise
   stop to max(stop, entry fill) (breakeven). Evaluated exactly once.
4. Trail: from the `partial_day`-th session after the entry day on (i.e. after
   the partial/breakeven decision; before it only the stop applies), exit the
   remainder at the close if close < `trail_ma` (default `sma10`; options
   `ema10`, `sma20`, `ema21`) computed including that close. Ablation
   `trail_after_partial=False`: trail from the first session after entry.
5. Time stop: exit at the close after `max_hold` (60) sessions.
6. Data gap: if a held symbol has no bar for `data_gap_sessions` (5)
   consecutive sessions, exit at its last close (`data_end`). Where a symbol's
   data finally ends is never consulted (that is future information); a
   position still open on the last session exits as `end_of_test`.

Contract-like exits (`exit_mode="contract"`, ablation mirroring our pipeline's
trade-plan rules): stop distance floored at 0.5×ATR14_{t-1} (stop = min(stop,
fill − 0.5×ATR14)); fixed take-profit at fill + `contract_target_r` (2.0) × R;
time exit at the close of session `contract_horizon` (10). Daily bar: open ≥
target → fill at open; elif high ≥ target → target. If stop and target are both
touched in one daily bar → stop first. No partials or trail.

All exit fills apply slippage adversely (price × (1 − slip_bps/1e4)).
R multiple = position P&L / (qty × (entry fill − initial stop)).

## 9. Evaluation & outputs
Period: first trading day of 2017 → latest cached session (2016 is warmup).
Benchmarks: SPY and QQQ buy-and-hold (same split-adjusted price series).
Results dir `~/.tradingagents/research/luk/results/<run_name>/`: `trades.csv`,
`equity.csv`, `summary.json` (metrics overall/by year/by setup/by exit
reason, config snapshot, data coverage notes), `report.md`.

Metrics: total return, CAGR, max drawdown (+ duration), annualized vol,
Sharpe (rf=0, daily), Sortino, MAR (CAGR/|MaxDD|), win rate, avg win %, avg
loss %, avg win R, avg loss R, expectancy R, profit factor, trades/year, avg
hold sessions, avg gross exposure, share of total P&L from the top 10% trades,
best/worst trade, yearly returns vs SPY/QQQ, triggers skipped by reason.

Also reported: entry-day stops per setup (`entry_day_stops`: count, share,
P&L, mean R) and data coverage (`coverage`: symbols whose data ends more than
10 sessions before the panel end, by year; test years with fewer than 10% of
the busiest year's early endings flagged as effectively survivor-only; the
`no_data` rate of evaluated candidates split by data status).

Ablations (`cli ablate`): baseline; BO only; EP only; PB only; contract exits;
daily entry mode; regime none; regime breadth; slippage 25 bps; risk 1%; trail
sma20; trail from day 1 (`trail_day1`); all cutoffs 15:30; stops capped at 3% (`tight_stop3`, Luk's stated typical stop). Output
`ablation_table.csv` + `.md`.

## 10. Known limitations (must appear in every report)
Survivorship: the symbol list is today's Alpaca asset list filtered by current
exchange, so many delisted names are missing entirely (e.g. SIVB, FRC, BBBY,
TWTR), pre-delisting history is uneven by year (on the 2026 cache almost all of
it ends in 2018–2022, so 2017 and 2023+ are effectively survivor-only; the
report computes this from the panel), and delisted/renamed tickers more often
lack 5-min bars (intraday `no_data`); no news/catalyst or pre-market data for
EPs; no sector/theme judgment; 5-min bar granularity and intra-bar ordering;
SIP split-adjusted prices without dividends, where Alpaca's adjustment also
folds in some spin-offs and has a few misdated factors (EP gaps are re-checked
on raw prices; other uses are not corrected); no borrow / margin / taxes;
second-hand description of a discretionary trader; parameters set a priori
(by-year results are the only robustness view in v1).

## Changelog
Rule changes after the v1 review (2026-10-14). Each item names the review finding.
- §5 intraday initial stop (LA-1, F6): built from the low of day known at the
  fill (bars before the trigger bar + the trigger bar's open), no longer from
  the trigger bar's low, which may print after the fill; the width filter is
  therefore decided on pre-fill prices only. §8.1: the trigger bar's low is
  checked against that stop as an entry-day stop.
- §5 daily entry mode (LA-2): BO/PB stop capped at open_t − tick and the width
  filter re-applied, so a pre-fill price can no longer make the same-day stop
  certain. Entry-day stops are reported per setup (§9).
- §5 tick (LA-6, F3): "+/−0.01" is one as-traded cent converted to adjusted
  units at t−1 instead of a fixed adjusted 0.01.
- §5 EP gap (LA-4): raw-gap re-check when the adjustment factor moves between
  close_{t-1} and open_t.
- §3 min_history (F4): `hist_len` restarts after more than 20 consecutive
  missing sessions.
- §3 name filter (F7): word-anchored patterns; ` Trust` exempt for
  Corporation/Corp/Bancorp/Bancshares names. Re-run `fetch-daily` to add the
  newly included symbols (e.g. BTSG, NTRS, MANU) to the cache.
- §3 cache consistency (F1, execution): intraday symbol-days are invalidated
  when a daily re-fetch changes their adjustment.
- §8.3 partial (F8): taken at the first session with a bar from `partial_day`
  on; evaluated once.
- §8.4 trail (F1, fidelity): starts at `partial_day` (after the partial /
  breakeven), per the method's "partial after a few days, then trail the
  rest"; the old day-1 trail is the `trail_day1` ablation
  (`trail_after_partial=False`). Decided on a-priori fidelity, not on results.
- §8.6 data gap (LA-5): `data_end` fires only after `data_gap_sessions`
  consecutive missing bars; the full-panel "data ends here" shortcut is gone.
- §9/§10 (LA-3): coverage disclosure (early endings by year, survivor-only
  years, `no_data` split by data status) and a more specific survivorship
  limitation.
- Independent audit (re-derived all baseline trades from raw caches without the
  package code; 4,068/4,068 match under the rules below):
  - §3 the price floor uses the **as-traded (unadjusted) close**; split-adjusted
    history would encode future splits.
  - §3 zero-volume flat bars (`volume == 0 and high == low`) are Alpaca
    placeholders, not trades, and are dropped on load (they spliced delisted and
    reused tickers into continuous histories).
  - §5 opening range: if the 09:30 bar is missing, the earliest bar starting
    before 09:45 serves as the opening range (and for the EP first-bar volume
    test); otherwise `no_data`. Entries are skipped as `price_mismatch` when the
    first 5-min open differs from the daily open by more than 15%.
  - §5/§8 touches are float-tolerant (relative 1e-9): a print exactly at a
    trigger level, stop or target counts.
  - §5 intraday bars at or after the exchange's session close (half-days close
    at 13:00) are dropped using Alpaca's calendar.
  - §8.3 the partial sells floor(qty×partial_frac) (≥1 share) only if shares
    remain afterwards; the breakeven stop move applies either way.
- Codex review (PR #4):
  - §7 whole-share rounding (qty floors, caps, the partial's ⅓) is done in
    **as-traded shares** using the candidate's point-in-time adjustment factor
    (`tick`/0.01); quantities are then carried in split-adjusted units for
    accounting. Rounding in adjusted units let later reverse splits shrink or
    zero historical positions. Results moved by a few points (baseline −61.2% →
    −58.3%); conclusions unchanged.
  - Data plumbing: daily cache batches record their start and are re-fetched
    when they do not cover a requested start; batches are written atomically;
    the panel prefilter and warmup derive from the configured thresholds and
    `min_history` (defaults unchanged).

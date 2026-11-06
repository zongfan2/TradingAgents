# Spec: Daily post-close review and data archive

**Builder**: Claude Code · **Status**: Implemented (2026-11-07)

## Why

User decision 2026-11-07: after every close, review the day's operations,
judge whether the strategy needs optimising, send the result to Lark, and
keep all the related data, categorised and by date, under
`/mnt/d/massive/stock` (files chosen over Postgres: none runs on the server).
The 11-06 retrospective found real gaps only by putting each day's research,
decisions and orders next to the market's actual moves (MU bought on a
gap-down, three slots of 401 errors, core names skipped before a −17% day);
this makes that a daily habit instead of an ad-hoc one.

## Design

### R1 — Archive (deterministic)

`pipeline.daily_review.archive_day(config, day)` copies or extracts, for the
US trading date `day`, into `<root>/<category>/<day>/…` (root: env
`TRADINGAGENTS_ARCHIVE_DIR`, default `/mnt/d/massive/stock`):

| category | content |
|---|---|
| `macro_briefs` | `<session>.md` + `<session>.eval.json` (cn, us) |
| `ticker_briefs` | `<TICKER>.md` + `<TICKER>.eval.json` |
| `funnel` | `<session>.json` (the slot's decision-funnel snapshot) |
| `decisions` | `decisions.jsonl` — the ledger rows dated `day` |
| `orders` | `orders.jsonl` — the ledger order rows whose client order id is dated `day` |
| `digests` | `<session>.txt` — the research digest as sent |
| `prices` | `closes.csv` — date, ticker, close, change % for the day's names and benchmarks |
| `llm_usage` | `cli.jsonl`, `analysis.jsonl` — the usage rows of `day` |
| `broker` | `account.json`, `positions.json`, `fills.json` — read-only Alpaca snapshot (only when archived on the day itself) |
| `reviews` | `review.md`, `review.json` (R2) |

Every write is atomic and idempotent: re-archiving a day rewrites the same
content. A missing input is skipped (and counted), never an error.
`python -m pipeline.daily_review archive --since D [--until D]` backfills.

### R2 — Review

`pipeline.daily_review.build_facts(config, day)` assembles only structured
facts (no brief prose — that is web-derived text and stays out of the
prompt):

- per us/cn name of the day: brief impact word, catalyst score, eval verdict,
  trigger code, decision and PM rating, execute outcome, the day's close
  change and excess vs the benchmark (SPY / 000300.SS / ^HSI);
- names skipped or not analysed that moved ≥ 5 % that day;
- the macro brief's six Impact words and eval verdict;
- account equity change, fills, open positions (us);
- analysis errors by kind, entry-zone skips, other execute skips;
- a rolling scoreboard over the last 20 archived sessions: next-session
  excess return by brief impact word and by PM rating, with counts.

One model call (the evaluator's model, `claude_command(..., role="eval")`,
schema-bound JSON, 300 s timeout, usage component `daily_review`) writes, in
Chinese: a summary, issues (each with its evidence and severity), a strategy
verdict — `需要优化` / `继续观察` / `暂不调整` — and proposals (change, why,
evidence with counts, risk). The prompt requires evidence from the facts
only, calls a sample under 10 too small for a strategy change, and forbids
inventing data. **The review never changes the strategy**: proposals go to
the user, who decides.

The result is rendered as one Lark message to the trades group through the
trades outbox (key `review:<day>`, exactly once), and written to
`reviews/<day>/review.md` and `review.json`. Without the model (failure,
timeout) the facts alone are sent with `复盘模型不可用` — the day's data is
never lost.

### R3 — Schedule

The hourly watchdog (`_hourly_trade_notifications`) starts the day's review
once the New York clock is past 17:00 on a weekday and the day's us funnel
snapshot exists: it launches `python -m pipeline.daily_review run --date D`
as a detached process (the model call can outlast the watchdog's run) and
returns. A `reviews/<day>` marker under `state_dir/daily_review/` records
started/done; a run started more than 2 h ago without done is retried; a
done day is never re-run. No new scheduled task is registered.

## Prior art (specs/README.md rule 6)

No survey: no external integration. The archive is a copy of files the
pipeline already writes; the model call reuses the hermetic CLI path the
evaluator uses; delivery reuses the trades outbox (prior art in
specs/trade-notifications.md).

## Acceptance criteria

1. `archive_day` writes each category under `<root>/<category>/<day>/`,
   atomically and idempotently; missing inputs are skipped.
2. `build_facts` contains no brief prose, only structured fields and numbers.
3. One review message per day reaches the trades group; a model failure
   still sends the facts.
4. The review proposes, never applies, a strategy change.
5. The watchdog starts the review once per day after 17:00 ET, detached;
   a stuck run is retried after 2 h.
6. Tests are offline.

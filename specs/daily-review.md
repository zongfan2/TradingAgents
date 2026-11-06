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
| `digests` | `<session>.txt` — the research digest as sent (research_digest writes `state_dir/research_digest/<session>/<date>.txt` when it queues one) |
| `prices` | `closes.csv` — date, ticker, close, change % for the day's names and benchmarks |
| `llm_usage` | `cli.jsonl` (the CLI usage log the writer uses, rows whose New York date is `day`), `analysis.jsonl` |
| `broker` | `account.json`, `positions.json`, `fills.json` — read-only Alpaca snapshot (only when archived on the day itself) |
| `reviews` | `review.md`, `review.json` (R2) |

Every write is atomic and idempotent: re-archiving a day rewrites the same
content. A missing or unreadable input is skipped and counted
(`<category>:missing`) and **never replaces an archived file with an empty
one** (an unreadable ledger, a failed price fetch, a date older than the
data window). The review archives after its model call, so the day's
`daily_review` usage row is included, and reuses the facts' closes.
`python -m pipeline.daily_review archive --since D [--until D]` backfills.

### R2 — Review

`pipeline.daily_review.build_facts(config, day)` assembles only structured
facts (no brief prose — that is web-derived text and stays out of the
prompt):

- per us/cn name of the day: brief impact word, catalyst score, eval verdict,
  trigger code, decision and PM rating, execute outcome, the day's close
  change and excess vs the benchmark (SPY / 000300.SS / ^HSI);
- names without a decision that moved ≥ 5 % that day (missed moves);
- the macro brief's six Impact words and eval verdict;
- account equity change, fills, open positions (us);
- analysis errors by kind, entry-zone skips, other execute skips;
- every funnel entry of the session, a brief-less one too (a failed
  collection, a watch name), with its layer and collect code;
- analysis errors by kind (`llm_usage.error_kind`) and the day's fills;
- a rolling scoreboard over the last 20 us sessions with a snapshot: the
  **decision day's own session** excess return (the pre-market decision's
  day — it includes the overnight gap before the 09:45 execution) by brief
  impact word and by PM rating, with counts.

One model call (the evaluator's model, `claude_command(..., role="eval")`,
schema-bound JSON, 300 s timeout, usage component `daily_review`) writes, in
Chinese: a summary, issues (each with its evidence and severity), a strategy
verdict — `需要优化` / `继续观察` / `暂不调整` — and proposals (change, why,
evidence with counts, risk). The prompt requires evidence from the facts
only, calls a sample under 10 too small for a strategy change, and forbids
inventing data. **The review never changes the strategy**: proposals go to
the user, who decides.

A call that used a web tool (tool-call fields or a billed search) is
rejected: the review is the facts' alone. The output is checked before it
reaches anyone: every field has its schema shape and no text carries a
link or markup (`<`, `>`, a scheme, `www.`); a failure falls back to the
facts. Every step but the send is guarded (facts, model, archive), so the
day's message is always queued.

The result is rendered as one Lark message to the trades group through the
trades outbox (key `review:<day>`, exactly once), and written to
`reviews/<day>/review.md` and `review.json`. Without the model (failure,
timeout) the facts alone are sent with `复盘模型不可用` — the day's data is
never lost.

### R3 — Schedule

The hourly watchdog (`_hourly_trade_notifications`, guarded) calls
`maybe_start`: once the New York clock is past 17:00 on a weekday whose us
funnel snapshot exists, it runs the review **inline** (a few minutes, well
inside the watchdog task's 15-minute limit; its log is the watchdog's). Marks
`state_dir/daily_review/<date>.started|done`: a started run older than 2 h is
retried, a done day never re-runs; the done mark is written once the message
is queued (or when no trades channel is configured — the review is then only
archived, with a warning). By hand, `python -m pipeline.daily_review run
--date D` loads the repo `.env` first, like every job.

## Prior art (specs/README.md rule 6)

- **freqtrade** — https://github.com/freqtrade/freqtrade (GPL-3.0, design
  reference only, nothing copied; recorded in specs/trade-notifications.md):
  its `/daily`, `/performance` and `/profit` Telegram reports summarise a
  bot's trades per day from its own trade database. Taken: one daily
  summary built from the system's own records, sent where the trades go.
  Not taken: profit-only reporting — this review also judges the research
  and the gates, and its strategy verdict is advisory.
- **The repository's own retry pattern** (trade outbox, research-digest
  pending marks): started/done marks with a stuck-retry age, so a review
  that died mid-run is not lost and a done one never repeats.

## Acceptance criteria

1. `archive_day` writes each category under `<root>/<category>/<day>/`,
   atomically and idempotently; missing inputs are skipped.
2. `build_facts` contains no brief prose, only structured fields and numbers.
3. One review message per day reaches the trades group; a model failure
   still sends the facts.
4. The review proposes, never applies, a strategy change.
5. The watchdog runs the review once per day after 17:00 ET, inline; a
   stuck run is retried after 2 h; a by-hand run loads `.env`.
7. A model call that used the web, a malformed or unsafe review, or any
   failure before the send still sends the facts; the archive never
   replaces a file with an empty one.
6. Tests are offline.

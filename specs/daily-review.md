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
| `prices` | `closes.csv` — date, ticker, close, change % for the day's names and benchmarks (a change across a missing close, or a missing bar — the name's previous bar is not the benchmark's previous session —, is empty, never a two-session move); `quote_filled.json` — the names whose archived close came from the live quote (R2), kept until a later archive refreshes that row from a listed close |
| `llm_usage` | `cli.jsonl` (the CLI usage log the writer uses, rows whose New York date is `day`), `analysis.jsonl` |
| `broker` | `account.json`, `positions.json`, `fills.json` — read-only Alpaca snapshot (only when archived on the day itself) |
| `reviews` | `review.md`, `review.json` (R2) |

Every write is atomic and idempotent: re-archiving a day rewrites the same
content. A missing or unreadable input is skipped and counted
(`<category>:missing`) and **never replaces an archived file with an empty
one** (a missing or unreadable ledger or usage log, an empty or row-less
read of one — a truncated ledger —, an empty source file, a failed price
fetch, a date older than the data window); `closes.csv` is
merged — a partial refetch refreshes rows, never drops one. A root that
cannot be written (a missing drive) leaves the day without a done mark, so
the stuck retry archives it later (the message, already queued, is not
resent): the review as queued (facts, review, text) is kept in
`state_dir/daily_review/<date>.queued.json` before the send, and a retry
re-queues and archives that same text — no second model call. The review archives after its model call, so the day's
`daily_review` usage row is included, and reuses the facts' closes.
`python -m pipeline.daily_review archive --since D [--until D]` backfills.

### R2 — Review

`pipeline.daily_review.build_facts(config, day)` assembles only structured
facts (no brief prose — that is web-derived text and stays out of the
prompt):

- a day's close Yahoo has not listed yet (a NaN close, or no row — 510300.SS
  and ^HSI on 2026-11-10) is filled from Yahoo's chart quote
  (`regularMarketPrice`, its `regularMarketTime`, `previousClose`) for the
  benchmarks and the day's names only, read after that market's close
  (Shanghai 16:00, Hong Kong 17:00 — Yahoo's Asian quotes are delayed —,
  New York 16:30 local), only when the
  quote's own trade time is on the day at or after the regular close less
  10 minutes (a holiday, a suspension or an intraday price never passes),
  its previous close equals the series' last close before the day within
  0.2 % (no missed session), and no later bar exists; a listed close is
  never replaced. A shortened session (a US early close, a Hong Kong
  half-day) closes before that time, so its late close is not filled — the
  move stays null, never a wrong one;
- `core_analysed` per market: a market whose core is not analysed by
  configuration (llm-cost R6; cn today) lists its core names
  `core-session-off`, and the prompt tells the model this is by design, not
  a fault to report;
- per us/cn name of the day: brief impact word, catalyst score, eval verdict,
  trigger code, decision and PM rating, execute outcome, the day's close
  change and excess vs the benchmark (SPY / 510300.SS — the CSI 300 ETF: Yahoo serves the 000300.SS index
  as a single latest bar — / ^HSI);
- names without a decision that moved ≥ 5 % that day (missed moves);
- the macro brief's six Impact words and eval verdict;
- account equity change, fills, open positions (us);
- analysis errors by kind, entry-zone skips, other execute skips;
- every funnel entry of the session, a brief-less one too (a failed
  collection, a watch name), with its layer and collect code;
- analysis errors by kind (`llm_usage.error_kind`), with ERROR decisions
  that left no usage row (a run refused before any call, such as a 401)
  counted as `unrecorded`, and the day's fills;
- `price_reads`: how many price reads were skipped (budget spent) or
  failed, and how many closes were filled from the quote (`quote_filled`,
  named in `quote_filled_tickers`) — so an archived close from the quote is
  identifiable in `review.json` and, for an archive that fetches for itself,
  in `prices/<day>/quote_filled.json` (R1). A truncated read is a fact in the
  prompt and a warning line in the message, never a quiet null;
- a rolling scoreboard over the last 20 us sessions with a snapshot: the
  excess return over **1, 3 and 5 sessions** from the close before the
  pre-market decision (so it includes the overnight gap before the 09:45
  execution), by decision, brief impact word and PM rating, with counts; a
  horizon not yet closed, or whose start or end close is missing, is left out
  (only the two end closes are needed: a missing bar inside the span does not
  change the return between them). The
  prompt has the model judge patterns on 3d/5d — 1d is one session's tape
  (2026-11-09: a sector rally made Underweight look inverted on 1d while its
  3d/5d agreed) — and never propose a change from 1d against 3d/5d.

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
link or markup (any scheme, `www.`, a markdown link, an HTML/Lark tag, a
domain-like host other than a numeric exchange symbol — a plain comparison
such as `超额>2%` or `NVDA<SPY` is fine), and stays within its size bounds
(300 characters a field, at most 8 issues, 5 proposals, 6 watch items, in the
prompt and the check — not the schema, which codex sends in OpenAI strict form; a rendered message over 6000 characters is replaced by
the facts); a failure falls back to the facts. Every step but the send is guarded (facts, model, archive), so the
day's message is always queued.

The result is rendered as one Lark message to the trades group through the
trades outbox (key `review:<day>`, exactly once), and written to
`reviews/<day>/review.md` and `review.json`. Without the model (failure,
timeout) the facts alone are sent with `复盘模型不可用` — the day's data is
never lost.

### R3 — Schedule

The hourly watchdog (`_hourly_trade_notifications`, guarded) calls
`maybe_start`: once the New York clock is past 17:00 on a weekday whose us
funnel snapshot exists, it runs the review **inline** (its log is the watchdog's): the
price reads share a 240 s budget, read benchmarks first, then the day's
names, then the scoreboard window newest first (tickers left are skipped,
counted in `price_reads`) and the model call has its own 300 s timeout, inside the watchdog
task's 15-minute limit. It runs whether or not a trades webhook exists. Marks
`state_dir/daily_review/<date>.started|done`: a started run older than 2 h is
retried, a done day never re-runs, and a day left undone (the host off all
evening, the drive missing past midnight) is still picked up for 3 days
(Monday reaches Friday), oldest first; a weekday the broker's calendar calls
a market holiday is archived and marked done without a model call or a
message (no calendar: the weekday rule stands). The read-only broker is
built for the calendar; its account/positions/fills snapshot is taken only
when the day is today; the done mark is written once the message
is queued (or when no trades channel is configured — the review is then only
archived, with a warning). By hand, `python -m pipeline.daily_review run
--date D` loads the repo `.env` first, like every job.

## Prior art (specs/README.md rule 6)

- **freqtrade** — https://github.com/freqtrade/freqtrade @ `0789f5d5c3e7`,
  GPL-3.0 (design reference only, nothing copied): `/daily`, `/performance`
  and `/profit` Telegram reports summarise a bot's trades per day from its
  own trade database. Taken: one daily summary built from the system's own
  records, sent where the trades go. Avoided: profit-only reporting — this
  review also judges the research and the gates.
- **NautilusTrader** — https://github.com/nautechsystems/nautilus_trader @
  `df64cfe7b63f`, LGPL-3.0 (reference only): its portfolio analyzer computes
  return statistics from fills and positions after a run. Taken: the facts'
  numbers (moves, excess, scoreboard, account, fills) are computed from
  recorded fills/positions and prices, never by the model; the model's own
  prose figures are bound only by the prompt (cite the facts, invent nothing)
  — the user checks them against the archived `review.json` facts.
- **QuantConnect LEAN** — https://github.com/QuantConnect/Lean @
  `06eb5cb315c7`, Apache-2.0 (reference only, nothing copied): its result
  handler writes per-day statistics and orders into a results package next to
  the run. Taken: a by-date archive of every input next to the review, so a
  day can be re-examined later. Avoided: a single monolithic results blob —
  categories by date keep the archive browsable and re-archivable.
- **This repository's retry pattern** (trade outbox, research-digest pending
  marks): started/done marks with a stuck-retry age.

## Acceptance criteria

1. `archive_day` writes each category under `<root>/<category>/<day>/`,
   atomically and idempotently; missing inputs are skipped.
2. `build_facts` contains no brief prose, only structured fields and numbers.
3. One review message per day reaches the trades group; a model failure
   still sends the facts.
4. The review proposes, never applies, a strategy change.
5. The watchdog runs the review once per day after 17:00 ET, inline; a
   stuck run is retried after 2 h; a by-hand run loads `.env`.
6. A model call that used the web, a malformed or unsafe review, or any
   failure before the send still sends the facts; the archive never
   replaces a file with an empty one (a missing or empty source, a partial
   price refetch — closes are merged, never dropped).
7. Tests are offline (the broker is replaced in every test).
8. A retry never calls the model again for a queued day; a holiday sends
   nothing; a day undone at midnight is still reviewed within 3 days.

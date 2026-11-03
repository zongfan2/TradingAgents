# Spec: Trade notifications — a separate Lark trades channel, real-time fills

**Builder**: Claude Code · **Status**: Implemented (2026-11-03)

## Why

From 10-20 to 11-03 all 30 Lark messages were slot/execution status reports
("8 ok, 1 skipped…"). A trade appeared only as a detail line of the
execution summary (MU on 10-30: `• MU BUY 5 @940 … (live, pending_new)`),
and nothing at all was sent when MU **filled** ($893.92), nor would be on a
stop/target exit: the pipeline learns of fills only at the next slot's
refresh. User decision 2026-11-03: trades go to a **new Lark group with its
own bot**, fills are notified **in real time** by an always-on listener.

## Design

### T1 — Channels

- **ops** (unchanged): `TRADINGAGENTS_LARK_WEBHOOK_URL` / `_SECRET` — slot
  summaries, failures, watchdog, LLM cost.
- **trades** (new): `TRADINGAGENTS_LARK_TRADES_WEBHOOK_URL` / `_SECRET`,
  validated like the ops pair (https, no whitespace, secret needs a URL).
  Unset → trade notifications are off (nothing is sent anywhere; the ops
  channel is never flooded).

Both use the existing signed `LarkWebhook` (URL/secret scrubbed from every
error text, never logged).

### T2 — Events and messages (Chinese, one message per event)

| Broker event (order) | Message |
|---|---|
| `new`/`accepted` of an entry (`…-entry`) or signal close (`…-close-N`) | 📝 下单 — side, qty, limit, bracket stop/target; for an entry the executed run's conviction, catalyst score and invalidation (one line: `orders.jsonl` client_order_id → the run of its live `submitted` (else live `intent`) row, never skip or dry-run rows → `decisions.jsonl`; without that link only a brief-arm, valid BUY plan of the id's date/session/ticker) |
| `fill` | ✅ 成交 (entry) / 🔚 平仓 (exit: 止损 stop leg, 止盈 target leg — sell stop/limit orders with `order_class: bracket`, measured on the MU/SNDK legs; 信号平仓 close order; manual simple orders are not reported — a leg does not name its parent, so the legs of a bracket placed *by hand* would read as pipeline exits: the paper account is the pipeline's alone, and missing a real exit is worse) — the **order's totals** (filled qty, average price); exits add realized P&L $ and % on those totals vs the entry average |
| `canceled` / `expired` / `rejected` of an entry or close order | ⚠️ 撤单 / 过期 / 拒单 with the client order id and, when it partly filled, the filled qty and average (a bracket leg canceled because its sibling filled is not reported) |
| `partial_fill`, `replaced`, `pending_*`, `done_for_day`… | not reported — orders fill in several executions (measured 10-26..11-02: AAPL 4, SNDK 2, MU 2), the final `fill` reports the totals |

Exit P&L uses the entry average recorded when the entry filled — or ended
partly filled — (`trade_notify/entries.json`). The listener, at the moment
of the fill, takes it from the broker's position (adds average in exactly);
the fallback, replaying events that may be hours old, uses the orders' own
totals (today's position is not the position then) and afterwards only
drops symbols the broker no longer held in a positions snapshot taken
**before** the events were read (a stop filling during the sends never
drops its row before its own event is booked) — and never a row booked
after that snapshot (its `as_of` is later than the snapshot's broker time:
an entry the listener or the batch booked meanwhile) — never adds or resizes (every
fill in the window reaches the book as an event, so a position read would
count an in-flight or partial fill twice) — and only after a complete
batch.
Seeded on the first run from open positions; unknown → the message omits
P&L rather than guess. Messages leave through the T3 outbox, so an exit is
never reported before its entry; a queued message is dropped (error log;
its book change stands, the trade happened) only after 10 failed sends
**and** 2 hours since its first failure — a message that never goes
through cannot hold back every later one, yet a short outage during busy
trading loses nothing.

A placement (`new`) is never sent after a later event of the same order was
already reported (it would arrive out of order), and a rejected order has
no placement. The plan line reads the configured `ledger_dir`.

### T3 — Exactly-once delivery

A persistent store `<state_dir>/trade_notify/sent.jsonl` holds one key per
delivered message: `<order_id>:new`, `<order_id>:fill`,
`<order_id>:<canceled|expired|rejected>`; keys older than 45 days (past
both lookbacks) are pruned. A send that times out without
an answer is treated as not sent and retried, so in that one ambiguous case
a message can arrive twice (a duplicate is preferred over a missed trade).
Under an exclusive file lock shared by the listener and the fallback (T5),
each new event is booked at once and its message — P&L computed then, with
the right cost basis — appended to a persistent **outbox**
(`trade_notify/outbox.jsonl`) and its key recorded; the outbox is sent head
first. Writes go in a kill-safe order, each idempotent on a rerun: the
book with the events it booked and the entry average each was measured
against (`_applied`, reused on a rerun, so a kill never loses an exit's
P&L) → the outbox
(deduplicated by key) → the handled keys; each sent or dropped message is
removed from the outbox at once. A kill at any point can at worst resend
one message, never lose one, and never books an event twice. A failed send keeps the
message at the head, so later ones (same batch, the listener, the next
reconcile) queue behind it: nothing goes out of order and a retry resends
the same text. On the very first run —
decided under the store lock, so a concurrent listener and watchdog agree — (no
`trade_notify/initialized` marker) the broker history **before the first
start** (`trade_notify/first_started`, written once by whichever of the
listener or a reconcile runs first; a first-run reconcile uses its
positions snapshot's broker time for the cutoff and the seed stamp) is recorded **without sending** — old
events are never replayed into the new group — while later events are sent
as usual (a listener whose startup reconcile failed loses nothing); the
marker is written even when that history is empty. A seeded row keeps its
`seeded_at` through every rewrite.

### T4 — Listener (`python -m pipeline.trade_monitor`)

- alpaca-py `TradingStream` pinned to the paper stream
  (`wss://paper-api.alpaca.markets/stream`), REST reads pinned to the
  adapter's `PAPER_ENDPOINT` with the `PA…` account check; keys from
  `.env` as the adapter loads them, never logged; the SDK reconnects.
- **Read-only**: it never submits, replaces or cancels an order (amends
  specs/README.md rule 2: the execution adapter remains the only component
  that *acts* on the broker; trade notifications may *read* it).
- Single instance: an exclusive lock on `<state_dir>/locks/trade_monitor.lock`;
  a second start exits 0 at once.
- It runs the T5 reconcile at start and every 15 minutes, so events missed
  while it was down or disconnected are still sent; a failing reconcile is
  logged and never keeps the stream from starting.
- It honours `notifications_enabled` (off: not started), as the watchdog
  hook does.
- Scheduled as an always-on Windows task like the webui (re-fired every
  10 min, IgnoreNew), generated by `pipeline.install_schedule` when the
  trades webhook is set; registering it needs an elevated Windows shell.

### T5 — Fallback reconcile (hourly, in the watchdog)

`reconcile`: (a) entry/close orders submitted in the last 30 days (the
newest 500 if more) map to
the `new` / `canceled` / `expired` / `rejected` events their *current*
state implies; (b) fills come from the account's FILL activities by **fill
time** (last 3 days, `/v2/account/activities/FILL`, all pages) — an order
filter by submission time would miss the stop/target exit of a bracket
entered weeks earlier — one `fill` per order that reached `filled`, with
the order's totals. The two are merged into **one timeline** by event time
(the order's submitted/filled/canceled/expired/failed time) before
delivery, so the entry book changes in the order things happened. A
reconcile hole longer than the 3-day fill window (both listener and
watchdog down, e.g. the host off over a long weekend) may have hidden
exits: that batch carries no P&L and the book is rebuilt from the broker's
current positions (read first: a failed read changes nothing and the hole
is retried); the "last reconcile" time advances only on a complete batch.
A seeded row (first run or rebuild) is stamped `seeded_at`: a later
fallback fill of an order placed before that snapshot may already be inside
it, so it makes the symbol's cost basis **unknown** (no P&L) instead of
averaging in twice — a sell placed before the snapshot (a bracket leg) the
same, since a slice of it may already be out of the seeded quantity. Unknown is a state, not a deletion: later buys do not
restart the book as if they were the whole position; it ends when the
listener reads the broker's own position or the position is closed.

Every book row carries `as_of` — the time its figure is valid for, always
on the **broker's clock** (an event's broker time; a listener position read
is stamped with the broker clock read *after* the position read — the
event time is only a lower bound; a seed is stamped with the broker clock
after its read and remembers the clock before it: a fill inside that read
window may or may not be in the seeded figure, so it makes the symbol's
cost basis unknown), never the host's (WSL drifts after sleep): when the
broker clock cannot be read, the reconcile is skipped and retried. A
listener position read is a fresh seed in the same sense (`seeded_at` = the
broker clock after the read, `seed_from` = the event time): an order placed
whose execution started before it, or filled inside that window, makes the
cost basis unknown when the fallback replays it. "Started" is the order's
first execution from the fill activities (a bracket leg is *placed* with
its entry but executes much later, so its exit keeps its P&L); the
placement time only when no execution time is known. A fallback event
older than its symbol's `as_of` is already reflected in the row (the
listener moved the book ahead while this one was missed): it changes
nothing, and its exit carries no P&L. These age and seed-window rules apply
to fallback replays only — the listener reads the broker's own position,
the truth whatever the book's age (a sell keeps the average). A batch with
a fill whose order could not be read is incomplete: it neither aligns the
book nor closes the window, so the next reconcile replays it intact. A filled order's placement older than the fill window is not
announced (its fill is out of view, so it would read as a pending order).
Both go through T3, so anything the listener missed arrives within an hour. The reconcile and the daily report fail
independently. Best-effort:
a failure is logged and never fails the watchdog.

### T6 — Daily report (watchdog, once per US trading day after 16:15 ET)

📊 日报: account equity and the day's change (equity − last_equity, $ and
%), each open position (qty, average entry, last price, unrealized P&L $
and %), and the count of open orders (the broker's open-order listing). It goes
through the T3 outbox, so it never overtakes a queued trade message. Trading day per the broker calendar;
once per day (marker `trade_notify/daily-<date>.sent`, written once the
report is queued in the outbox, which then owns its delivery and ordering).

## Prior art (specs/README.md rule 6)

- **alpaca-py** — https://github.com/alpacahq/alpaca-py @ `e41650fd7085`,
  Apache-2.0, installed 0.43.5. `TradingStream.subscribe_trade_updates`
  (paper endpoint, auth, reconnect loop in `_run_forever`). Used as the
  dependency, nothing copied. Known gap: a websocket does not replay
  events sent while disconnected → T5 reconcile at start and hourly.
- **freqtrade** — https://github.com/freqtrade/freqtrade @ `2cebcc38974a`,
  GPL-3.0 (design reference only, nothing copied). Telegram notifications
  per event type (entry, entry_fill, exit, exit_fill, cancel) with a
  per-type on/off; taken: one message per order lifecycle event, fills
  distinct from submissions.
- **Lumibot** — https://github.com/Lumiwealth/lumibot @ `113c6c86aa6d`,
  GPL-3.0 (design reference only). Trade-event callbacks
  (`on_filled_order`, `on_canceled_order`) driven by the broker stream
  with polling as a fallback; taken: stream first, poll to reconcile.

## Acceptance criteria

1. Every T2 event produces exactly one message in the trades group (at
   most a repeat after an ambiguous send timeout, T3),
   whether seen by the listener, the fallback, or both; a failed send is
   retried by the next fallback; the first run replays nothing.
2. Exits carry realized P&L when the entry average is known, never a
   guessed one.
3. The listener and fallback never place, replace or cancel an order;
   paper endpoint only; no key or webhook value in any log.
4. Trades unset → no trade message anywhere; the ops channel is unchanged.
5. The daily report is sent once per trading day after 16:15 ET.

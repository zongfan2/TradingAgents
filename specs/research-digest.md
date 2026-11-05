# Spec: Research digest in the Lark trades group

**Builder**: Claude Code · **Status**: Implemented (2026-11-05)

## Why

The slots' research (the macro brief, the ticker briefs, their evals and the
analysis decisions) stays on the server: Lark gets only the one-line ops
summary ("8 ok, 1 skipped; plans 0/0 …"). The cn slot is research-only (no
execution, core analysis off) and its results were visible nowhere. User
decision 2026-11-05: send each slot's research, cn and us, to the **trades
group** — it is low-frequency (one message per slot, two a day).

## Design

### D1 — Content (v2, 2026-11-06)

The first version (per-ticker lines with English impact rationales cut at
110 characters, raw `HOLD（PM …）`, a bare `未评估` / `评估 warn` on every
line) was hard to read: the user asked why a name with no position was
"HOLD", why briefs were "未评估", and found the lines incomplete. v2:

```
📊 研究摘要 · 美股 · 11-05 ｜ 持仓：无 ｜ 计划下单：无
宏观（评估 warn）：6 个板块均为分歧
  要点：<one Chinese sentence>
已分析（2）
• HUM 8.0 财报 11-06 → 不买（PM 看淡；无持仓，不做空）
  <one Chinese sentence>
被拦下（1）
• LITE 9.0 财报 11-05 → 简报评估 fail，未分析
  <one Chinese sentence>
未触发（12）
• 近几日已分析且无新变化，不重复：SPCX 5.0 · MU 4.5 · …
• 机会池分数 <7，未评估：FN 4.0 · CHRW 2.0 · FTNT 2.0
```

- **Header**: positions from the trades book (`trade_notify/entries.json`;
  the segment is left out when there is no book) and the planned orders
  (us) or `A股不下单` (cn).
- **Macro**: the revision-bound eval verdict and each of sections 1–6's
  Impact direction (one phrase when all six agree), found by the contract's
  heading rule.
- **Tickers** — every ticker of the slot's own session (a wrong-session
  nominee is skipped: brief paths carry no session) with a brief for the
  slot date or a decision; a malformed row is skipped, never the digest —
  grouped by outcome:
  - **已分析**: the decision of the arm that trades (the brief arm's latest
    decide event when there is one; the stage keeps a paired ticker's
    strongest arm) in plain words. `HOLD` only means "no order":
    `不买` without a position (`；无持仓，不做空` when the PM view is
    Underweight/Sell — the account is long-only), `继续持有` with one;
    `BUY` → `买入`/`加仓（… 09:45 ET 下单）`; `SELL` → `卖出` with a
    position, `不买（…不做空）` without; a BUY/SELL that cannot become an
    order reads `看多/看空，不下单（A股不下单 | 对照分析 | 计划无效 |
    不满足下单条件）` — only an explicit `execute_eligible: false`, never
    an absent one. PM ratings in words: Buy 强烈看多 · Overweight 看多 ·
    Hold 中性 · Underweight 看淡 · Sell 看空. An analysis error reads
    `分析出错` and **never** carries the error text (API error bodies).
  - **被拦下**: trigger code `eval-fail` — the brief failed evaluation and
    was not analysed.
  - **未触发**: one line per trigger code (`core-unchanged`,
    `below-threshold` — not evaluated by the cost rule —,
    `core-session-off`, else `其他`) listing ticker and score.
  - Catalyst type and window in Chinese (`MM-DD`, ranges `~`); a window
    wholly before the slot date already happened and is dropped.
- **Chinese summaries**: one call for the whole digest on
  `DIGEST_SUMMARY_MODEL` (Haiku, effort low, ladder-routed like every
  pipeline call, logged as component `research_digest`; measured ≈ $0.002
  and 8 s), schema-bound JSON. Input: the macro brief's Impact rationales
  and watchlist, and for each analysed/blocked name its Impact line, the
  first two sections and — when analysed — the PM view in words with the
  plan's conviction and invalidation (never the internal HOLD/BUY/SELL).
  The prompt forbids new facts and the words 持有/减持/增持/评级/HOLD. Only
  summaries for the names asked are used. Any failure or a 45 s timeout:
  logged, and the digest goes out without them.

### D2 — Delivery

At the end of every **full** slot run (never a single-component `--only`
run, which would take the day's key with a partial slot), after the ops
end-of-slot summary, after the slot is recorded finished and after the slot
lock is released (a finished slot whose lock is held means an execute run to
`execute_slot`, so nothing waits while holding it), through
the trades outbox (`trade_notify.enqueue`, specs/trade-notifications.md T3)
under the key `research:<session>:<date>`: exactly once per slot day (a
re-run, `--from-step`, or the us execution reopen never sends it again), and
never ahead of a queued trade message. Off when notifications are disabled
or the trades webhook is unset. Imported lazily, guarded, and bounded
(90 s: the summary call plus the trades store lock, which has no timeout): a digest failure is logged and never changes the slot's outcome. The
slot end first marks the digest pending
(`trade_notify/research_pending/<session>_<date>`, a plain file, no lock);
queuing it clears the mark, and the hourly watchdog retries any mark still
there (one older than 3 days is dropped with an error). Once queued, a Lark
failure is the outbox's to retry; the hourly watchdog also flushes the outbox
without the broker (`trade_notify.flush_outbox`), so a queued digest never
waits for the broker to be healthy. The us digest goes
out at slot end (the 05:30 ET slot finishes around 07:00–07:30), so its BUY
decisions precede the 09:45 order messages.

## Prior art (specs/README.md rule 6)

No new survey was done, and none is claimed. The digest adds no external
integration and no new delivery mechanism. It is a formatter over files the
pipeline already writes, delivered through the trades outbox, whose prior
art (alpaca-py, freqtrade's per-event notifications, the transactional
outbox) is recorded in specs/trade-notifications.md. The one new piece, the
pending mark retried by the hourly watchdog, is that outbox's own pattern
applied one step earlier: the intent is persisted before the step that can
fail (here, getting the store lock) and a periodic job finishes it, with an
age bound so it cannot retry forever.

## Acceptance criteria

1. One digest per slot day in the trades group, cn and us, built from the
   slot's files; analysed names first.
2. Revision-bound macro verdict; ticker verdicts from the funnel's eval stage.
3. An analysis error never puts its error text in the message.
4. No trades webhook or notifications off → nothing sent anywhere; a failure
   never changes the slot's outcome.
5. Tests are offline.

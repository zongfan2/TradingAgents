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

### D1 — Content (Chinese labels; brief text quoted as written)

```
📊 研究摘要 · 美股 · 2026-11-04
宏观（评估 warn）：利率 偏空 · 增长 分歧 · 地缘 分歧 · 流动性 分歧 · 大宗 分歧 · 中国/亚洲 分歧
个股 15 只，分析 2 只：
• HUM 8.0 earnings 2026-11-06 · 中性 — <impact rationale>｜评估 warn｜分析 HOLD（PM Hold）
• LITE 8.0 earnings 2026-11-05 · 分歧 — <impact rationale>｜评估 fail
• FN 2.0 earnings 2026-11-02 · 偏多 — <impact rationale>｜未评估
```

- **Macro**: the revision-bound eval verdict of `<date>.<session>.md`
  (`未评估` without one) and the `**Impact**` word of each of sections 1–6
  (bullish 偏多 · bearish 偏空 · neutral 中性 · mixed 分歧).
- **Tickers**: every ticker of the slot's funnel snapshot that has a brief
  for the slot's date (written now, or already there when a slot re-fires)
  or a decision (a core ticker is analysed even when its collection failed:
  `（无简报）`) — catalyst score, type and window (`none`/`other` omitted),
  the final `**Impact**` line (rationale cut to 110 characters), the
  funnel's eval stage (`评估 pass|warn|fail`, else `未评估`), and the
  decision of the arm that trades: the decide stage keeps a paired ticker's
  strongest arm, so the brief arm's latest event is read when there is one
  (a feeds-only decision reads `feeds 对照 …`). `BUY|SELL|HOLD（PM rating）`;
  a BUY/SELL adds `，计划无效` when its plan is invalid and `，不下单` when it
  cannot become an order (cn, the feeds arm, or `execute_eligible` false — absent, as on
  a stage rebuilt from files, is unknown and says nothing); an analysis error
  reads `分析 出错` and **never** carries the error text (it holds API error
  bodies). Analysed names first, then by catalyst score.
- Macro sections are found by the contract's heading rule (a line starting
  `##` and whitespace), so a body without a blank line after the frontmatter
  loses nothing.
- Built only from files the slot already wrote — no model call, no cost. A
  missing or unreadable input gives a shorter digest (`宏观：无简报`,
  `个股：无简报`), never an exception.

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
(60 s: the trades store lock has no timeout, the slot lock must not wait on
it): a digest failure is logged and never changes the slot's outcome. The
slot end first marks the digest pending
(`trade_notify/research_pending/<session>_<date>`, a plain file, no lock);
queuing it clears the mark, and the hourly watchdog retries any mark still
there (one older than 3 days is dropped with an error). Once queued, a Lark
failure is the outbox's to retry. The us digest goes
out at slot end (05:30 ET), so its BUY decisions precede the 09:45 order
messages.

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

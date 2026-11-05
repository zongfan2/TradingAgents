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
个股简报 15 份，分析 2 只：
• HUM 8.0 earnings 2026-11-06 · 中性 — <impact rationale>｜评估 warn｜分析 HOLD（PM Hold）
• LITE 8.0 earnings 2026-11-05 · 分歧 — <impact rationale>｜评估 fail
• FN 2.0 earnings 2026-11-02 · 偏多 — <impact rationale>｜未评估
```

- **Macro**: the revision-bound eval verdict of `<date>.<session>.md`
  (`未评估` without one) and the `**Impact**` word of each of sections 1–6
  (bullish 偏多 · bearish 偏空 · neutral 中性 · mixed 分歧).
- **Tickers**: every ticker the slot's funnel snapshot shows as collected
  (`collect` code `written`) whose brief file exists — catalyst score, type
  and window (`none`/`other` omitted), the final `**Impact**` line (rationale
  cut to 110 characters), the funnel's eval stage (`评估 pass|warn|fail`,
  else `未评估`), and the decide stage when there is one: `BUY|SELL|HOLD（PM
  rating）`; on cn a BUY/SELL adds `，不下单` (cn is never executed); an
  analysis error reads `分析 出错` and **never** carries the error text (it
  holds API error bodies). Analysed names first, then by catalyst score.
- Built only from files the slot already wrote — no model call, no cost. A
  missing or unreadable input gives a shorter digest (`宏观：无简报`,
  `个股：无简报`), never an exception.

### D2 — Delivery

At the end of every slot, right after the ops end-of-slot summary, through
the trades outbox (`trade_notify.enqueue`, specs/trade-notifications.md T3)
under the key `research:<session>:<date>`: exactly once per slot day (a
re-run, `--from-step`, or the us execution reopen never sends it again), and
never ahead of a queued trade message. Off when notifications are disabled
or the trades webhook is unset. Imported lazily and guarded: a digest
failure is logged and never changes the slot's outcome. The us digest goes
out at slot end (05:30 ET), so its BUY decisions precede the 09:45 order
messages.

## Acceptance criteria

1. One digest per slot day in the trades group, cn and us, built from the
   slot's files; analysed names first.
2. Revision-bound macro verdict; ticker verdicts from the funnel's eval stage.
3. An analysis error never puts its error text in the message.
4. No trades webhook or notifications off → nothing sent anywhere; a failure
   never changes the slot's outcome.
5. Tests are offline.

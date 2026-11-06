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
  with no book, one that does not parse, or a row it cannot read,
  positions are **unknown** — the
  segment is left out and no line claims 无持仓) and the planned orders
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
    strongest arm) in plain words, and whether an order follows decided by
    the **execution adapter's own rules** on the ledger row of that run
    (`non_candidate_reason`, then `blocking_reason`; no ledger row → `下单待核实`,
    a feeds-arm decision → `对照分析`). `HOLD` only means "no order": `不买`
    without a position (`；无持仓，不做空` for a PM view of Underweight/Sell —
    the account is long-only), `继续持有` with one, `不操作（持仓未知）` when
    positions are unknown. `BUY` → `买入（… 09:45 ET 下单）` without a
    position; on a held name `继续持有（不加仓）` unless the plan sets
    `add_intent` (`可能加仓（按加仓规则）` — the adapter's add-on gates still
    apply); `看多（持仓未知，是否下单待定）` when positions are unknown. `SELL`
    → `卖出` when held, `不买（…不做空）` when not, `看空（持仓未知，是否下单待定）`
    when unknown. A blocked BUY/SELL reads `看多/看空，不下单（<adapter reason>）` (also a
    blocked SELL on a flat name);
    cn always `A股不下单` (checked first: no unknown execution state applies to cn). Execution mode at build time: the kill switch
    (`EXECUTION_HALT`) → `执行已暂停`, `TRADINGAGENTS_EXECUTION_ENABLED` off →
    `模拟`, in the header and in place of `下单`; neither is listed as a
    planned live order. With positions unknown a BUY/SELL is **undetermined**
    (the adapter reads live broker state, so it may still order): the header
    lists it as `待核实：X`, never `计划下单：无`. A digest built after
    step 7 (the funnel snapshot's `execute_ran`: a late retry, an inline
    execution) makes no pre-order claim and tells each name's own adapter
    outcome from its funnel entry: `final` `order-live` → `已下单，见交易消息`,
    `order-dry` → `模拟单`, otherwise `未下单：<execute-stage reason in
    Chinese>` (an unmapped code reads plain `未下单`, never the raw code); HOLD →
    `不操作`; the header reads `已执行：X 已下单` (or `无下单`). A blocked SELL is
    `看空，不下单（reason）` even with positions unknown (the adapter rejects
    the row whatever the position). Like the adapter, only the latest
    attempt per (ticker, arm) of the slot counts: a later (manual) attempt
    shadows the scheduled row → `被手动重跑取代`. At or past the slot day's
    execution time (New York) with step 7 not run (a late manual slot, a
    slot that overran — the execute job fires once then and skips an
    unfinished slot) the mode is `已过执行时间，下单待核实` and BUY/SELL read
    `看多/看空（已过执行时间，下单待核实）`, undetermined: no timed order is
    promised, none is denied. **Unread inputs are never "no order"**: a
    missing or unreadable funnel snapshot, a funnel row that had to be
    skipped, an unreadable ledger, or a missing ledger while decisions exist
    makes the header `待核实（决策数据未读到）`; with step 7 inline (no
    execution time) the digest is built after it, so a step 7 that left no
    outcome reads `下单结果待核实`, never a coming order; a decision with no ledger row read reads
    `看多/看空（下单待核实）`; after execution a `final` other than
    `order-live`/`order-dry`/`excluded` (e.g. a submit killed after its
    intent row) reads `下单结果待核实`; an `order-live` name whose latest
    decision is HOLD/ERROR reads `已下单，见交易消息（最新分析：…）`; a feeds-arm
    decide stage while the ledger has the brief arm's row (its event was
    lost) reads `下单待核实（简报一路的记录缺失）` whatever the feeds arm decided —
    after step 7 `下单结果待核实（简报一路的记录缺失）` unless the name has a live
    order on record —
    (under 模拟 / 执行已暂停 `不下单（mode）`: no live order can follow);
    the greatest later ledger attempt (the decision step 7 reads, unseen by
    this digest) — judged by the adapter's own rules: manual →
    `被手动重跑取代`, not a candidate or blocked → that reason, else open — reads `下单待核实（有更新的分析记录）` whatever the
    shown event decided (BUY/SELL/HOLD/ERROR), only a manual one
    `被手动重跑取代`, and under 模拟 / 执行已暂停 `不下单（mode）`; on cn neither
    suggests an order (A股不下单); only a scheduled brief row counts as a
    lost brief-arm event (manual runs emit no events); an unreadable execution switch or kill
    switch (`执行状态未知`) leaves BUY/SELL undetermined too; after execution
    the header keeps `已执行：` and appends `待核实（决策数据未读到）` when an
    input was unread. Undetermined names are listed in the
    header as `待核实：X` — including any scheduled BUY/SELL ledger row of the
    slot that the (valid) snapshot does not show (an older snapshot kept when
    a rebuild failed). After execution the reason is the brief arm's own
    outcome event (the stage may hold the feeds arm's; info events such as
    an older attempt's `superseded` note never count; the shown run's event
    first); a brief-arm
    decision whose execute stage holds only the feeds arm's outcome reads
    `下单结果待核实` (the brief arm's outcome is unknown). A brief-arm run whose ticker or macro evaluation failed adds
    `；简报未采用（评估 fail）` (the brief was withheld). PM ratings in words: Buy 强烈看多 · Overweight 看多 ·
    Hold 中性 · Underweight 看淡 · Sell 看空. An analysis error reads
    `分析出错` and **never** carries the error text (API error bodies).
  - **被拦下**: trigger code `eval-fail` — the brief failed evaluation and
    was not analysed.
  - **已触发未分析**: a ticker that met the trigger (or whose trigger stage
    was reconstructed with no code) but has no decision — its own group,
    never under 未触发: `budget-dropped` → `因预算上限未分析`, any other passed
    trigger → `无分析结果（运行失败或被中止）`, no code → `无分析结果（原因未记录）`.
  - **未触发**: one line per trigger code (`core-unchanged`,
    `below-threshold` — not evaluated by the cost rule; the label shows the
    snapshot's trigger threshold in force —,
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
  The prompt frames the brief text as data and forbids new facts and the
  words 持有/减持/增持/评级/HOLD. Only names with source text the decision
  actually used are asked: no brief, an analysis error, or an analysed run
  that did not read the brief (a feeds run, a withheld eval-fail brief, a
  macro-fail feeds-only day) get no summary. Each returned summary is
  screened: dropped if it contains a forbidden or action word, simplified or
  traditional (持有/减持/增持/评级/买入/买进/卖出/抛售/加仓/增仓/减仓/建仓/清仓/平仓/做空/做多/
  下单/止损/止盈/HOLD/BUY/SELL), exceeds 60 characters, or holds a whole number its source
  (links removed; leading zeros ignored; a comma-grouped number such as
  1,200 is one number; an English month name — a whole word —
  vouches only for a number written as a month, so "Nov 6" vouches for
  11月6日 but not for 下跌11%) does not, or contains — case-insensitively — a link or any domain-like token
  (whole multi-label hosts; only a numeric exchange symbol such as
  `0700.HK` standing alone is excepted), markup (a Lark `<at>`
  mention) or a figure in Chinese numerals — the prompt asks for Arabic digits
  (checked against the source); the numeral screen is a best-effort backstop:
  a numeral (everyday or formal/traditional) next to another, or before a
  measure/currency/unit (元/金/币/镑/分/角/块/毛/点/倍/成/亿/万/%/股, 百分点,
  基点, 卢布, 法郎, 比特币, 美分), after removing common non-numeric words
  (一季度, 一致, 进一步 …). A fail-closed "any numeral" rule was tried and
  dropped: on real output it removed most summaries (两日 = two days); only the names asked are used. A call that used
  a web tool is discarded — by its tool-call fields or a billed search count
  (the CLI keeps web tools on; outside facts would pass every screen).
  Any failure or a 45 s timeout: logged, and the digest goes out without
  them. A re-run whose digest key is already queued or sent builds nothing
  (no model call) and only flushes the outbox.

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
integration and no new delivery mechanism: it renders files the pipeline
already writes, reuses the execution adapter's own candidate and gate rules
(never a second copy), and makes one call through the existing hermetic CLI
path (`claude_command`, the provider ladder and usage logging the evaluator
already uses). Delivery is the trades outbox, whose prior art (alpaca-py,
freqtrade's per-event notifications, the transactional outbox) is recorded
in specs/trade-notifications.md. The pending mark retried by the hourly
watchdog is that outbox's own pattern applied one step earlier: the intent
is persisted before the step that can fail (getting the store lock) and a
periodic job finishes it, with an age bound so it cannot retry forever.

## Acceptance criteria

1. One Chinese digest per slot day in the trades group, cn and us, grouped
   as 已分析 / 被拦下 / 已触发未分析 / 未触发 with the reason per group.
2. An action word never claims an order the execution adapter would not
   place (its own candidate filter and gate, `add_intent` for add-ons, the
   execution switch and kill switch), and never claims a position state
   that was not read (unknown ≠ 无持仓).
3. Revision-bound macro verdict; blocked = trigger `eval-fail`.
4. An analysis error never puts its error text in the message.
5. The summary call is one small schema-bound call; its output is screened;
   its failure costs only the summary lines; a re-run makes no call.
6. No trades webhook or notifications off → nothing sent anywhere; a failure
   never changes the slot's outcome.
7. Tests are offline (the summary call is stubbed by an autouse fixture).

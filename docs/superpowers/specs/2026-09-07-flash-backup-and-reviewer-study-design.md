# Amendment: DeepSeek Flash as the Common Backup

- **Date:** 2026-09-07
- **Owner:** Codex
- **Status:** Latest routing intent approved in chat; written amendment awaiting review. No production activation.
- **Governing base:** [Reviewer fallback and DeepSeek study design](2026-09-07-reviewer-fallback-and-deepseek-study-design.md), introduced by `bfac666608c6656fdcab14f1962be134608c3ba6`.
- **Latest user direction:** All model-assisted purposes use `deepseek-v4-flash` as their backup provider; preserve existing backends and backups against future Claude subscription cancellation.

## 1. Precedence and scope

This amendment takes precedence over the base design only for backup selection
and the explicit clarifications below. The base design's dataset, $5 cap,
64-call pilot, frozen evidence, no-retry rules, safety gates and staged rollout
remain authoritative. Do not launch a second pilot or spend another $5 under
this amendment. There is one approved campaign, with a shared budget.

The base design arrived in the shared repository while this amendment was
being prepared. Preserve that committed work; this supplement replaces a
parallel draft, not the existing design or any historical evidence.

The user has approved the experiment and universal Flash backup direction.
Written-design review is the next workflow gate, not a request for a new
spending authorization. This document does not switch production backends,
cancel a subscription, change schedules or waive the current Claude gate.

## 2. Universal backup selection

| Purpose | Current or proposed primary | Specified backup | Qualification needed |
|---|---|---|---|
| Macro, pool and ticker collection | Current Codex CLI | DeepSeek V4 Flash | Search/fetch executor, collector contracts, dates and completeness |
| Macro and ticker evaluation | Current Claude; Terra already selectable | DeepSeek V4 Flash | Independent search, citation fetching, freshness and factual checks |
| Analysts, researchers, Trader and risk roles | Current Flash; Pro for RM/PM | DeepSeek V4 Flash | Role prompts, tools and decision/output contracts |
| Routine development review | Proposed Terra; current formal runner Claude-only | DeepSeek V4 Flash | Frozen evidence then exact-SHA isolated review |
| High-risk/final review | Current Claude until policy migration | DeepSeek V4 Flash | Qualified independent review and approved provider-neutral policy/report contract |
| Experimental scoring | Deterministic gold checks; model judge only where needed | DeepSeek V4 Flash | Blind labels, known-answer calibration and explicit same-family limitations |

DeepSeek Pro remains a pilot control/candidate primary, not the backup default.
Flash is designated for every purpose but becomes eligible independently for
each. A successful brief screen does not qualify it for code review or
collection. A failed qualification leaves that role blocked/degraded rather
than silently selecting Pro or treating Flash as approved.

A role already using Flash has no distinct Flash fallback; do not retry itself
under a different alias. Pro-to-Flash changes model, not provider/account, and
does not recover a DeepSeek-wide outage, missing API key or shared balance
failure. All-provider outage still preserves honest missing/unavailable states.

Raw DeepSeek API access does not provide the CLI's search/fetch execution
capability. Collection failover needs its own validated implementation. The
closed-book pilot remains an evaluation of judgment, not retrieval parity.
Deterministic formatting, Lark publishing and scheduling stay deterministic;
they do not acquire an unnecessary model dependency.

## 3. Cancellation behavior and safety

Keep Claude, GPT/Codex and DeepSeek implementations and recoverable presets.
Disabling Claude does not delete its CLI, authentication state or historical
reports. The future cancellation profile must skip it before startup,
fallback, health probes and sibling ticker jobs; zero Claude subprocess calls
is an acceptance requirement, not just a disabled-looking UI setting.

Flash fallback occurs at most once for classified operational unavailability,
within the remaining logical-job deadline and budget. It is never a means of
replacing a `warn`, `fail`, schema-invalid result or insufficient evidence
with a preferred verdict. Retain both attempts and actual provider identity.

Existing revision, independence and cache rules in the base design still
apply. Same-provider judgment is labeled as correlated; Flash must not grade
Flash/Pro as its own independent gold standard. The cancellation-safe profile
requires `execute_on_missing_eval=false` and refuses an incompatible profile
without silently rewriting the user's existing setting.

Changing the review policy cannot waive its own required review. Its migration
must pass the current gate or have an explicit exact-revision user waiver;
only subsequent reviews can use the activated new policy. Until then, Flash
results cannot close a mandatory Claude gate or be named `<sha>.claude.json`.

Cancellation readiness requires the base design's fault-injection, retrieval,
independent review, recovery and qualifying CN/US automatic-slot evidence.
This amendment and the 64-call pilot do not establish that readiness. Nothing
here guarantees fresh research if both subscription and API backends fail.

## 4. Existing recovery baseline

The original baseline remains preserved:

- Ref: `codex/backup-before-reviewer-routing-2026-09-07`.
- SHA: `8e7fb0cbd1326fb7e75fe7be9f4a0e951f937bf0`.
- Bundle: `/Users/zongfan/.tradingagents/verification/backups/20260907-reviewer-routing-s9AwiU/repository.bundle`.
- SHA-256: `e7156cde589607674b1a83caacbc99dbbcfd8d470618ffd3dde34fa22eb5785e`.
- Verification: `git bundle verify` succeeded; a no-checkout clone in a separate
  owner-only directory resolved to the same SHA and passed `git fsck --full`.

This is a local tracked-source/history backup, not an off-device backup or
a copy of untracked files, `.env`, runtime ledgers, authentication state or
subscription access. Its baseline still lacks an accepted final Claude report.
A later implementation must snapshot the exact configuration files it changes
in restricted local storage, as specified in the base design. Do not restore
over new ledgers or roll back protective trading fixes.

The unrelated new base-design commit is preserved in Git and is not claimed
to be inside this earlier bundle. No remote push or upload has been performed.

## 5. Next deliverable

After written-design review, create the pilot implementation plan against
both documents and run the already authorized single $5 campaign. Use Flash
and Pro as specified by the base design, with Flash as the explicitly intended
backup candidate. Follow-up production plans cover reviewer routing first and
runtime collection/evaluation separately; each must retain the existing backend
and pass its own qualification before activation.

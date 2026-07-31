# Specs Index

Cross-agent specifications for the macro-brief subsystem. Any agent (Claude
Code, Codex) picking up a task reads the **data contract first**, then its
component spec. The contract is the only shared interface — components never
depend on each other's internals.

| Spec | Component | Builder | Status |
|---|---|---|---|
| [macro-brief-data-contract.md](macro-brief-data-contract.md) | Shared file/schema contract | — (change requires touching all consumers) | **v1 frozen** |
| [macro-brief-pipeline.md](macro-brief-pipeline.md) | TradingAgents consumption side (`get_macro_brief` tool, `--macro-source` A/B switch) | Claude Code | **Implemented** |
| [macro-brief-collector.md](macro-brief-collector.md) | Daily scheduled deep-search job writing briefs | Codex | Open |
| [macro-brief-evaluator.md](macro-brief-evaluator.md) | GPT-5.6 Terra accuracy scoring of briefs | Codex | Open |

## System overview

```
[daily schedule]                      [analysis time]
collector (deep search, Claude/Codex) ──▶ ~/.tradingagents/macro_briefs/YYYY-MM-DD.md
        │                                          │
        ▼                                          ▼
evaluator (gpt-5.6-terra) ──▶ YYYY-MM-DD.eval.json │
                                                   ▼
                              TradingAgents news analyst (--macro-source brief)
                              vs fixed feeds        (--macro-source feeds)  ← A/B
```

## Rules

1. **Contract first**: a change to file location, naming, frontmatter, or section
   structure lands in the contract spec before any component changes.
2. Components communicate **only through files named in the contract** — the
   collector never reads pipeline state; the pipeline never writes to the brief
   directory; the evaluator writes only `*.eval.json`.
3. Each spec lists acceptance criteria; a component is done when they all pass.

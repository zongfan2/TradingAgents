"""Machine-readable contract models (specs/README.md rule 5).

One module per contract spec:

- :mod:`pipeline.contracts.briefs` — macro/ticker brief frontmatter + structure
- :mod:`pipeline.contracts.evals`  — ``*.eval.json`` reports + verdict rule
- :mod:`pipeline.contracts.pool`   — tiered pool files + reading rule
- :mod:`pipeline.contracts.ledger` — TradePlan validator + jsonl records

Writers validate strictly (unknown fields rejected); readers use each model's
``parse_lenient`` (unknown fields warned about and dropped).
"""

from pipeline.contracts.base import ContractError, ContractModel

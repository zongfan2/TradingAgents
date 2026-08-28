# P0 Runtime Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate evaluator schema/quota misclassification, give collectors useful first-attempt time budgets without losing their aggregate deadline, and complete analysis slots within the existing outer budget by running two ticker jobs concurrently.

**Architecture:** Claude evaluation uses the CLI's native structured-output envelope and converts only `structured_output` into the existing `_ModelJudgment` path; CLI/quota/envelope failures remain backend failures and never enter schema retry. Collectors retain one retry but derive every real subprocess timeout from an absolute deadline, while ticker jobs also receive a fair per-wave window. Analysis parallelizes at the `RunJob` boundary, keeps each job's arms sequential, writes every ledger row through the existing flock-protected append, and assembles the final summary in planned-job order.

**Tech Stack:** Python 3.10+, Pydantic v2, `subprocess`, `concurrent.futures.ThreadPoolExecutor`, pytest, Ruff.

**Spec:** `specs/macro-brief-evaluator.md`, `specs/macro-brief-collector.md`, `specs/ticker-brief-collector.md`, `specs/analysis-runner.md`, `specs/orchestrator.md`

## Global Constraints

- Tests are offline; no Claude, Codex, OpenAI, or DeepSeek process may be invoked by the test suite.
- Collector and evaluator backends remain different by default: collect=`codex`, eval=`claude` (D19).
- Analysis A/B selection, total run count, provider/model settings, and per-child timeout remain unchanged.
- The existing orchestrator component budgets remain hard outer limits: macro collector 1800s, ticker fan-out 2700s, macro evaluator 1200s, analysis runner 7200s by default.
- A paired ticker's `brief` arm must finish before its `feeds` arm starts; separate ticker jobs may overlap.
- Ledger rows continue to be written immediately through `mint_and_append_decision`, whose flock owns run-id and pair-id allocation.
- Runtime artifacts remain under `~/.tradingagents/`; no live artifacts enter the repository.

---

### Task 1: Record the approved runtime policy in the source-of-truth specs

**Files:**
- Modify: `specs/macro-brief-evaluator.md`
- Modify: `specs/macro-brief-collector.md`
- Modify: `specs/ticker-brief-collector.md`
- Modify: `specs/analysis-runner.md`
- Modify: `specs/orchestrator.md`
- Modify: `RUNBOOK.md`

**Interfaces:**
- Consumes: the approved evaluator, deadline, and job-concurrency design.
- Produces: normative behavior against which Tasks 2–4 are reviewed.

- [ ] **Step 1: Update evaluator R2 and failure semantics**

  State that the Claude backend invokes `claude -p --output-format json --json-schema <schema>`, consumes only the `structured_output` object, and classifies non-zero exit, `is_error`, malformed envelope, or missing structured output as backend failure. Schema retry remains only for an actual `_ModelJudgment` contract failure.

- [ ] **Step 2: Update collector timeout semantics**

  State that macro collection gives the first attempt the component budget minus headroom and lets retry consume only the remaining absolute deadline. State that ticker collection computes `(component_budget - headroom) / ceil(jobs / concurrency)` without dividing by a hypothetical retry; each job's retry consumes only that job window's remainder, capped by the aggregate deadline.

- [ ] **Step 3: Update analysis concurrency semantics**

  Add `analysis_job_concurrency`, default 2, env `TRADINGAGENTS_ANALYSIS_JOB_CONCURRENCY`. Define jobs as the concurrency unit, arms as sequential inside a job, immediate flock-protected ledger writes, parent-thread emits, and planned-order final-summary assembly.

- [ ] **Step 4: Update operational guidance**

  Document that concurrency changes peak analysis load from one to two children but does not change selected runs or theoretical total model spend. Preserve the rule that no catch-up slot is run without explicit authorization.

- [ ] **Step 5: Commit the contract update**

  Run: `git diff --check`

  Expected: exit 0.

  Commit: `docs(pipeline): define P0 runtime reliability policy`

### Task 2: Make Claude evaluator output natively structured

**Files:**
- Modify: `tests/test_evaluator.py`
- Modify: `pipeline/evaluator.py`

**Interfaces:**
- Consumes: `_ModelJudgment.model_json_schema()` and the Claude JSON result envelope.
- Produces: `claude_runner(prompt: str) -> str`, returning a JSON serialization of the envelope's `structured_output`; `_run_judgment` stays backend-neutral.

- [ ] **Step 1: Write failing command/envelope test**

  Replace the raw-output fixture with a complete Claude envelope:

  ```python
  envelope = {
      "type": "result",
      "subtype": "success",
      "is_error": False,
      "result": "",
      "structured_output": json.loads(judgment()),
  }
  ```

  Assert the command contains `--output-format`, `json`, `--json-schema`, and that the schema argument describes required `scores`. Assert `json.loads(claude_runner(...)) == envelope["structured_output"]`.

- [ ] **Step 2: Run the command/envelope test and observe RED**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_evaluator.py::test_claude_runner_command_construction_and_stdin -q`

  Expected: FAIL because the command lacks structured-output flags and raw envelope text is returned.

- [ ] **Step 3: Write failing backend-envelope tests**

  Add table-driven cases for `is_error: true`, invalid JSON, and missing `structured_output`. Each calls `claude_runner` and expects `BackendError`; the quota case's message must include a one-line version of the envelope `result`.

- [ ] **Step 4: Run backend-envelope tests and observe RED**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_evaluator.py -q -k 'claude_runner'`

  Expected: FAIL because current code returns every exit-0 stdout string.

- [ ] **Step 5: Implement the minimal structured-envelope parser**

  Build the Claude command as:

  ```python
  schema = json.dumps(_ModelJudgment.model_json_schema(), separators=(",", ":"))
  command = [
      "claude", "-p", "--allowedTools", "WebSearch,WebFetch",
      "--output-format", "json", "--json-schema", schema,
  ]
  ```

  After the existing subprocess checks, `json.loads(proc.stdout)`, require a mapping with `is_error is not True` and a mapping-valued `structured_output`, then return `json.dumps(structured_output)`. Raise `BackendError` for every envelope failure; do not catch it in `_run_judgment`.

- [ ] **Step 6: Adapt default-backend test fixtures and run evaluator tests**

  Claude command fakes return the full envelope; Codex command fakes continue returning raw judgment JSON.

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_evaluator.py -q`

  Expected: all evaluator tests pass.

- [ ] **Step 7: Commit evaluator change**

  Run: `git diff --check && /Users/zongfan/Projects/TradingAgents/.venv/bin/python -m ruff check pipeline/evaluator.py tests/test_evaluator.py`

  Expected: both commands exit 0.

  Commit: `fix(evaluator): require Claude structured output`

### Task 3: Replace speculative retry splitting with absolute collector deadlines

**Files:**
- Modify: `tests/test_macro_collector.py`
- Modify: `tests/test_ticker_collector.py`
- Modify: `pipeline/macro_collector.py`
- Modify: `pipeline/ticker_collector.py`

**Interfaces:**
- Consumes: orchestrator `component_timeouts`, collector headroom constants, and existing two-attempt validators.
- Produces: `default_runner(..., deadline: float | None = None)` in both collectors; ticker `fanout_backend_timeout(...)` becomes a per-job-window calculation that does not reserve an unused retry.

- [ ] **Step 1: Write failing macro deadline tests**

  Assert the default macro first-attempt ceiling is `1800 - 120 == 1680`, and with a fixed absolute deadline two calls at monotonic times 100 and 400 receive timeouts 600 and 300 respectively. Assert an exhausted deadline raises `CollectorError` before `subprocess.run`.

- [ ] **Step 2: Run macro deadline tests and observe RED**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_macro_collector.py -q -k 'timeout or deadline'`

  Expected: FAIL because the current calculation divides by `BACKEND_ATTEMPTS` and has no deadline parameter.

- [ ] **Step 3: Implement macro shared deadline**

  Change `backend_timeout_seconds()` to `max(1.0, component_budget - HEADROOM_SECONDS)`. Add optional keyword-only `deadline`; before each subprocess call use `min(timeout_ceiling, deadline - time.monotonic())`, failing immediately when the remainder is non-positive. In `collect`, create one deadline and pass `functools.partial(default_runner, deadline=deadline)` only when no runner was injected.

- [ ] **Step 4: Run macro collector tests GREEN**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_macro_collector.py -q`

  Expected: all macro collector tests pass.

- [ ] **Step 5: Write failing ticker window/deadline tests**

  Hand-check these literal expectations:

  ```python
  assert fanout_backend_timeout(2, 3, 2700.0) == 1800.0  # ceiling
  assert fanout_backend_timeout(15, 3, 2700.0) == 516.0  # 2580 / 5 waves
  assert fanout_backend_timeout(4, 1, 2700.0) == 645.0   # 2580 / 4 waves
  ```

  Add a production-path fake which captures a single job's `deadline` and `timeout` arguments, plus a retry test proving the second attempt receives only its job-window remainder.

- [ ] **Step 6: Run ticker deadline tests and observe RED**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_ticker_collector.py -q -k 'timeout or deadline'`

  Expected: FAIL because current fan-out math divides by two attempts and reuses a fixed timeout on retry.

- [ ] **Step 7: Implement ticker aggregate and job deadlines**

  At fan-out start compute `aggregate_deadline = time.monotonic() + component_budget - FANOUT_HEADROOM_SECONDS` and `job_window = fanout_backend_timeout(...)`. In each worker, after the idempotent skip check, set `job_deadline = min(aggregate_deadline, time.monotonic() + job_window)` and use a partial default runner carrying both the window ceiling and deadline. Keep injected runner signatures unchanged.

- [ ] **Step 8: Run collector tests GREEN and commit**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_macro_collector.py tests/test_ticker_collector.py -q`

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m ruff check pipeline/macro_collector.py pipeline/ticker_collector.py tests/test_macro_collector.py tests/test_ticker_collector.py`

  Expected: both commands exit 0.

  Commit: `fix(collectors): allocate retries from shared deadlines`

### Task 4: Run analysis ticker jobs concurrently while preserving pair order

**Files:**
- Modify: `tests/test_analysis_runner.py`
- Modify: `pipeline/analysis_runner.py`

**Interfaces:**
- Consumes: `RunJob`, `ChildRunner`, flock-protected `mint_and_append_decision`, and locked runner log append.
- Produces: `RunnerSettings.analysis_job_concurrency: int = 2`; private `_run_job(...) -> JobOutcome`; `run_slot` schedules jobs with `ThreadPoolExecutor` and assembles output by original index.

- [ ] **Step 1: Write failing settings tests**

  Assert the default is 2, `TRADINGAGENTS_ANALYSIS_JOB_CONCURRENCY=3` overrides it, and zero raises `ValueError` matching `analysis_job_concurrency`.

- [ ] **Step 2: Run settings test and observe RED**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_analysis_runner.py::test_load_settings_env_overrides_and_defaults -q`

  Expected: FAIL because the setting and env constant do not exist.

- [ ] **Step 3: Implement the setting and snapshot field**

  Add `ANALYSIS_JOB_CONCURRENCY_ENV`, the dataclass field/default/validation, env loading, and `analysis_job_concurrency` in `write_config_digest`'s snapshot.

- [ ] **Step 4: Write failing real-concurrency test**

  Use two core ticker jobs and a `threading.Barrier(2)` inside a child fake so a sequential implementation cannot complete. Record the active child count under a lock and assert peak activity is 2. For paired jobs, record per-ticker arms and assert each ticker sees exactly `["brief", "feeds"]`. Assert returned `run_ids` remain in planned ticker/arm order even when the second job finishes first.

- [ ] **Step 5: Run concurrency test and observe RED**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_analysis_runner.py -q -k 'job_concurrency'`

  Expected: FAIL or barrier timeout because current `run_slot` is sequential.

- [ ] **Step 6: Extract one-job execution without changing its behavior**

  Move the current outer-loop body into `_run_job`. Keep both arm iterations in that function, reuse one pair-id variable, append each ledger row immediately, and return per-job records and run lines. Do not create any nested arm executor.

- [ ] **Step 7: Add parent-thread job scheduling**

  Submit indexed jobs to `ThreadPoolExecutor(max_workers=min(settings.analysis_job_concurrency, len(jobs)))`. Consume futures with `as_completed`, store each result at its original index, and call `emit` from the parent thread for that completed job. Flatten stored outcomes in index order for deterministic `records`, `run_ids`, and aggregate counts.

- [ ] **Step 8: Run analysis runner tests GREEN**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_analysis_runner.py -q`

  Expected: all analysis runner tests pass, including existing pair-id flock and back-to-back pair coverage.

- [ ] **Step 9: Commit analysis concurrency**

  Run: `git diff --check && /Users/zongfan/Projects/TradingAgents/.venv/bin/python -m ruff check pipeline/analysis_runner.py tests/test_analysis_runner.py`

  Expected: both commands exit 0.

  Commit: `fix(analysis): run ticker jobs concurrently`

### Task 5: Run repository and offline runtime gates

**Files:**
- Verify: all changed files and repository tests.

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: evidence suitable for review and a stacked PR based on `codex/hybrid-verification`.

- [ ] **Step 1: Run focused regression group**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m pytest tests/test_evaluator.py tests/test_macro_collector.py tests/test_ticker_collector.py tests/test_analysis_runner.py tests/test_orchestrator.py -q`

  Expected: all focused tests pass without network or paid backend calls.

- [ ] **Step 2: Run complete hybrid offline gate**

  Run: `/Users/zongfan/Projects/TradingAgents/.venv/bin/python -m devtools.verification.offline --only all`

  Expected: pytest, Ruff, and `git diff --check` all exit 0.

- [ ] **Step 3: Inspect the final diff against the approved design**

  Confirm no provider/model/A-B selection/outer-timeout changes, no runtime data, and no secrets. Confirm every new production branch has a regression test that failed before its implementation.

- [ ] **Step 4: Commit any final documentation-only alignment**

  Commit: `docs(runbook): explain runtime reliability controls`

- [ ] **Step 5: Push and open a stacked PR only after review**

  Push `codex/p0-runtime-reliability`; target `codex/hybrid-verification` while PR #2 remains open. Do not merge either PR and do not run a live slot without explicit user authorization.

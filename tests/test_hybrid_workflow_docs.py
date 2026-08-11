from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_ci_uses_offline_verifier_for_tests_and_lint():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python -m devtools.verification.offline --only tests" in workflow
    assert "python -m devtools.verification.offline --only lint" in workflow
    assert "clean-install smoke" in workflow
    assert "pip install ." in workflow
    assert "import tradingagents, cli.main" in workflow
    assert "claude" not in workflow.lower()
    assert "subscription" not in workflow.lower()


@pytest.mark.unit
def test_agent_guide_declares_hybrid_roles_and_required_gate():
    guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Codex — primary builder" in guide
    assert "Claude Code — independent verifier" in guide
    assert "python -m devtools.verification.offline --only all" in guide
    assert "two or more" in guide
    assert "pipeline/contracts/" in guide
    assert "~/.tradingagents/verification/" in guide
    assert "returns reports for Codex to assess" in guide
    assert "or patches" not in guide
    assert (
        "isolated worktree and returns reports for Codex to assess; it does not edit "
        "the primary worktree" in " ".join(guide.split())
    )


@pytest.mark.unit
def test_agent_guide_covers_every_mandatory_claude_trigger():
    guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    for policy_phrase in (
        "contracts, interfaces, schemas",
        "trading or portfolio safety",
        "data integrity, storage, replay, migration, retention",
        "concurrency, idempotency, scheduling",
        "timezone, or DST behavior",
        "nondeterministic, recurring, or flaky bug",
        "silent-bad-data risk",
        "new component or end-to-end feature",
        "final material merge/PR checkpoint",
    ):
        assert policy_phrase in guide


@pytest.mark.unit
def test_agent_guide_covers_complex_bugs_reports_waivers_and_d19():
    guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    normalized_guide = " ".join(guide.split())

    assert "any mandatory trigger applies" in guide
    assert "two or more" in guide
    assert "reviewed head SHA" in guide
    assert "exact bound revision" in guide
    assert "visibly `waived`, never `pass`" in guide
    assert "runtime D19: Codex performs collection and Claude performs evaluation" in normalized_guide

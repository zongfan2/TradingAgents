from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_ci_uses_offline_verifier_for_tests_and_lint():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python -m devtools.verification.offline --only tests" in workflow
    assert "python -m devtools.verification.offline --only lint" in workflow


@pytest.mark.unit
def test_agent_guide_declares_hybrid_roles_and_required_gate():
    guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Codex — primary builder" in guide
    assert "Claude Code — independent verifier" in guide
    assert "python -m devtools.verification.offline --only all" in guide
    assert "two or more" in guide
    assert "pipeline/contracts/" in guide
    assert "~/.tradingagents/verification/" in guide

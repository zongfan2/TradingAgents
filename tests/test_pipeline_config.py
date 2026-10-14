"""Pipeline config backend roles (D20: claude collects on Sonnet 5.5 and
evaluates on Opus 5.5; supersedes D19's codex collection default).

Covers the design-doc config appendix rows ``collect_backend`` /
``eval_backend`` and the pinned claude model/effort per role: shipped
defaults, ``TRADINGAGENTS_*`` env overrides, and loud rejection of invalid
values (config-time, never a mid-slot CLI error).
"""

import pytest

from pipeline.claude_cli import EFFORT_CHOICES
from pipeline.config import (
    BACKEND_CHOICES,
    DEFAULT_COLLECT_BACKEND,
    DEFAULT_EVAL_BACKEND,
    PipelineConfig,
    load_config,
)


@pytest.mark.unit
def test_backend_defaults_are_the_d20_claude_roles():
    config = load_config({})
    assert config.collect_backend == "claude"
    assert config.eval_backend == "claude"
    assert (DEFAULT_COLLECT_BACKEND, DEFAULT_EVAL_BACKEND) == ("claude", "claude")
    assert set(BACKEND_CHOICES) == {"claude", "codex"}
    assert (config.claude_collect_model, config.claude_collect_effort) == (
        "claude-sonnet-5-5",
        "medium",
    )
    assert (config.claude_eval_model, config.claude_eval_effort) == ("claude-opus-5-5", "high")
    # Independence out of the box: the roles must not collapse onto one model.
    assert config.claude_collect_model != config.claude_eval_model


@pytest.mark.unit
def test_claude_model_and_effort_env_overrides_win():
    config = load_config(
        {
            "TRADINGAGENTS_CLAUDE_COLLECT_MODEL": "claude-haiku-5-5",
            "TRADINGAGENTS_CLAUDE_COLLECT_EFFORT": "low",
            "TRADINGAGENTS_CLAUDE_EVAL_MODEL": "claude-fable-5-1",
            "TRADINGAGENTS_CLAUDE_EVAL_EFFORT": " medium ",
        }
    )
    assert (config.claude_collect_model, config.claude_collect_effort) == (
        "claude-haiku-5-5",
        "low",
    )
    assert (config.claude_eval_model, config.claude_eval_effort) == ("claude-fable-5-1", "medium")


@pytest.mark.unit
@pytest.mark.parametrize(
    "key", ["TRADINGAGENTS_CLAUDE_COLLECT_EFFORT", "TRADINGAGENTS_CLAUDE_EVAL_EFFORT"]
)
def test_invalid_claude_effort_env_is_rejected_loudly(key):
    with pytest.raises(ValueError, match="turbo"):
        load_config({key: "turbo"})


@pytest.mark.unit
def test_effort_choices_match_the_claude_cli_levels():
    assert EFFORT_CHOICES == ("low", "medium", "high", "xhigh", "max")


@pytest.mark.unit
def test_empty_claude_model_field_is_rejected_at_construction():
    with pytest.raises(ValueError, match="claude_eval_model"):
        PipelineConfig(claude_eval_model="")


@pytest.mark.unit
def test_backend_env_overrides_win():
    config = load_config(
        {
            "TRADINGAGENTS_COLLECT_BACKEND": "claude",
            "TRADINGAGENTS_EVAL_BACKEND": "codex",
        }
    )
    assert config.collect_backend == "claude"
    assert config.eval_backend == "codex"


@pytest.mark.unit
def test_blank_backend_env_falls_back_to_default():
    config = load_config(
        {
            "TRADINGAGENTS_COLLECT_BACKEND": "  ",
            "TRADINGAGENTS_EVAL_BACKEND": "",
            "TRADINGAGENTS_CLAUDE_EVAL_MODEL": " ",
        }
    )
    assert config.collect_backend == "claude"
    assert config.eval_backend == "claude"
    assert config.claude_eval_model == "claude-opus-5-5"


@pytest.mark.unit
@pytest.mark.parametrize("key", ["TRADINGAGENTS_COLLECT_BACKEND", "TRADINGAGENTS_EVAL_BACKEND"])
def test_invalid_backend_env_is_rejected_loudly(key):
    with pytest.raises(ValueError, match="gemini"):
        load_config({key: "gemini"})


@pytest.mark.unit
def test_invalid_backend_field_is_rejected_at_construction():
    with pytest.raises(ValueError, match="collect_backend"):
        PipelineConfig(collect_backend="gpt")
    with pytest.raises(ValueError, match="eval_backend"):
        PipelineConfig(eval_backend="")

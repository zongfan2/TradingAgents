"""Pipeline config backend roles (D19: codex collects, claude evaluates).

Covers the design-doc config appendix rows ``collect_backend`` /
``eval_backend``: shipped defaults, ``TRADINGAGENTS_*_BACKEND`` env overrides,
and loud rejection of invalid values (config-time, never a mid-slot CLI error).
"""

import pytest

from pipeline.config import (
    BACKEND_CHOICES,
    DEFAULT_COLLECT_BACKEND,
    DEFAULT_EVAL_BACKEND,
    PipelineConfig,
    load_config,
)


@pytest.mark.unit
def test_backend_defaults_are_the_d19_swap():
    config = load_config({})
    assert config.collect_backend == "codex"  # collection is the heavy consumer
    assert config.eval_backend == "claude"
    assert (DEFAULT_COLLECT_BACKEND, DEFAULT_EVAL_BACKEND) == ("codex", "claude")
    # Independence out of the box: the roles must not collapse onto one CLI.
    assert config.collect_backend != config.eval_backend
    assert set(BACKEND_CHOICES) == {"claude", "codex"}


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
        {"TRADINGAGENTS_COLLECT_BACKEND": "  ", "TRADINGAGENTS_EVAL_BACKEND": ""}
    )
    assert config.collect_backend == "codex"
    assert config.eval_backend == "claude"


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

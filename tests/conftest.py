"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Importing the package triggers tradingagents/__init__.py's load_dotenv(),
# which pours the developer's real repo .env into os.environ (override=False).
# Force it HERE, once, so the hermetic strip below sees the complete damage —
# otherwise the first test that imports tradingagents re-pollutes the env
# mid-session (bit us 2026-08-24 when TRADINGAGENTS_SLOT_TIME_US landed in
# .env and leaked into the installer tests).
import tradingagents  # noqa: F401,E402


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        # `or` not a .get default: an env var present but empty (e.g. a key left
        # blank in a .env copied from .env.example) must still get the placeholder.
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


@pytest.fixture(autouse=True)
def _isolate_config():
    """Reset the global dataflows config before and after each test.

    ``set_config`` merges (it never clears keys absent from the override), so a
    test that sets e.g. ``tool_vendors`` would otherwise leak into later tests
    and make routing behavior order-dependent. Replace the global outright so
    every test starts from a clean DEFAULT_CONFIG.
    """
    import copy

    import tradingagents.dataflows.config as config_module
    import tradingagents.default_config as default_config

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    yield
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client


@pytest.fixture(autouse=True)
def _hermetic_tradingagents_env(monkeypatch):
    """Strip EVERY ambient TRADINGAGENTS_* env var and block entrypoint-level
    .env loading, so each test sees only shipped defaults plus what it sets
    itself. The developer's real .env (loaded by the package import above and
    by orchestrator/installer mains) must never skew assertions."""
    for key in list(os.environ):
        if key.startswith("TRADINGAGENTS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TRADINGAGENTS_SKIP_DOTENV", "1")


@pytest.fixture(autouse=True)
def _isolate_claude_usage_log(_hermetic_tradingagents_env, monkeypatch, tmp_path):
    """Route the D20 claude usage log to a per-test temp file. It requests the
    env strip above so the redirect is always set after it: autouse fixtures
    carry no ordering guarantee between themselves, and the strip would remove
    the redirect. The HOME sandbox below is the second line of defence."""
    monkeypatch.setenv("TRADINGAGENTS_CLAUDE_USAGE_LOG", str(tmp_path / "claude_usage.jsonl"))


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    """A Notifier built without an injected runner would really run osascript
    (macOS) or pop a Windows toast (WSL). Tests that care inject their own
    runner; every other one gets a runner that delivers nothing."""
    from pipeline import orchestrator

    monkeypatch.setattr(orchestrator, "default_osascript_runner", lambda argv: 0)


# The developer's real review archive (claude_review's default report dir),
# resolved at import, before any fixture can sandbox HOME. By 2026-10-14 tests
# that omitted report_dir had filed 500+ fake runs/bbbb… records there.
_PRODUCTION_VERIFICATION_DIR = Path(
    os.path.realpath(Path.home() / ".tradingagents" / "verification")
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
# Mutating audit event -> (path arg, its dir_fd arg or None) pairs to check.
# For links only the new name is a write; the source may lie anywhere.
_MUTATED_PATHS = {
    "os.mkdir": ((0, 2),), "os.rmdir": ((0, 1),), "os.remove": ((0, 1),),
    "os.rename": ((0, 2), (1, 3)), "os.link": ((1, 3),), "os.symlink": ((1, 2),),
    "os.utime": ((0, 3),), "os.chmod": ((0, 2),), "os.chown": ((0, 3),),
    "os.chflags": ((0, None),), "os.truncate": ((0, None),),
    "shutil.rmtree": ((0, 1),),
}
_blocked_writes: list[str] = []


def _block_production_verification_writes(event, args):
    """Audit hook: refuse writes this process aims at the real archive.

    Defense in depth behind ``_sandbox_home`` that also covers paths captured
    before the sandbox existed. It is process-local on purpose: a before/after
    snapshot of the archive would blame the suite for concurrent real reviews
    writing there (Layer 1 runs this suite inside those reviews). Not covered:
    sqlite files, names relative to a directory fd, and child processes (they
    inherit the sandboxed HOME)."""
    if event == "open":
        if not args[2] & _WRITE_FLAGS:
            return
        targets = ((args[0], None),)
    elif event in _MUTATED_PATHS:
        targets = [
            (args[path], None if dir_fd is None else args[dir_fd])
            for path, dir_fd in _MUTATED_PATHS[event]
        ]
    else:
        return
    for candidate, dir_fd in targets:
        # fd-based rmtree audits bare names relative to dir_fd, not the cwd.
        if dir_fd not in (None, -1) or not isinstance(candidate, (str, bytes, os.PathLike)):
            continue
        try:
            path = Path(os.fsdecode(os.path.realpath(candidate)))
        except (OSError, ValueError):  # e.g. a deleted cwd; the call itself reports it
            continue
        if path.is_relative_to(_PRODUCTION_VERIFICATION_DIR):
            _blocked_writes.append(f"{event} {path}")
            raise PermissionError(f"tests must not write the real archive: {path}")


sys.addaudithook(_block_production_verification_writes)


@pytest.fixture(scope="session")
def production_verification_dir():
    return _PRODUCTION_VERIFICATION_DIR


@pytest.fixture(autouse=True)
def _sandbox_home(monkeypatch, tmp_path_factory):
    """Point ``~`` at a fresh per-test directory, so defaults resolved at call
    time, such as claude_review's ``~/.tradingagents/verification``, land there
    instead of in the developer's real home (values computed at import keep the
    real home); the shipped defaults stay untouched. Fails the test if the
    audit guard had to block a write to the real archive."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    yield
    blocked = list(_blocked_writes)
    _blocked_writes.clear()
    if blocked:
        pytest.fail(
            "test wrote to the real verification archive:\n" + "\n".join(blocked),
            pytrace=False,
        )


@pytest.fixture
def blocked_production_writes(_sandbox_home):
    """The guard's log, for tests of the guard itself: what they provoke on
    purpose is dropped before ``_sandbox_home`` checks for leaks."""
    yield _blocked_writes
    _blocked_writes.clear()

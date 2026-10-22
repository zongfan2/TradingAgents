"""Lark/Feishu webhook notification channel — specs/orchestrator.md
"Notifications". Fully offline: the HTTP POST is injected."""

import base64
import hashlib
import hmac
import json
import logging

import pytest

from pipeline import orchestrator
from pipeline.config import PipelineConfig, load_config

URL = "https://open.feishu.cn/open-apis/bot/v2/hook/0000-secret-token"
SECRET = "s3cr3t-signing-key"


class FakePost:
    def __init__(self, status=200, body=b'{"code":0,"msg":"success","data":{}}', exc=None):
        self.status, self.body, self.exc = status, body, exc
        self.calls = []

    def __call__(self, url, body, timeout):
        self.calls.append((url, json.loads(body.decode("utf-8")), timeout))
        if self.exc is not None:
            raise self.exc
        return self.status, self.body


def ok_desktop(argv):
    return 0


def failing_desktop(argv):
    raise FileNotFoundError("powershell.exe")


# ---------------------------------------------------------------------------
# Message format and signature
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unsigned_text_message():
    post = FakePost()
    orchestrator.LarkWebhook(URL, post=post)("TradingAgents pipeline", "slot us 2026-10-21: 8 ok")
    url, body, timeout = post.calls[0]
    assert url == URL and timeout == orchestrator.LARK_TIMEOUT_S
    assert body == {
        "msg_type": "text",
        "content": {"text": "TradingAgents pipeline\nslot us 2026-10-21: 8 ok"},
    }


@pytest.mark.unit
def test_signed_message_matches_the_documented_algorithm():
    post = FakePost()
    orchestrator.LarkWebhook(URL, SECRET, post=post, clock=lambda: 1761000000.9)("t", "m")
    body = post.calls[0][1]
    assert body["timestamp"] == "1761000000"
    key = f"1761000000\n{SECRET}".encode()
    expected = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
    assert body["sign"] == expected == orchestrator.lark_sign("1761000000", SECRET)


@pytest.mark.unit
def test_non_ascii_text_is_sent_as_utf8():
    post = FakePost()
    orchestrator.LarkWebhook(URL, post=post)("标题", "消息 — ok")
    assert post.calls[0][1]["content"]["text"] == "标题\n消息 — ok"


# ---------------------------------------------------------------------------
# Failures: raised as LarkError, never carrying the credentials
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("post", "fragment"),
    [
        (FakePost(status=400, body=b"bad"), "HTTP 400"),
        (FakePost(body=b'{"code":19021,"msg":"sign match fail or timestamp is not within one hour"}'),
         "code 19021"),
        (FakePost(body=b'{"StatusCode":9499,"StatusMessage":"Bad Request"}'), "code 9499"),
        (FakePost(body=b"<html>"), "not JSON"),
        (FakePost(body=b"[]"), "code None"),
        (FakePost(exc=OSError(f"connection to {URL} refused ({SECRET})")), "OSError"),
    ],
)
def test_failures_raise_without_credentials(post, fragment):
    with pytest.raises(orchestrator.LarkError) as excinfo:
        orchestrator.LarkWebhook(URL, SECRET, post=post)("t", "m")
    text = str(excinfo.value)
    assert fragment in text
    assert URL not in text and SECRET not in text


@pytest.mark.unit
def test_legacy_status_code_zero_is_success():
    post = FakePost(body=b'{"StatusCode":0,"StatusMessage":"success"}')
    orchestrator.LarkWebhook(URL, post=post)("t", "m")  # does not raise


# ---------------------------------------------------------------------------
# Notifier: both channels, independent failures, shared dedup
# ---------------------------------------------------------------------------


def notifier(desktop=ok_desktop, post=None, enabled=True):
    post = post or FakePost()
    webhook = orchestrator.LarkWebhook(URL, SECRET, post=post)
    return orchestrator.Notifier(
        enabled=enabled, runner=desktop, platform="wsl", webhook=webhook
    ), post


@pytest.mark.unit
def test_both_channels_deliver_and_dedup_covers_both():
    n, post = notifier()
    assert n.notify("t", "m", dedup_key=("x", ("us", "d"), "r")) is True
    assert n.notify("t", "m", dedup_key=("x", ("us", "d"), "r")) is False
    assert n.delivered == [("t", "m")] and n.webhook_delivered == [("t", "m")]
    assert len(post.calls) == 1


@pytest.mark.unit
def test_desktop_failure_still_sends_to_lark():
    n, post = notifier(desktop=failing_desktop)
    assert n.notify("t", "m") is True
    assert n.delivered == [] and n.webhook_delivered == [("t", "m")]


@pytest.mark.unit
def test_lark_failure_is_log_only_and_keeps_desktop(caplog):
    caplog.set_level(logging.WARNING, logger="pipeline.orchestrator")
    n, _ = notifier(post=FakePost(exc=OSError(f"boom {URL}")))
    assert n.notify("t", "m") is True
    assert n.delivered == [("t", "m")] and n.webhook_delivered == []
    assert "lark notifier failed" in caplog.text
    assert URL not in caplog.text and SECRET not in caplog.text


@pytest.mark.unit
def test_unexpected_webhook_exception_logs_only_its_type(caplog):
    caplog.set_level(logging.WARNING, logger="pipeline.orchestrator")

    def webhook(title, message):
        raise ValueError(f"leaky {URL}")

    n = orchestrator.Notifier(runner=ok_desktop, platform="wsl", webhook=webhook)
    n.notify("t", "m")
    assert "lark notifier failed (ValueError)" in caplog.text
    assert URL not in caplog.text


@pytest.mark.unit
def test_disabled_notifier_sends_nothing():
    n, post = notifier(enabled=False)
    assert n.notify("t", "m") is False
    assert post.calls == [] and n.delivered == []


# ---------------------------------------------------------------------------
# Config and wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_reads_webhook_env_and_hides_it_from_repr():
    config = load_config(
        {"TRADINGAGENTS_LARK_WEBHOOK_URL": f" {URL} ",
         "TRADINGAGENTS_LARK_WEBHOOK_SECRET": SECRET}
    )
    assert config.lark_webhook_url == URL and config.lark_webhook_secret == SECRET
    assert URL not in repr(config) and SECRET not in repr(config)
    assert load_config({}).lark_webhook_url is None


@pytest.mark.unit
def test_config_rejects_http_and_secret_without_url(tmp_path):
    with pytest.raises(ValueError, match="https://") as excinfo:
        PipelineConfig(state_dir=tmp_path, lark_webhook_url="http://example.com/hook/tok")
    assert "tok" not in str(excinfo.value)
    with pytest.raises(ValueError, match="without lark_webhook_url"):
        PipelineConfig(state_dir=tmp_path, lark_webhook_secret=SECRET)


@pytest.mark.unit
def test_make_notifier_wires_lark_only_when_configured(tmp_path):
    plain = orchestrator.make_notifier(PipelineConfig(state_dir=tmp_path))
    assert plain._webhook is None
    wired = orchestrator.make_notifier(
        PipelineConfig(state_dir=tmp_path, lark_webhook_url=URL, lark_webhook_secret=SECRET)
    )
    assert isinstance(wired._webhook, orchestrator.LarkWebhook)
    assert URL not in repr(wired._webhook) and SECRET not in repr(wired._webhook)


@pytest.mark.unit
def test_orchestrator_and_watchdog_use_the_configured_notifier(tmp_path, monkeypatch):
    built = []
    real = orchestrator.make_notifier

    def spy(config):
        built.append(config)
        return real(config)

    monkeypatch.setattr(orchestrator, "make_notifier", spy)
    config = PipelineConfig(state_dir=tmp_path / "state", notifications_enabled=False)
    orchestrator.Orchestrator(config, "us")
    orchestrator.run_watchdog(config)
    assert built == [config, config]


# ---------------------------------------------------------------------------
# Review of 91a4f45: partial-URL leaks, bad settings, total deadline
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_transport_error_quoting_only_the_path_never_leaks_the_token(caplog):
    import http.client

    caplog.set_level(logging.WARNING, logger="pipeline.orchestrator")
    leak = http.client.InvalidURL(
        "URL can't contain control characters. "
        f"{'/open-apis/bot/v2/hook/0000-secret-token '!r} (found at least ' ')"
    )
    n, _ = notifier(post=FakePost(exc=leak))
    n.notify("t", "m")
    assert "lark notifier failed (InvalidURL)" in caplog.text
    assert "0000-secret-token" not in caplog.text


@pytest.fixture
def no_network(monkeypatch):
    """Any socket use fails the test (AGENTS.md: tests are offline)."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("test reached the network")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.mark.unit
def test_real_stack_rejects_an_inner_space_locally_without_echoing_it(no_network):
    # urllib strips outer whitespace, so the space must be inside the path for
    # http.client to refuse the request before any connection is attempted.
    webhook = orchestrator.LarkWebhook(URL + " x")
    with pytest.raises(orchestrator.LarkError) as excinfo:
        webhook("t", "m")
    assert str(excinfo.value) == "InvalidURL"


@pytest.mark.unit
def test_real_stack_http_error_reports_the_status(no_network, monkeypatch):
    import io
    import urllib.error
    import urllib.request

    closed = []

    class Body(io.BytesIO):
        def close(self):
            closed.append(True)
            super().close()

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, Body(b"gone"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(orchestrator.LarkError) as excinfo:
        orchestrator.LarkWebhook(URL)("t", "m")
    assert str(excinfo.value) == "HTTP 404"
    assert closed


@pytest.mark.unit
def test_a_hanging_post_is_abandoned_at_the_deadline():
    import threading
    import time

    release = threading.Event()

    def hang(url, body, timeout):
        release.wait(5)
        return 200, b'{"code":0}'

    started = time.monotonic()
    with pytest.raises(orchestrator.LarkError, match="no response within"):
        orchestrator.LarkWebhook(URL, post=hang, deadline=0.2)("t", "m")
    assert time.monotonic() - started < 2
    release.set()


@pytest.mark.unit
@pytest.mark.parametrize(
    "env",
    [
        {"TRADINGAGENTS_LARK_WEBHOOK_URL": f'"{URL}"'},
        {"TRADINGAGENTS_LARK_WEBHOOK_URL": f"{URL} # team bot"},
        {"TRADINGAGENTS_LARK_WEBHOOK_URL": "http://open.feishu.cn/hook/0000-secret-token"},
        {"TRADINGAGENTS_LARK_WEBHOOK_SECRET": SECRET},
        {"TRADINGAGENTS_LARK_WEBHOOK_URL": URL, "TRADINGAGENTS_LARK_WEBHOOK_SECRET": "a b"},
    ],
)
def test_bad_lark_env_disables_only_lark_and_never_echoes_it(env, caplog):
    caplog.set_level(logging.WARNING, logger="pipeline.config")
    config = load_config(env)  # must not raise: every job would die at startup
    assert config.lark_webhook_url is None and config.lark_webhook_secret is None
    assert "Lark notifications disabled" in caplog.text
    assert "0000-secret-token" not in caplog.text and SECRET not in caplog.text


@pytest.mark.unit
def test_watchdog_still_runs_with_a_bad_lark_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_SKIP_DOTENV", "1")
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TRADINGAGENTS_NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("TRADINGAGENTS_LARK_WEBHOOK_URL", f'"{URL}"')
    assert orchestrator.main(["watchdog"]) == 0


@pytest.mark.unit
def test_details_reach_lark_only():
    n, post = notifier()
    assert n.notify("t", "m", details="• AMZN BUY 37 @263 (dry-run)") is True
    assert n.delivered == [("t", "m")]
    assert n.webhook_delivered == [("t", "m\n• AMZN BUY 37 @263 (dry-run)")]

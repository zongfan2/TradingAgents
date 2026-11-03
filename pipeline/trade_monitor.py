"""Always-on trade listener (specs/trade-notifications.md T4).

    python -m pipeline.trade_monitor

Subscribes to the paper account's ``trade_updates`` stream and sends each
reportable order event to the Lark trades group (exactly once, shared with
the hourly fallback). Read-only toward the broker. Single instance: a second
start exits 0 at once (the scheduled task re-fires every 10 minutes so a
crashed listener comes back). Exits 0 without the trades webhook.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import trade_notify
from pipeline.config import load_config

logger = logging.getLogger("pipeline.trade_monitor")

LOCK_NAME = "trade_monitor.lock"
#: Re-run the snapshot reconcile this often inside the listener, so a gap
#: the stream cannot replay (a reconnect) is filled within minutes.
RECONCILE_EVERY_SECONDS = 15 * 60


def make_trades_sender(config: Any) -> trade_notify.Sender | None:
    """The trades group's signed webhook, or None when it is not configured."""
    if not config.lark_trades_webhook_url:
        return None
    from pipeline.orchestrator import LarkWebhook

    return LarkWebhook(config.lark_trades_webhook_url, config.lark_trades_webhook_secret)


def acquire_single_instance(state_dir: Path) -> Any | None:
    """The held lock file handle, or None when another listener runs."""
    path = state_dir / "locks" / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - held for the process lifetime
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _reconcile_loop(broker: Any, state_dir: Path, send: trade_notify.Sender, stop: threading.Event,
                    ledger_dir: Path | None = None) -> None:
    while not stop.wait(RECONCILE_EVERY_SECONDS):
        try:
            trade_notify.reconcile(broker, state_dir=state_dir, send=send, now=datetime.now(timezone.utc),
                                   ledger_dir=ledger_dir)
        except Exception as exc:  # the stream keeps running
            logger.warning("periodic reconcile failed: %s", type(exc).__name__)


def main(argv: list[str] | None = None, *, stream_factory: Any = None, broker_factory: Any = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from pipeline.orchestrator import _load_repo_dotenv

    _load_repo_dotenv()
    config = load_config()
    send = make_trades_sender(config) if config.notifications_enabled else None
    if send is None:
        logger.info("trades webhook not configured or notifications disabled — trade listener not started")
        return 0
    lock = acquire_single_instance(config.state_dir)
    if lock is None:
        logger.info("another trade listener is running — exiting")
        return 0
    api_key, secret = trade_notify.alpaca_keys()
    broker = (broker_factory or (lambda: trade_notify.AlpacaReadOnly(api_key, secret)))()
    state_dir = config.state_dir
    # Events missed while no listener ran (first run: history recorded only).
    ledger_dir = config.ledger_dir
    # The first-run cutoff: events from now on are this listener's to report.
    trade_notify.SentStore(state_dir).first_started(datetime.now(timezone.utc))
    try:  # a failing snapshot read must not keep the real-time stream down
        trade_notify.reconcile(broker, state_dir=state_dir, send=send, now=datetime.now(timezone.utc),
                               ledger_dir=ledger_dir)
    except Exception as exc:
        logger.warning("startup reconcile failed (%s); the stream starts anyway", type(exc).__name__)

    async def on_update(data: Any) -> None:
        try:
            event = trade_notify.event_from_update(data)
            if event is not None:
                await asyncio.to_thread(trade_notify.deliver, [event], state_dir=state_dir,
                                        send=send, positions=broker.positions, ledger_dir=ledger_dir)
        except Exception as exc:  # one bad message never stops the stream
            logger.warning("trade update not handled: %s", type(exc).__name__)

    if stream_factory is None:
        from alpaca.common.enums import BaseURL
        from alpaca.trading.stream import TradingStream

        def stream_factory() -> Any:
            # Paper pin, as structural as the adapter's REST one.
            return TradingStream(api_key, secret, paper=True, raw_data=True,
                                 url_override=BaseURL.TRADING_STREAM_PAPER.value)

    stop = threading.Event()
    threading.Thread(target=_reconcile_loop, args=(broker, state_dir, send, stop, ledger_dir),
                     name="trade-reconcile", daemon=True).start()
    stream = stream_factory()
    stream.subscribe_trade_updates(on_update)
    logger.info("trade listener running (paper)")
    try:
        stream.run()
    finally:
        stop.set()
        lock.close()
    return 1  # run() returns only when the stream stopped: let the task restart it


if __name__ == "__main__":
    sys.exit(main())

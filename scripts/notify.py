"""Set up, check and repair the notification channel.

    python scripts/notify.py --status          # what is queued, what failed
    python scripts/notify.py --preview         # what each alert would look like
    python scripts/notify.py --test            # send one message, prove it works
    python scripts/notify.py --resend          # deliver alerts left pending

Alerts are otherwise sent only by the daily worker, and only when something
happens. That leaves two gaps this closes.

**Nothing tells you the credentials work.** Without a test send you find out on
the day of a regime change, which is the worst day to discover a typo in a chat
id.

**A message can be recorded and never delivered.** The worker persists an alert
before sending it, so a channel that was down leaves the row PENDING rather than
losing it. ``--resend`` is how those get delivered once the channel is back.

Only the *names* of the environment variables live in ``config/alerts.yaml``;
the token itself is read from the environment and is never written anywhere
(``ARCHITECTURE.md`` §9).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder.alerts.engine import AlertEngine, NullNotifier
from fear_ladder.alerts.telegram import TelegramNotConfiguredError, TelegramNotifier
from fear_ladder.alerts.templates import TEMPLATES, render
from fear_ladder.config.loader import load_config
from fear_ladder.config.schema import AppConfig
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging

logger = logging.getLogger("notify")

#: A pending alert older than this describes a market that has moved on.
DEFAULT_RESEND_WITHIN_DAYS = 7


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true", help="queue and failure counts")
    action.add_argument("--preview", action="store_true", help="render every template")
    action.add_argument("--test", action="store_true", help="send one test message")
    action.add_argument("--resend", action="store_true", help="deliver pending alerts")
    parser.add_argument(
        "--within-days",
        type=int,
        default=DEFAULT_RESEND_WITHIN_DAYS,
        help="with --resend: how far back to reach (0 = no limit)",
    )
    return parser


def _channel(config: AppConfig):
    """The configured channel, or ``None`` with the reason said out loud."""
    if not config.alerts.telegram.enabled:
        logger.warning("telegram is disabled in config/alerts.yaml")
        return None
    try:
        return TelegramNotifier.from_spec(config.alerts.telegram)
    except TelegramNotConfiguredError as exc:
        logger.error("%s", exc)
        logger.error(
            "set %s and %s in the environment (locally in .env, in CI as "
            "repository secrets). They are never written to a file here.",
            config.alerts.telegram.bot_token_env,
            config.alerts.telegram.chat_id_env,
        )
        return None


def _status(config: AppConfig, db: Path | None) -> int:
    with SQLiteUnitOfWork(db) as uow:
        rows = uow.connection.execute(
            "SELECT delivery_status, COUNT(*) n FROM alert_events GROUP BY 1 ORDER BY n DESC"
        ).fetchall()
        pending = uow.events.get_pending_alerts()
        recent = uow.connection.execute(
            "SELECT event_date, event_type, delivery_status, error FROM alert_events "
            "ORDER BY event_date DESC, id DESC LIMIT 5"
        ).fetchall()

    channel = _channel(config)
    print(f"channel   : {channel.name if channel else 'none configured'}")
    print(f"delivering: {bool(channel and channel.delivers)}")
    print("\nalerts by status:")
    for row in rows or []:
        print(f"  {row[0]:<12}{row[1]:>5}")
    if not rows:
        print("  (none recorded yet)")
    if pending:
        print(f"\n{len(pending)} pending — run --resend once a channel is configured")
    print("\nmost recent:")
    for row in recent:
        detail = f"  {row[3]}" if row[3] else ""
        print(f"  {row[0]}  {row[1]:<24}{row[2]}{detail}")
    return 0


def _preview(db: Path | None) -> int:
    """Render every template against the latest stored day.

    Reads nothing from the network and writes nothing, so it is safe to run
    against production at any time.
    """
    with SQLiteUnitOfWork(db) as uow:
        state = uow.states.get_latest_state()
        if state is None:
            logger.error("no stored state yet; run scripts/daily_runner.py first")
            return 2
        allocation = uow.states.get_allocation(
            state.observation_date, strategy_version=state.strategy_version
        )

    for name in TEMPLATES:
        rendered = render(name, state, allocation)
        print("=" * 64)
        print(f"[{name}]  {rendered.title}")
        print("-" * 64)
        print(rendered.body)
        print()
    print(f"({len(TEMPLATES)} templates, rendered from {state.observation_date})")
    return 0


def _test(config: AppConfig) -> int:
    channel = _channel(config)
    if channel is None:
        return 2
    message = (
        "<b>FearLadder 연결 확인</b>\n\n"
        "이 메시지가 보이면 봇 토큰과 chat id 가 올바릅니다.\n"
        "실제 알림은 단계가 바뀌거나 데이터에 문제가 생겼을 때만 옵니다."
    )
    try:
        channel.send_text(message)
    except Exception as exc:  # the reason matters more than the type here
        logger.error("send failed: %s", exc)
        return 1
    logger.info("sent. check the chat.")
    return 0


def _resend(config: AppConfig, db: Path | None, within_days: int) -> int:
    channel = _channel(config)
    engine = AlertEngine(config.alerts, channel or NullNotifier())
    with SQLiteUnitOfWork(db) as uow:
        report = engine.send_pending(
            uow.events, within_days=within_days if within_days > 0 else None
        )
        uow.commit()
    logger.info("%s", report.summary())
    return 0 if not report.failed else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)
    config = load_config()

    if args.status:
        return _status(config, args.db)
    if args.preview:
        return _preview(args.db)
    if args.test:
        return _test(config)
    return _resend(config, args.db, args.within_days)


if __name__ == "__main__":
    raise SystemExit(main())

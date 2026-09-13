"""The operational daily worker (``ARCHITECTURE.md`` §4.1).

    python scripts/daily_runner.py

Run by GitHub Actions after the US close. It collects, validates, computes,
persists, detects events and notifies. It does not place orders
(``PRD.md`` §1.2).

Exit codes, chosen so a workflow can react:

``0``  the day was processed (including a recorded DATA_FAILURE)
``1``  the run crashed; nothing was committed as a normal state
``2``  the strategy is not frozen and the research gate was not opened
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder.alerts.engine import (
    AlertEngine,
    NotificationProvider,
    NullNotifier,
)
from fear_ladder.alerts.telegram import TelegramNotConfiguredError, TelegramNotifier
from fear_ladder.config.loader import (
    ensure_production_ready,
    load_config,
    load_research_placeholder_config,
)
from fear_ladder.config.schema import AppConfig, ResearchParameterError
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.pipeline.daily import DailyPipeline

logger = logging.getLogger("daily_runner")

#: How far back to retry alerts that were never delivered.
RESEND_WITHIN_DAYS = 7


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="run as of this date instead of today (safe to repeat: idempotent)",
    )
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--profile",
        default="operational",
        choices=("operational", "placeholder"),
        help="'placeholder' runs the RESEARCH_PLACEHOLDER profile for development",
    )
    parser.add_argument(
        "--no-collect", action="store_true", help="use whatever is already stored"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and persist, but do not send notifications",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def _notifier(config: AppConfig, *, dry_run: bool) -> NotificationProvider:
    if dry_run or not config.alerts.telegram.enabled:
        logger.info("notifications disabled for this run")
        return NullNotifier()
    try:
        return TelegramNotifier.from_spec(config.alerts.telegram)
    except TelegramNotConfiguredError as exc:
        # Loud, and the run continues: the state is still worth recording, and
        # the pending alert will be retried next run.
        logger.error("%s", exc)
        logger.error("continuing without notifications; alerts will stay PENDING")
        return NullNotifier()


def _deliver_pending(config: AppConfig, notifier: NotificationProvider, uow: object):
    """Deliver anything an earlier run recorded but could not send.

    A day when Telegram was down — or when the credentials were not set yet —
    leaves its alerts PENDING rather than losing them. Retrying here is what
    makes configuring the channel later actually deliver what is queued, instead
    of requiring someone to notice and run a repair command.

    Bounded on purpose: an alert that has waited a week describes a market that
    has moved on, and sending it then is noise rather than news.
    """
    if not notifier.delivers:
        return None
    report = AlertEngine(config.alerts, notifier).send_pending(
        uow.events,  # type: ignore[attr-defined]
        within_days=RESEND_WITHIN_DAYS,
    )
    if not (report.sent or report.failed or report.suppressed):
        return None
    return f"pending alerts: {report.summary()}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)

    config = (
        load_research_placeholder_config()
        if args.profile == "placeholder"
        else load_config()
    )
    logger.info(
        "strategy %s (%s)",
        config.strategy.strategy_version,
        config.strategy.parameter_status.value,
    )

    try:
        ensure_production_ready(config)
    except ResearchParameterError as exc:
        logger.error("%s", exc)
        logger.error(
            "set FEAR_LADDER_ALLOW_RESEARCH_PARAMS=1 only for development, "
            "never in the scheduled workflow"
        )
        return 2

    notifier = _notifier(config, dry_run=args.dry_run)
    pipeline = DailyPipeline(config, notifier=notifier)

    with SQLiteUnitOfWork(args.db) as uow:
        result = pipeline.run(
            uow,
            as_of=args.date,
            collect=not args.no_collect,
            send_alerts=not args.dry_run,
        )
        retried = _deliver_pending(config, notifier, uow)
        if retried:
            uow.commit()

    print(result.summary())
    if retried:
        logger.info("%s", retried)
    if result.alerts.sent:
        logger.info("sent %d alert(s)", len(result.alerts.sent))
    if result.alerts.failed:
        logger.error("%d alert(s) could not be delivered", len(result.alerts.failed))
    if result.is_data_failure:
        logger.warning("recorded a DATA_FAILURE; no investment signal was produced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

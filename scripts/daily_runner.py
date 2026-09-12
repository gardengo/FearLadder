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

from regime_monitor.alerts.engine import NotificationProvider, NullNotifier
from regime_monitor.alerts.telegram import TelegramNotConfiguredError, TelegramNotifier
from regime_monitor.config.loader import (
    ensure_production_ready,
    load_config,
    load_research_placeholder_config,
)
from regime_monitor.config.schema import AppConfig, ResearchParameterError
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork
from regime_monitor.monitoring.logging import configure_logging
from regime_monitor.pipeline.daily import DailyPipeline

logger = logging.getLogger("daily_runner")


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
            "set REGIME_MONITOR_ALLOW_RESEARCH_PARAMS=1 only for development, "
            "never in the scheduled workflow"
        )
        return 2

    pipeline = DailyPipeline(config, notifier=_notifier(config, dry_run=args.dry_run))

    with SQLiteUnitOfWork(args.db) as uow:
        result = pipeline.run(
            uow,
            as_of=args.date,
            collect=not args.no_collect,
            send_alerts=not args.dry_run,
        )

    print(result.summary())
    if result.alerts.sent:
        logger.info("sent %d alert(s)", len(result.alerts.sent))
    if result.alerts.failed:
        logger.error("%d alert(s) could not be delivered", len(result.alerts.failed))
    if result.is_data_failure:
        logger.warning("recorded a DATA_FAILURE; no investment signal was produced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

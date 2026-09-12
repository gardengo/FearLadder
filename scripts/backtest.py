"""Run a strategy backtest and write its artifacts (research path).

    python scripts/backtest.py --profile placeholder --report

This is research tooling. It never touches the operational strategy file and the
daily worker never imports it (``BACKTEST_SPEC.md`` 28).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from regime_monitor import paths
from regime_monitor.config.loader import load_config, load_research_placeholder_config
from regime_monitor.config.schema import AppConfig
from regime_monitor.data.collection import CollectionService
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork
from regime_monitor.monitoring.logging import configure_logging
from regime_monitor.research.backtest_runner import StrategyBacktest
from regime_monitor.research.data_loader import load_market_data
from regime_monitor.research.reports import ReportWriter

logger = logging.getLogger("backtest")


def _load_profile(profile: str) -> AppConfig:
    if profile == "placeholder":
        return load_research_placeholder_config()
    if profile == "operational":
        return load_config()
    return load_config(strategy_path=Path(profile))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        default="placeholder",
        help=(
            "'placeholder' (RESEARCH_PLACEHOLDER profile), 'operational' "
            "(config/strategy.yaml), or a path to a strategy yaml"
        ),
    )
    parser.add_argument("--db", type=Path, default=None, help="SQLite path override")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--collect",
        action="store_true",
        help="fetch the configured sources into the database before running",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help=f"write artifacts to {paths.BACKTEST_REPORT_DIR}",
    )
    parser.add_argument("--no-benchmarks", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)

    config = _load_profile(args.profile)
    logger.info(
        "strategy %s (%s)",
        config.strategy.strategy_version,
        config.strategy.parameter_status.value,
    )
    if unresolved := config.unresolved_parameters():
        logger.error(
            "this profile has %d unresolved research parameters: %s",
            len(unresolved),
            ", ".join(unresolved),
        )
        logger.error("a backtest needs a complete parameter set; use --profile placeholder")
        return 2

    with SQLiteUnitOfWork(args.db) as uow:
        if args.collect:
            start = args.start or config.data_sources.price.start_date
            end = args.end or date.today()
            report = CollectionService.from_config(config.data_sources).collect(
                uow.observations, start=start, end=end
            )
            logger.info("collection: %s", report.summary())
            uow.commit()

        data = load_market_data(uow.observations, config, start=args.start, end=args.end)

    logger.info(
        "loaded %d trading days (%s .. %s)",
        len(data.closes),
        data.closes.index.min(),
        data.closes.index.max(),
    )

    run = StrategyBacktest(config).run(data, include_benchmarks=not args.no_benchmarks)
    print(run.comparison().to_string())

    if config.strategy.parameter_status.value == "RESEARCH_PLACEHOLDER":
        logger.warning(
            "these numbers come from RESEARCH_PLACEHOLDER parameters and describe "
            "the machinery, not a strategy"
        )

    if args.report:
        written = ReportWriter().write(run, config)
        logger.info("wrote %d artifacts to %s", len(written), paths.BACKTEST_REPORT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

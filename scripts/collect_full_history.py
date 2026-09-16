"""Rebuild the full observation history into a scratch database.

    python scripts/collect_full_history.py --out path/to/full.db

``data/fear_ladder.db`` is pruned to five years on purpose: it is the
*operational* database, it is committed, and the daily worker only ever reads
recent rows. But every measurement in ``docs/strategy.md`` §2.5–§2.11 needs the
whole history, so research has to rebuild it somewhere else first.

This script refuses to write to the operational database. Overwriting it has
already cost this project once: a report regenerated against the pruned window
silently replaced 30-year figures with 5-year ones and the difference (CAGR
18.88% → 14.62%) looked like a strategy change rather than a data accident.
``scripts/make_performance_report.py`` grew a guard for the same reason.

The rebuilt file is disposable and belongs outside the repository. Collection
takes a few minutes and hits the configured providers, so keep the result
around for the length of a research session rather than rebuilding per run.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.data.collection import CollectionService
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging

logger = logging.getLogger("collect_full")

#: The strategy's own history starts at the 1999 QQQ inception; indicators need
#: a warm-up before that, and the reconstruction reaches back further still.
START = date(1996, 1, 2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, required=True,
        help="where to build the full database (must not be the operational one)",
    )
    parser.add_argument("--start", type=date.fromisoformat, default=START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    return parser


def _refuse_operational(out: Path) -> None:
    operational = (paths.DATA_DIR / "fear_ladder.db").resolve()
    if out.resolve() == operational:
        raise SystemExit(
            f"refusing to rebuild into the operational database ({operational}).\n"
            "It is committed and deliberately pruned to five years. Pass --out "
            "somewhere outside the repository instead."
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)
    _refuse_operational(args.out)

    # Seed from the operational database so its schema and any rows it already
    # holds are reused; collection then fills in everything older.
    if not args.out.exists():
        args.out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.DATA_DIR / "fear_ladder.db", args.out)
        logger.info("seeded %s from the operational database", args.out)

    config = load_config()
    service = CollectionService.from_config(config.data_sources)
    with SQLiteUnitOfWork(args.out) as uow:
        report = service.collect(
            uow.observations, start=args.start, end=args.end or date.today()
        )
        uow.commit()

    logger.info("collected: %s", report.collected)
    if report.failures:
        logger.error("failures: %s", report.failures)
    if report.skipped:
        logger.warning("skipped: %s", report.skipped)

    connection = sqlite3.connect(args.out)
    for symbol, count, first, last in connection.execute(
        "SELECT symbol, COUNT(*), MIN(observation_date), MAX(observation_date) "
        "FROM market_observations GROUP BY symbol ORDER BY symbol"
    ):
        logger.info("%-16s %6d rows  %s .. %s", symbol, count, first, last)

    # The measurements in §2.11 turn on this date: before it, the leveraged
    # sleeves are reconstructed prices rather than observed ones.
    logger.info(
        "leveraged sleeves are real prices from %s onward",
        max(config.data_sources.price.symbols[s].inception for s in ("QLD", "TQQQ")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

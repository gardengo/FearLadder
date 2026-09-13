"""Trim raw observations to a rolling window, so the committed database stays small.

    python scripts/prune_observations.py --keep-years 5

The daily workflow commits ``data/fear_ladder.db`` on every trading day, so the
file's size is a standing cost. Git turns out to handle it far better than the
raw size suggests — measured on this database, a 9.7 MB file packs to 1.2 MB and
an ordinary day's change adds about 6 KB — so this is about keeping the working
tree and a fresh clone small, not about rescuing the history.

Only ``market_observations`` is pruned. Computed state — the daily regime,
allocation, scores and events — is tiny and is what the dashboard's history is
actually made of, so it is kept forever.

The window is checked against what the indicators need before anything is
deleted (:mod:`fear_ladder.data.retention`). A window shorter than the longest
lookback would not just shrink the file, it would quietly degrade the next
signal, so this refuses rather than warns.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder.config.loader import load_config
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.data.retention import plan
from fear_ladder.monitoring.logging import configure_logging

logger = logging.getLogger("prune")

DEFAULT_KEEP_YEARS = 5.0
#: Compact only once this share of the file is free pages. Below it, rewriting
#: the file costs more in git history than the space it reclaims.
VACUUM_ABOVE_WASTE = 0.10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-years", type=float, default=DEFAULT_KEEP_YEARS)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="measure the window back from this date instead of today",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would go, delete nothing"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    retention = plan(config, keep_years=args.keep_years, as_of=args.as_of or date.today())
    logger.info("%s", retention.describe())
    if not retention.safe:
        logger.error(
            "refusing to prune: %g years is below the %.2f-year floor the enabled "
            "indicators need. Pruning here would leave tomorrow's score computed "
            "from a half-filled window with nothing to say so.",
            retention.keep_years,
            retention.minimum_years,
        )
        return 2

    with SQLiteUnitOfWork(args.db) as uow:
        connection = uow.connection
        cutoff = retention.cutoff.isoformat()
        doomed = connection.execute(
            "SELECT COUNT(*) FROM market_observations WHERE observation_date < ?",
            (cutoff,),
        ).fetchone()[0]
        kept = connection.execute(
            "SELECT COUNT(*) FROM market_observations WHERE observation_date >= ?",
            (cutoff,),
        ).fetchone()[0]

        if not doomed:
            logger.info("nothing older than %s; %d observations kept", cutoff, kept)
            return 0
        if args.dry_run:
            logger.info("would delete %d observations before %s, keeping %d",
                        doomed, cutoff, kept)
            return 0

        connection.execute(
            "DELETE FROM market_observations WHERE observation_date < ?", (cutoff,)
        )
        uow.commit()
        logger.info("deleted %d observations before %s, kept %d", doomed, cutoff, kept)

    # VACUUM cannot run inside a transaction, so it comes after the unit of work
    # closes. Without it the pages are freed but the file does not shrink.
    #
    # It is not run on every prune, though. VACUUM rewrites the file, and the
    # database is committed to git daily: measured, an ordinary day costs about
    # 6 KB of pack and a day that vacuums costs about 51 KB. Deleting a handful
    # of rows and leaving the pages on the freelist is cheaper, and the next
    # prune reuses them.
    from fear_ladder import paths
    from fear_ladder.data.repositories.connection import connect

    target = Path(args.db or paths.default_db_path())
    connection = connect(args.db)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        free = connection.execute("PRAGMA freelist_count").fetchone()[0]
        total = connection.execute("PRAGMA page_count").fetchone()[0]
        wasted = free / total if total else 0.0
        if wasted >= VACUUM_ABOVE_WASTE:
            logger.info("%.0f%% of pages are free; compacting", wasted * 100)
            connection.execute("VACUUM")
        else:
            logger.info(
                "%.0f%% of pages are free, under the %.0f%% mark; leaving the file "
                "as it is so tomorrow's commit stays a small delta",
                wasted * 100,
                VACUUM_ABOVE_WASTE * 100,
            )
    finally:
        connection.close()

    logger.info("%s is now %.1f MB", target.name, target.stat().st_size / 1048576)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

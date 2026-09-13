"""Replay past trading days so the dashboard has history on its first load.

    python scripts/backfill_history.py --from 2024-01-01

The daily worker records one day per run, so a freshly seeded database shows a
single row and the history tab is empty. This walks the trading days already
stored and runs the same pipeline over each one, in order, exactly as the
scheduled job would have.

Two things make that honest rather than a fabrication:

* The pipeline is strictly causal. Replaying 2024-03-15 reads only what was
  observable on 2024-03-15, so the row it writes is the row the worker would
  have written that evening.
* Alerts are not sent. A regime change in 2024 is real history worth keeping in
  the events table, but notifying anyone about it now would be nonsense, so the
  alert rows are marked SKIPPED instead of left PENDING for a later resend.

The window cannot start earlier than the indicators' warm-up allows; the
observations before it exist to fill the rolling windows, not to be scored.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder.config.loader import load_config
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.data.retention import required_trading_days
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.pipeline.daily import DailyPipeline

logger = logging.getLogger("backfill")

#: Commit every so often so a long run is not one enormous transaction.
COMMIT_EVERY = 50
PROGRESS_EVERY = 50


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replay days that already have a stored state",
    )
    return parser


def _trading_days(uow: SQLiteUnitOfWork, start: date, end: date | None) -> list[date]:
    sql = (
        "SELECT DISTINCT observation_date FROM market_observations "
        "WHERE symbol = 'QQQ' AND observation_date >= ?"
    )
    params: list[object] = [start.isoformat()]
    if end is not None:
        sql += " AND observation_date <= ?"
        params.append(end.isoformat())
    rows = uow.connection.execute(sql + " ORDER BY observation_date", params).fetchall()
    return [date.fromisoformat(row[0]) for row in rows]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    logger.info(
        "%s (%s)", config.strategy.strategy_version, config.strategy.parameter_status.value
    )
    version = config.strategy.strategy_version

    with SQLiteUnitOfWork(args.db) as uow:
        first_observed = uow.connection.execute(
            "SELECT MIN(observation_date) FROM market_observations WHERE symbol = 'QQQ'"
        ).fetchone()[0]
        if first_observed is None:
            logger.error("no observations stored; collect data before backfilling")
            return 2

        warm_up = required_trading_days(config)
        available = _trading_days(uow, date.fromisoformat(first_observed), args.start)
        if len(available) < warm_up:
            logger.error(
                "only %d trading days of history sit before %s, and the indicators "
                "need %d to fill their windows. Scoring from there would produce "
                "numbers that look fine and are not.",
                len(available),
                args.start,
                warm_up,
            )
            return 2

        days = _trading_days(uow, args.start, args.end)
        if not days:
            logger.error("no stored trading days in the requested range")
            return 2

        done = {
            row[0]
            for row in uow.connection.execute(
                "SELECT observation_date FROM market_states WHERE strategy_version = ?",
                (version,),
            ).fetchall()
        }
        todo = days if args.force else [d for d in days if d.isoformat() not in done]
        logger.info(
            "%d trading days from %s to %s; %d already stored, replaying %d",
            len(days), days[0], days[-1], len(days) - len(todo), len(todo),
        )

        pipeline = DailyPipeline(config)
        started = time.monotonic()
        for index, day in enumerate(todo, start=1):  # no-op when nothing is left
            pipeline.run(uow, as_of=day, collect=False, send_alerts=False)
            if index % COMMIT_EVERY == 0:
                uow.commit()
            if index % PROGRESS_EVERY == 0 or index == len(todo):
                rate = index / max(time.monotonic() - started, 1e-9)
                logger.info(
                    "%d/%d (%s) — %.1f days/s, ~%.0fs left",
                    index, len(todo), day, rate, (len(todo) - index) / max(rate, 1e-9),
                )

        # A regime change in 2024 belongs in the events table; an unsent
        # notification about it does not belong in the resend queue. SUPPRESSED
        # is the status the schema allows for "decided not to send" — the
        # cooldown path already uses it, and delivery_status carries a CHECK
        # constraint, so inventing a new value aborts the transaction.
        marked = uow.connection.execute(
            "UPDATE alert_events SET delivery_status = 'SUPPRESSED', "
            "error = 'backfilled history; never eligible to send' "
            "WHERE delivery_status = 'PENDING' AND event_date <= ?",
            (days[-1].isoformat(),),
        ).rowcount
        uow.commit()
        logger.info("marked %d backfilled alert(s) SUPPRESSED", marked)

        states = uow.connection.execute(
            "SELECT COUNT(*) FROM market_states WHERE strategy_version = ?", (version,)
        ).fetchone()[0]
        logger.info("%d stored states for %s", states, version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

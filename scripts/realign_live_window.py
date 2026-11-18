"""Put stretches of the live record that a catch-up corrected back on the engine's path.

    python scripts/realign_live_window.py --db path/to/copy.db

Before TASK-188 the worker did not resume its regime rules from the frozen
backtest's own state. Its stored path drifted from the engine's, and the first
run after the fix stepped back onto it with a catch-up: an event on the day it
ran, naming the day the engine had actually moved (``REGIME_CHANGED_ON``). The
stored states between those two days still say what the drifted path said.

``scripts/rewrite_replayed_history.py`` left that stretch alone on purpose: it
was a record of what had been signalled. Nobody traded on it, though, and the
owner decided (2026-11-18) that a paper account carrying a bug's signals is
less useful than one carrying the strategy's. So for each catch-up event:

* every stored state from the day the engine moved to the day before the
  catch-up is recorded again by the pipeline, resuming from the regime memory
  the live rules carried — the engine's path. Allocations, scores and indicator
  values follow their state. The inputs are today's, as for any replay.
* the change becomes an ordinary event on the day the engine made it; its
  alerts are stored ``SUPPRESSED``, as for every replayed day.
* the catch-up event is removed, and the catch-up day's state is chained to the
  day before it. Its own regime, score and target were already the engine's and
  stay as they are. Alerts that were actually delivered stay in the log.
* the paper track record is rebuilt, because it trades on the targets that
  just changed.

The regime memory is put back exactly as it was afterwards: it is what the live
rules carry forward from, and it already held the engine's path. Every
rewritten day is checked against it before anything is written to ``--db``.

The work happens in a temporary copy. The operational database is refused
unless ``--operational`` is passed, and it should only be passed when no
scheduled run can commit in between.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.config.schema import AppConfig
from fear_ladder.data.repositories.connection import connect
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.pipeline.daily import DailyPipeline
from fear_ladder.pipeline.track_record import TrackRecorder
from fear_ladder.regime.transition import ReasonCode

logger = logging.getLogger("realign_live_window")

REALIGNED = "live window realigned to the engine's path; never sent"


class RealignError(RuntimeError):
    """The realignment cannot go ahead; nothing has been written to the target."""


@dataclass(frozen=True)
class CatchUp:
    """A catch-up event: the day it was recorded and the day the engine moved."""

    event_id: int
    recorded: date
    engine: date


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="the database to realign in place")
    parser.add_argument(
        "--operational",
        action="store_true",
        help="allow --db to be the operational database",
    )
    return parser


def catch_ups(db: Path, version: str) -> list[CatchUp]:
    connection = connect(db, read_only=True)
    try:
        rows = connection.execute(
            "SELECT id, event_date, reason_codes FROM regime_events "
            "WHERE strategy_version = ? AND reason_codes LIKE ? ORDER BY event_date",
            (version, f'%"{ReasonCode.CHANGED_EARLIER}:%'),
        ).fetchall()
    finally:
        connection.close()
    found = []
    for row in rows:
        prefix = f'"{ReasonCode.CHANGED_EARLIER}:'
        start = row["reason_codes"].index(prefix) + len(prefix)
        found.append(
            CatchUp(
                event_id=row["id"],
                recorded=date.fromisoformat(row["event_date"]),
                engine=date.fromisoformat(row["reason_codes"][start : start + 10]),
            )
        )
    return found


def snapshot(db: Path, version: str) -> dict[str, dict[str, tuple]]:
    connection = connect(db, read_only=True)
    try:
        states = connection.execute(
            "SELECT observation_date, regime, composite_score, target_leverage, reason_codes, "
            "previous_regime, previous_score FROM market_states WHERE strategy_version = ?",
            (version,),
        ).fetchall()
        memory = connection.execute(
            "SELECT observation_date, regime, since, days_held, pending_regime, pending_days "
            "FROM regime_memory WHERE strategy_version = ?",
            (version,),
        ).fetchall()
    finally:
        connection.close()
    return {
        "states": {row[0]: tuple(row) for row in states},
        "memory": {row[0]: tuple(row) for row in memory},
    }


def realign(
    config: AppConfig, work: Path, catch_up: CatchUp, pipeline: DailyPipeline | None = None
) -> list[date]:
    """Record ``catch_up.engine .. catch_up.recorded - 1`` again, in ``work``."""
    version = config.strategy.strategy_version
    pipeline = pipeline or DailyPipeline(config)
    span = (catch_up.engine.isoformat(), catch_up.recorded.isoformat())
    with SQLiteUnitOfWork(work) as uow:
        connection = uow.connection
        days = [
            date.fromisoformat(row[0])
            for row in connection.execute(
                "SELECT observation_date FROM market_states WHERE strategy_version = ? "
                "AND observation_date >= ? AND observation_date < ? ORDER BY 1",
                (version, *span),
            )
        ]
        if not days:
            raise RealignError(f"no stored state between {span[0]} and {span[1]}")
        remembered = {
            row[0]
            for row in connection.execute(
                "SELECT observation_date FROM regime_memory WHERE strategy_version = ? "
                "AND observation_date >= ? AND observation_date < ?",
                (version, *span),
            )
        }
        unremembered = [day for day in days if day.isoformat() not in remembered]
        if unremembered:
            raise RealignError(
                f"no regime memory for {unremembered[:5]}: the engine's path there is unknown"
            )

        kept = connection.execute(
            "SELECT * FROM regime_memory WHERE strategy_version = ?", (version,)
        ).fetchall()
        names = [key for key in kept[0].keys() if key != "id"]  # noqa: SIM118

        # Allocations hang off their state by a cascading foreign key. Emptying
        # the stretch first means each day is chained to the one rewritten
        # before it, and the first to the last day the drift had not reached.
        connection.execute(
            "DELETE FROM market_states WHERE strategy_version = ? "
            "AND observation_date >= ? AND observation_date < ?",
            (version, *span),
        )
        connection.execute("DELETE FROM regime_events WHERE id = ?", (catch_up.event_id,))

        runs = []
        for day in days:
            result = pipeline.run(uow, as_of=day, collect=False, send_alerts=False)
            if result.state is None or result.state.observation_date != day:
                raise RealignError(f"could not score {day}; the run reported {result.summary()}")
            runs.append(result.run.run_id)

        # Not runs of the daily worker; the dashboard reads "the last batch" from these.
        connection.executemany(
            "DELETE FROM pipeline_runs WHERE run_id = ?", [(run_id,) for run_id in runs]
        )
        connection.execute(
            "UPDATE alert_events SET delivery_status = 'SUPPRESSED', error = ? "
            "WHERE delivery_status = 'PENDING' AND strategy_version = ? "
            "AND event_date >= ? AND event_date < ?",
            (REALIGNED, version, *span),
        )
        connection.execute("DELETE FROM regime_memory WHERE strategy_version = ?", (version,))
        connection.executemany(
            f"INSERT INTO regime_memory ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            [tuple(row[name] for name in names) for row in kept],
        )

        # The catch-up day now follows a day in its own regime.
        before = connection.execute(
            "SELECT regime, composite_score FROM market_states WHERE strategy_version = ? "
            "AND observation_date = ?",
            (version, days[-1].isoformat()),
        ).fetchone()
        connection.execute(
            "UPDATE market_states SET previous_regime = ?, previous_score = ? "
            "WHERE strategy_version = ? AND observation_date = ?",
            (before["regime"], before["composite_score"], version, span[1]),
        )
        uow.commit()
    return days


def check(before: dict, after: dict, days: list[date], recorded: list[date]) -> None:
    """Only the realigned days changed, each now holds its remembered regime."""
    if after["memory"] != before["memory"]:
        raise RealignError("the regime memory changed, which this script must never do")
    touched = {day.isoformat() for day in days}
    chained = {day.isoformat() for day in recorded}
    problems = []
    for day, row in before["states"].items():
        if day in touched:
            continue
        now = after["states"].get(day)
        if day in chained:
            row, now = row[:5], None if now is None else now[:5]
        if now != row:
            problems.append(f"{day}: changed outside the realigned days")
    if set(after["states"]) != set(before["states"]):
        problems.append("the set of stored days changed")
    for day in sorted(touched):
        state, memory = after["states"].get(day), after["memory"].get(day)
        if state is None or memory is None or state[1] != memory[1]:
            problems.append(
                f"{day}: stored {state and state[1]}, the engine held {memory and memory[1]}"
            )
    if problems:
        raise RealignError(f"{len(problems)} problem(s): {'; '.join(problems[:8])}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    version = config.strategy.strategy_version
    if args.db.resolve() == paths.default_db_path().resolve() and not args.operational:
        logger.error("%s is the operational database; pass --operational to realign it", args.db)
        return 2

    found = catch_ups(args.db, version)
    if not found:
        logger.info("no catch-up events; the live record already follows the engine")
        return 0

    before = snapshot(args.db, version)
    try:
        with tempfile.TemporaryDirectory(prefix="fear-ladder-realign-") as scratch:
            work = Path(scratch) / "work.db"
            shutil.copyfile(args.db, work)
            realigned: list[date] = []
            for catch_up in found:
                days = realign(config, work, catch_up)
                logger.info(
                    "%s: recorded %d day(s) %s .. %s again on the engine's path "
                    "(it moved on %s); catch-up event removed",
                    catch_up.recorded, len(days), days[0], days[-1], catch_up.engine,
                )
                realigned.extend(days)
            check(before, snapshot(work, version), realigned, [c.recorded for c in found])
            with SQLiteUnitOfWork(work) as uow:
                latest = uow.states.get_latest_state(strategy_version=version)
                if latest is None:
                    raise RealignError("no stored state to rebuild the track record through")
                rebuilt = TrackRecorder(config).rebuild(uow, through=latest.observation_date)
                logger.info("%s", rebuilt.summary())
                integrity = uow.connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RealignError(f"integrity check failed: {integrity}")
            # Nothing has touched the target until this line.
            shutil.copyfile(work, args.db)
    except RealignError as exc:
        logger.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

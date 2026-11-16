"""Put the replayed part of the stored history back on the backtest's path.

    python scripts/rewrite_replayed_history.py --full-db path/to/full.db --db path/to/copy.db

The stored history has two parts, and they are not the same kind of thing.

* **Before the freeze** it is a replay: ``scripts/backfill_history.py`` ran the
  frozen rules over past dates so the dashboard would have something to show.
  That replay was made before TASK-188, when each day's regime depended on
  where the loaded window happened to begin, so most of it follows a path the
  frozen backtest never took. Nobody was ever told any of it. Correcting it
  loses nothing.
* **After the freeze** it is a record: what the worker actually announced each
  morning. Part of that was wrong too, and it stays exactly as it is. A record
  of what was signalled that has been edited to say what should have been
  signalled is not a record.

So this rewrites the first part and leaves the second alone. Up to the anchor
(the last day of the frozen backtest) every stored state, allocation, score,
indicator value, regime event and alert is replaced with what the pipeline
writes when its regime rules resume from the backtest's own path. The paper
track record is then rebuilt, because it is derived from the targets that just
changed.

Nothing is written to ``--db`` until the replay has been checked, day by day,
against the backtest run over the same full history: the same regime, the same
target leverage. One disagreement and the script stops with the target
untouched.

It also fills the trading days after the freeze for which the worker never
stored a state (it did not run, or the price feed was late). Those days get the
row the worker would have written. The regime memory, which the live rules
carry forward from, is put back exactly as it was afterwards.

``--full-db`` is never modified: the replay happens in a temporary copy. The
operational database is refused unless ``--operational`` is passed, and it
should only be passed when no scheduled run can commit in between.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder import paths
from fear_ladder.config.loader import FROZEN_DIR_NAME, load_config
from fear_ladder.config.schema import AppConfig
from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.data.repositories.connection import connect
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.pipeline.daily import DailyPipeline
from fear_ladder.pipeline.track_record import TrackRecorder
from fear_ladder.regime.anchor import RegimeAnchor, anchor_from_decisions, load_anchor
from fear_ladder.research.backtest_runner import BacktestRun, StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.freeze import Fingerprint, FreezeError, RegressionRecord

logger = logging.getLogger("rewrite_history")

FROZEN_DIR = paths.CONFIG_DIR / FROZEN_DIR_NAME

#: Tables whose rows are replaced for the replayed range, with the column that
#: dates them and whether they are scoped to a strategy version.
REPLACED = (
    ("regime_events", "event_date", True),
    ("alert_events", "event_date", True),
    ("indicator_scores", "observation_date", True),
    ("indicator_values", "observation_date", False),
)

#: Everything the pipeline computes. Emptied in the working copy before the
#: replay so that what comes out of it is the replay's and nothing else.
COMPUTED = (
    "target_allocations",
    "market_states",
    "regime_events",
    "alert_events",
    "indicator_scores",
    "indicator_values",
    "track_record",
    "regime_memory",
)

LEVERAGE_TOLERANCE = 1e-6
BACKFILLED = "backfilled history; never eligible to send"


class RewriteError(RuntimeError):
    """The rewrite cannot go ahead; nothing has been written to the target."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-db", type=Path, required=True, help="full history; not modified")
    parser.add_argument("--db", type=Path, required=True, help="the database to rewrite in place")
    parser.add_argument(
        "--operational",
        action="store_true",
        help="allow --db to be the operational database",
    )
    return parser


# ------------------------------------------------------------- the backtest


def backtest_path(config: AppConfig, full_db: Path, anchor: RegimeAnchor) -> BacktestRun:
    """Run the frozen backtest over the full history and prove it is the frozen one."""
    version = config.strategy.strategy_version
    split = config.strategy.dataset_split
    with SQLiteUnitOfWork(full_db) as uow:
        data = load_market_data(uow.observations, config)
    if split.research_start is not None and data.series.index[0] > split.research_start:
        raise RewriteError(
            f"{full_db} starts {data.series.index[0]}, after the research window "
            f"opens ({split.research_start}); it cannot reproduce the frozen backtest"
        )

    backtest = StrategyBacktest(config)
    regression = RegressionRecord.read(FROZEN_DIR / f"{version}.regression.json")
    research = backtest.run(
        data, start=split.research_start, end=split.research_end, include_benchmarks=False
    )
    try:
        regression.check(Fingerprint.of_run(research))
    except FreezeError as exc:
        raise RewriteError(
            f"the full history does not reproduce the frozen backtest: {exc}"
        ) from exc
    logger.info("research window reproduces the frozen fingerprint %s", regression.fingerprint[:16])

    run = backtest.run(data, include_benchmarks=False)
    replayed = anchor_from_decisions(
        run.decisions, strategy_version=version, as_of=anchor.as_of, provenance={}
    )
    if replayed.state != anchor.state:
        raise RewriteError(
            f"the replay reaches {anchor.as_of} in a different state than the "
            f"committed anchor: {replayed.state} vs {anchor.state}"
        )
    logger.info("the replay reaches the committed anchor (%s)", anchor.as_of)
    return run


# ---------------------------------------------------------------- the replay


def replay(
    config: AppConfig, work: Path, run: BacktestRun, start: date, end: date
) -> list[date]:
    """Write the pipeline's rows for ``start..end`` into ``work``, on the backtest's path."""
    version = config.strategy.strategy_version
    before = [
        decision
        for decision in run.decisions
        if decision.observation_date < start and decision.regime != UNKNOWN_REGIME
    ]
    if not before:
        raise RewriteError(f"the backtest has no regime before {start} to resume from")
    seed = before[-1]

    with SQLiteUnitOfWork(work) as uow:
        # The full-history database starts life as a copy of the operational
        # one, so it arrives carrying that database's computed rows — the very
        # rows this script exists to replace. A replay upserts day by day and
        # would leave any of them it did not happen to overwrite: an event on
        # a date the backtest's path has no change on would survive and be
        # transplanted back as if it belonged to the new path.
        for table in COMPUTED:
            uow.connection.execute(f"DELETE FROM {table}")
        uow.regime_memory.save(
            [seed.state.to_memory(seed.observation_date, strategy_version=version)]
        )
        days = [
            date.fromisoformat(row[0])
            for row in uow.connection.execute(
                "SELECT DISTINCT observation_date FROM market_observations "
                "WHERE symbol = 'QQQ' AND observation_date BETWEEN ? AND ? "
                "ORDER BY observation_date",
                (start.isoformat(), end.isoformat()),
            )
        ]
        logger.info(
            "replaying %d trading days %s .. %s from %s since %s (as of %s)",
            len(days), days[0], days[-1], seed.state.regime, seed.state.since,
            seed.observation_date,
        )
        pipeline = DailyPipeline(config)
        started = time.monotonic()
        for index, day in enumerate(days, start=1):
            pipeline.run(uow, as_of=day, collect=False, send_alerts=False)
            if index % 50 == 0:
                uow.commit()
            if index % 100 == 0 or index == len(days):
                logger.info(
                    "%d/%d (%s) — %.1f days/s",
                    index, len(days), day, index / max(time.monotonic() - started, 1e-9),
                )
        uow.connection.execute(
            "UPDATE alert_events SET delivery_status = 'SUPPRESSED', error = ? "
            "WHERE delivery_status = 'PENDING' AND event_date <= ?",
            (BACKFILLED, end.isoformat()),
        )
        uow.commit()
    return days


def check_replay(
    work: Path, run: BacktestRun, version: str, days: list[date], anchor: RegimeAnchor
) -> None:
    """Every replayed day against the backtest: same regime, same target leverage."""
    decisions = {decision.observation_date: decision for decision in run.decisions}
    connection = connect(work, read_only=True)
    try:
        stored = {
            date.fromisoformat(row["observation_date"]): row
            for row in connection.execute(
                "SELECT observation_date, regime, target_leverage FROM market_states "
                "WHERE strategy_version = ? AND observation_date BETWEEN ? AND ?",
                (version, days[0].isoformat(), days[-1].isoformat()),
            )
        }
        memory = connection.execute(
            "SELECT regime, since, days_held, pending_regime, pending_days "
            "FROM regime_memory WHERE strategy_version = ? AND observation_date = ?",
            (version, anchor.as_of.isoformat()),
        ).fetchone()
        events = {
            date.fromisoformat(row["event_date"]): (row["previous_regime"], row["new_regime"])
            for row in connection.execute(
                "SELECT event_date, previous_regime, new_regime FROM regime_events "
                "WHERE strategy_version = ?",
                (version,),
            )
        }
    finally:
        connection.close()

    changes = {
        decision.observation_date: (decision.previous_regime, decision.regime)
        for decision in run.decisions
        if decision.changed and days[0] <= decision.observation_date <= days[-1]
    }
    if events != changes:
        only_stored = sorted(set(events) - set(changes))
        only_backtest = sorted(set(changes) - set(events))
        differing = sorted(day for day in set(events) & set(changes) if events[day] != changes[day])
        raise RewriteError(
            "the replay's regime events are not the backtest's changes: "
            f"only in the replay {only_stored}, only in the backtest {only_backtest}, "
            f"different {differing}"
        )

    problems: list[str] = []
    for day in days:
        row, decision = stored.get(day), decisions.get(day)
        if row is None or decision is None:
            problems.append(f"{day}: {'no stored row' if row is None else 'not in the backtest'}")
            continue
        if row["regime"] != decision.regime:
            problems.append(f"{day}: stored {row['regime']}, backtest {decision.regime}")
            continue
        allocation = run.allocations.get(day)
        expected = None if allocation is None else allocation.target_leverage
        actual = row["target_leverage"]
        if (expected is None) != (actual is None) or (
            expected is not None and abs(expected - actual) > LEVERAGE_TOLERANCE
        ):
            problems.append(f"{day}: leverage stored {actual}, backtest {expected}")
    if problems:
        shown = "; ".join(problems[:8])
        raise RewriteError(
            f"{len(problems)} of {len(days)} replayed days disagree with the backtest: {shown}"
        )

    state = anchor.state
    if memory is None or (
        memory["regime"], date.fromisoformat(memory["since"]), memory["days_held"]
    ) != (state.regime, state.since, state.days_held):
        raise RewriteError(
            f"the replay does not arrive at the anchor on {anchor.as_of}: "
            f"{dict(memory) if memory else None}"
        )
    logger.info(
        "all %d replayed days match the backtest (regime and target leverage), "
        "its %d regime changes are the only events, and the replay arrives at the anchor",
        len(days),
        len(changes),
    )


# ------------------------------------------------------------- the transplant


def _columns(connection: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [
        row[1]
        for row in connection.execute(f"PRAGMA {schema}.table_info({table})")
        if row[1] != "id"
    ]


def transplant(target: Path, work: Path, version: str, start: date, end: date) -> dict[str, int]:
    """Replace the target's rows for ``start..end`` with the replay's, in one transaction."""
    span = (start.isoformat(), end.isoformat())
    connection = connect(target)
    counts: dict[str, int] = {}
    try:
        connection.execute("ATTACH DATABASE ? AS src", (str(work),))
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Allocations hang off their state by a cascading foreign key.
            connection.execute(
                "DELETE FROM market_states WHERE strategy_version = ? "
                "AND observation_date BETWEEN ? AND ?",
                (version, *span),
            )
            names = _columns(connection, "main", "market_states")
            listed = ", ".join(names)
            counts["market_states"] = connection.execute(
                f"INSERT INTO market_states ({listed}) SELECT {listed} "
                "FROM src.market_states WHERE strategy_version = ? "
                "AND observation_date BETWEEN ? AND ?",
                (version, *span),
            ).rowcount
            counts["target_allocations"] = connection.execute(
                "INSERT INTO target_allocations (market_state_id, observation_date, "
                "strategy_version, asset, weight, created_at, updated_at) "
                "SELECT m.id, a.observation_date, a.strategy_version, a.asset, a.weight, "
                "a.created_at, a.updated_at FROM src.target_allocations a "
                "JOIN main.market_states m ON m.observation_date = a.observation_date "
                "AND m.strategy_version = a.strategy_version "
                "WHERE a.strategy_version = ? AND a.observation_date BETWEEN ? AND ?",
                (version, *span),
            ).rowcount

            for table, dated, versioned in REPLACED:
                scope = " AND strategy_version = ?" if versioned else ""
                params = (*span, version) if versioned else span
                connection.execute(
                    f"DELETE FROM {table} WHERE {dated} BETWEEN ? AND ?{scope}", params
                )
                names = _columns(connection, "main", table)
                listed = ", ".join(names)
                counts[table] = connection.execute(
                    f"INSERT INTO {table} ({listed}) SELECT {listed} FROM src.{table} "
                    f"WHERE {dated} BETWEEN ? AND ?{scope}",
                    params,
                ).rowcount
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("DETACH DATABASE src")
    finally:
        connection.close()
    return counts


# -------------------------------------------------------------------- the gaps


def fill_gaps(config: AppConfig, target: Path, after: date) -> list[date]:
    """Store a state for each trading day after ``after`` that never got one.

    The pipeline run that writes the state also re-steps the regime rules for
    that day and stores the result. It is computed from today's inputs, which
    have been revised since, so it can differ in its pending counters from the
    row the live worker chained the following days from. The live chain is the
    one that matters, so the memory rows are restored afterwards.
    """
    version = config.strategy.strategy_version
    with SQLiteUnitOfWork(target) as uow:
        latest = uow.states.get_latest_state(strategy_version=version)
        if latest is None:
            return []
        missing = [
            date.fromisoformat(row[0])
            for row in uow.connection.execute(
                "SELECT DISTINCT o.observation_date FROM market_observations o "
                "WHERE o.symbol = 'QQQ' AND o.observation_date > ? AND o.observation_date < ? "
                "AND NOT EXISTS (SELECT 1 FROM market_states s WHERE s.strategy_version = ? "
                "AND s.observation_date = o.observation_date) ORDER BY 1",
                (after.isoformat(), latest.observation_date.isoformat(), version),
            )
        ]
        if not missing:
            return []

        kept = uow.connection.execute(
            "SELECT * FROM regime_memory WHERE strategy_version = ?", (version,)
        ).fetchall()
        names = [key for key in kept[0].keys() if key != "id"] if kept else []  # noqa: SIM118
        pipeline = DailyPipeline(config)
        runs = []
        for day in missing:
            result = pipeline.run(uow, as_of=day, collect=False, send_alerts=False)
            if result.state is None or result.state.observation_date != day:
                raise RewriteError(f"could not score {day}; the run reported {result.summary()}")
            runs.append(result.run.run_id)
        # These were not runs of the daily worker, and the run log is what the
        # dashboard reads "the last batch" from. Left in, the page would report
        # this script's minute as the time the market was last looked at.
        uow.connection.executemany(
            "DELETE FROM pipeline_runs WHERE run_id = ?", [(run_id,) for run_id in runs]
        )
        marks = ", ".join("?" for _ in missing)
        uow.connection.execute(
            f"UPDATE alert_events SET delivery_status = 'SUPPRESSED', error = ? "
            f"WHERE delivery_status = 'PENDING' AND event_date IN ({marks})",
            (BACKFILLED, *[day.isoformat() for day in missing]),
        )
        if kept:
            uow.connection.execute(
                "DELETE FROM regime_memory WHERE strategy_version = ?", (version,)
            )
            uow.connection.executemany(
                f"INSERT INTO regime_memory ({', '.join(names)}) "
                f"VALUES ({', '.join('?' for _ in names)})",
                [tuple(row[name] for name in names) for row in kept],
            )
        for day in missing:
            stored = uow.connection.execute(
                "SELECT s.regime AS state, m.regime AS memory FROM market_states s "
                "LEFT JOIN regime_memory m ON m.observation_date = s.observation_date "
                "AND m.strategy_version = s.strategy_version "
                "WHERE s.strategy_version = ? AND s.observation_date = ?",
                (version, day.isoformat()),
            ).fetchone()
            if stored["memory"] is not None and stored["state"] != stored["memory"]:
                raise RewriteError(
                    f"{day}: the filled state is {stored['state']} but the live rules "
                    f"held {stored['memory']} that day"
                )
        uow.commit()
    return missing


# ------------------------------------------------------------------ reporting


def snapshot(target: Path, version: str, after: date) -> dict[str, object]:
    """What must not change: every stored state after the anchor, and the memory."""
    connection = connect(target, read_only=True)
    try:
        states = connection.execute(
            "SELECT observation_date, regime, composite_score, target_leverage, reason_codes "
            "FROM market_states WHERE strategy_version = ? AND observation_date > ? "
            "ORDER BY observation_date",
            (version, after.isoformat()),
        ).fetchall()
        memory = connection.execute(
            "SELECT observation_date, regime, since, days_held, pending_regime, pending_days "
            "FROM regime_memory WHERE strategy_version = ? ORDER BY observation_date",
            (version,),
        ).fetchall()
    finally:
        connection.close()
    return {
        "states": {row[0]: tuple(row) for row in states},
        "memory": [tuple(row) for row in memory],
    }


def agreement(target: Path, run: BacktestRun, version: str, end: date) -> tuple[int, int]:
    """How many stored days up to ``end`` hold the backtest's regime."""
    decisions = {decision.observation_date: decision.regime for decision in run.decisions}
    connection = connect(target, read_only=True)
    try:
        rows = connection.execute(
            "SELECT observation_date, regime FROM market_states "
            "WHERE strategy_version = ? AND observation_date <= ?",
            (version, end.isoformat()),
        ).fetchall()
    finally:
        connection.close()
    same = sum(1 for day, regime in rows if decisions.get(date.fromisoformat(day)) == regime)
    return same, len(rows)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    version = config.strategy.strategy_version
    if not config.is_production_ready:
        logger.error("only a frozen strategy has a backtest to go back to")
        return 2
    anchor = load_anchor(version)
    if anchor is None:
        logger.error("no regime anchor for %s; see scripts/make_regime_anchor.py", version)
        return 2

    operational = paths.default_db_path().resolve()
    if args.full_db.resolve() == operational:
        logger.error("--full-db is the operational database, which is pruned to five years")
        return 2
    if args.db.resolve() == operational and not args.operational:
        logger.error(
            "%s is the operational database. Rehearse on a copy first; pass "
            "--operational only when no scheduled run can commit in between.",
            args.db,
        )
        return 2
    if not args.db.is_file():
        logger.error("no database at %s", args.db)
        return 2

    connection = connect(args.db, read_only=True)
    try:
        first = connection.execute(
            "SELECT MIN(observation_date) FROM market_states WHERE strategy_version = ?",
            (version,),
        ).fetchone()[0]
    finally:
        connection.close()
    if first is None or date.fromisoformat(first) > anchor.as_of:
        logger.info(
            "no stored state on or before the anchor (%s); nothing to rewrite", anchor.as_of
        )
        return 0
    start, end = date.fromisoformat(first), anchor.as_of

    try:
        run = backtest_path(config, args.full_db, anchor)
        before = agreement(args.db, run, version, end)
        kept = snapshot(args.db, version, end)

        with tempfile.TemporaryDirectory(prefix="fear-ladder-rewrite-") as scratch:
            work = Path(scratch) / "work.db"
            shutil.copyfile(args.full_db, work)
            days = replay(config, work, run, start, end)
            check_replay(work, run, version, days, anchor)
            # Nothing has touched the target until this line.
            counts = transplant(args.db, work, version, start, end)
        logger.info("replaced %s", ", ".join(f"{table}={n}" for table, n in counts.items()))

        filled = fill_gaps(config, args.db, end)
        if filled:
            logger.info("stored a state for %d day(s) that had none: %s",
                        len(filled), ", ".join(map(str, filled)))

        after = snapshot(args.db, version, end)
        changed = [
            day for day, row in kept["states"].items()  # type: ignore[union-attr]
            if after["states"].get(day) != row  # type: ignore[union-attr]
        ]
        if changed or after["memory"] != kept["memory"]:
            raise RewriteError(
                "rows after the anchor changed, which this script must never do: "
                f"states {changed[:5]}, memory equal={after['memory'] == kept['memory']}"
            )
    except RewriteError as exc:
        logger.error("%s", exc)
        return 2

    with SQLiteUnitOfWork(args.db) as uow:
        latest = uow.states.get_latest_state(strategy_version=version)
        rebuilt = TrackRecorder(config).rebuild(uow, through=latest.observation_date)
        logger.info("%s", rebuilt.summary())
        integrity = uow.connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        logger.error("integrity check failed: %s", integrity)
        return 1

    same, total = agreement(args.db, run, version, end)
    logger.info(
        "stored days up to %s on the backtest's path: %d of %d before, %d of %d now",
        end, before[0], before[1], same, total,
    )
    logger.info(
        "%d stored states after %s are unchanged; regime memory unchanged (%d rows)",
        len(kept["states"]), end, len(kept["memory"]),  # type: ignore[arg-type]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

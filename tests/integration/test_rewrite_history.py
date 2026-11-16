"""Replacing the replayed part of the stored history, and only that part.

``scripts/rewrite_replayed_history.py`` swaps the rows the worker stored for
days before the freeze with a replay that follows the backtest. The replay is
checked against the backtest inside the script, on real data; what is pinned
here is the part that writes to the database someone depends on: that the swap
covers exactly the requested days, leaves every other row as it found it, and
keeps each allocation attached to the state it belongs to.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
import scripts.rewrite_replayed_history as rewrite

from fear_ladder.constants import Asset
from fear_ladder.data.models import MarketState, TargetAllocation
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork, create_database

VERSION = "v-test"
REPLAYED = (date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11))
LIVE = (date(2026, 9, 14), date(2026, 9, 15))


def _fill(path: Path, days: tuple[date, ...], regime: str, weights: dict[Asset, float]) -> None:
    with SQLiteUnitOfWork(path) as uow:
        for day in days:
            uow.states.save_state(
                MarketState(
                    observation_date=day,
                    composite_score=40.0,
                    regime=regime,
                    strategy_version=VERSION,
                ),
                TargetAllocation(
                    observation_date=day, weights=weights, strategy_version=VERSION,
                    regime=regime,
                ),
            )


def _rows(path: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in connection.execute(sql)]
    finally:
        connection.close()


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = create_database(tmp_path / "target.db")
    _fill(path, (*REPLAYED, *LIVE), "Neutral", {Asset.QLD: 0.75, Asset.QQQ: 0.25})
    return path


@pytest.fixture
def replayed(tmp_path: Path) -> Path:
    path = create_database(tmp_path / "work.db")
    # The replay also holds days outside the range: it must not leak them.
    _fill(path, (date(2026, 9, 8), *REPLAYED), "Fear", {Asset.TQQQ: 0.2, Asset.QLD: 0.8})
    return path


def test_only_the_requested_days_are_replaced(target: Path, replayed: Path) -> None:
    counts = rewrite.transplant(target, replayed, VERSION, REPLAYED[0], REPLAYED[-1])

    assert counts["market_states"] == len(REPLAYED)
    stored = dict(_rows(target, "SELECT observation_date, regime FROM market_states"))
    assert {stored[day.isoformat()] for day in REPLAYED} == {"Fear"}
    # What was announced after the freeze is a record, and stays one.
    assert {stored[day.isoformat()] for day in LIVE} == {"Neutral"}
    # A day the replay holds but nobody asked for is not brought across.
    assert "2026-09-08" not in stored


def test_each_allocation_follows_its_state(target: Path, replayed: Path) -> None:
    """Allocations hang off a state by id, and the ids are the target's own."""
    rewrite.transplant(target, replayed, VERSION, REPLAYED[0], REPLAYED[-1])

    joined = _rows(
        target,
        "SELECT s.observation_date, s.regime, a.observation_date, a.asset, a.weight "
        "FROM target_allocations a JOIN market_states s ON s.id = a.market_state_id "
        "ORDER BY a.observation_date, a.asset",
    )
    assert all(state_day == allocation_day for state_day, _, allocation_day, _, _ in joined)
    by_day: dict[str, dict[str, float]] = {}
    for day, _, _, asset, weight in joined:
        by_day.setdefault(day, {})[asset] = weight
    for day in REPLAYED:
        assert by_day[day.isoformat()] == {"QLD": 0.8, "TQQQ": 0.2}
    for day in LIVE:
        assert by_day[day.isoformat()] == {"QLD": 0.75, "QQQ": 0.25}
    # No allocation is left pointing at a state that was deleted.
    assert _rows(
        target,
        "SELECT COUNT(*) FROM target_allocations a LEFT JOIN market_states s "
        "ON s.id = a.market_state_id WHERE s.id IS NULL",
    ) == [(0,)]


def test_a_failed_transplant_leaves_the_target_as_it_was(
    target: Path, replayed: Path
) -> None:
    before = _rows(target, "SELECT observation_date, regime FROM market_states ORDER BY 1")
    connection = sqlite3.connect(replayed)
    try:
        # The replay is missing a table the target expects: the swap must stop
        # and undo the deletions it had already made.
        connection.execute("DROP TABLE regime_events")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(sqlite3.OperationalError):
        rewrite.transplant(target, replayed, VERSION, REPLAYED[0], REPLAYED[-1])

    assert _rows(target, "SELECT observation_date, regime FROM market_states ORDER BY 1") == before
    assert _rows(target, "SELECT COUNT(*) FROM target_allocations") == [(10,)]

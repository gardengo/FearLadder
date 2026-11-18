"""TASK-194: the stretch of the live record a catch-up corrected, put on the engine's path."""

from __future__ import annotations

from pathlib import Path

import pytest
import scripts.realign_live_window as realign

from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.data.models import Provenance
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.regime.transition import ReasonCode
from tests.integration.test_daily_pipeline import TODAY, _seed
from tests.integration.test_regime_memory import (
    LOCKED,
    _anchor,
    _day,
    _pipeline,
    _run,
    _state,
)


@pytest.fixture
def seeded(db_path: Path, provenance: Provenance) -> Path:
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance)
    return db_path


@pytest.fixture
def drifted(seeded: Path, placeholder_config) -> Path:
    """The situation TASK-188 left: a drifted stretch, then a catch-up today.

    Three days stored on a path of their own with no memory of it, the way the
    worker ran before the fix; then today's run resumes from an anchor that
    entered another regime the day before them.
    """
    for offset in (3, 2, 1):
        _run(seeded, placeholder_config, _day(offset))
    with SQLiteUnitOfWork(seeded) as uow:
        uow.connection.execute("DELETE FROM regime_memory")
    result = _run(seeded, placeholder_config, TODAY, _anchor(placeholder_config, as_of=_day(4)))
    assert result.state.regime == LOCKED, "premise of the test"
    assert result.regime_event is not None
    assert result.regime_event.reason_codes[0] == f"{ReasonCode.CHANGED_EARLIER}:{_day(4)}"
    for offset in (3, 2, 1):
        assert _state(seeded, placeholder_config, _day(offset))[0] not in (LOCKED, UNKNOWN_REGIME)
    return seeded


def _realign(db: Path, config) -> tuple[dict, dict, list]:  # type: ignore[no-untyped-def]
    version = config.strategy.strategy_version
    [catch_up] = realign.catch_ups(db, version)
    assert (catch_up.recorded, catch_up.engine) == (TODAY, _day(4))
    before = realign.snapshot(db, version)
    days = realign.realign(
        config, db, catch_up, pipeline=_pipeline(config, _anchor(config, as_of=_day(4)))
    )
    after = realign.snapshot(db, version)
    return before, after, days


def test_the_drifted_days_take_the_engine_s_regime(drifted: Path, placeholder_config) -> None:
    before, after, days = _realign(drifted, placeholder_config)

    assert days == [_day(3), _day(2), _day(1)]
    for day in days:
        assert _state(drifted, placeholder_config, day)[0] == LOCKED
    # Nothing else changed, and the memory the live rules carry is as it was.
    realign.check(before, after, days, [TODAY])


def test_the_catch_up_event_goes_and_today_follows_its_own_regime(
    drifted: Path, placeholder_config
) -> None:
    _realign(drifted, placeholder_config)

    assert realign.catch_ups(drifted, placeholder_config.strategy.strategy_version) == []
    with SQLiteUnitOfWork(drifted) as uow:
        row = uow.connection.execute(
            "SELECT previous_regime, previous_score FROM market_states WHERE observation_date = ?",
            (TODAY.isoformat(),),
        ).fetchone()
        day_before = uow.connection.execute(
            "SELECT composite_score FROM market_states WHERE observation_date = ?",
            (_day(1).isoformat(),),
        ).fetchone()
    assert row["previous_regime"] == LOCKED
    assert row["previous_score"] == pytest.approx(day_before["composite_score"])


def test_the_rewrite_leaves_no_runs_and_nothing_to_send(drifted: Path, placeholder_config) -> None:
    with SQLiteUnitOfWork(drifted) as uow:
        runs = uow.connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]

    _realign(drifted, placeholder_config)

    with SQLiteUnitOfWork(drifted) as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == runs
        # Whatever the rewritten days raised is on record, never on its way out.
        assert uow.connection.execute(
            "SELECT COUNT(*) FROM alert_events WHERE delivery_status = 'PENDING' "
            "AND event_date >= ? AND event_date < ?",
            (_day(4).isoformat(), TODAY.isoformat()),
        ).fetchone()[0] == 0


def test_a_record_with_no_catch_up_is_left_alone(seeded: Path, placeholder_config) -> None:
    for offset in (2, 1, 0):
        _run(seeded, placeholder_config, _day(offset))
    assert realign.catch_ups(seeded, placeholder_config.strategy.strategy_version) == []

"""TASK-196: a scheduled attempt never rewrites a day that is already recorded.

The last attempt of a day runs whatever the feeds carry. When they carry
nothing new — a market holiday, a late feed, a test run in the morning — the
newest day it can score is one that was recorded and announced already. Its
inputs have moved since (CNN revises a day's value for a day or two), so
recomputing it can change a signal after the fact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fear_ladder.data.models import Provenance
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from tests.integration.test_daily_pipeline import TODAY, _pipeline, _seed
from tests.integration.test_regime_memory import _day


@pytest.fixture
def recorded(db_path: Path, provenance: Provenance, placeholder_config) -> Path:
    """Today recorded, and then a source revises today's value.

    Far more than CNN ever moves a day, so that the score visibly moves.
    """
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance)
    with SQLiteUnitOfWork(db_path) as uow:
        _pipeline(placeholder_config).run(uow, as_of=TODAY)
    with SQLiteUnitOfWork(db_path) as uow:
        uow.connection.execute(
            "UPDATE market_observations SET value = value * 3 "
            "WHERE symbol = 'VIX' AND observation_date = ?",
            (TODAY.isoformat(),),
        )
    return db_path


def _stored(db: Path) -> tuple:
    with SQLiteUnitOfWork(db) as uow:
        row = uow.connection.execute(
            "SELECT regime, composite_score, target_leverage, reason_codes, updated_at "
            "FROM market_states WHERE observation_date = ?",
            (TODAY.isoformat(),),
        ).fetchone()
    return tuple(row)


def _count(db: Path, table: str) -> int:
    with SQLiteUnitOfWork(db) as uow:
        return uow.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_a_scheduled_attempt_leaves_a_recorded_day_as_it_was(
    recorded: Path, placeholder_config
) -> None:
    before = _stored(recorded)
    events, alerts, runs = (
        _count(recorded, "regime_events"),
        _count(recorded, "alert_events"),
        _count(recorded, "pipeline_runs"),
    )

    with SQLiteUnitOfWork(recorded) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY, keep_recorded=True)

    assert result.kept
    assert result.state.observation_date == TODAY
    assert "already recorded; kept" in result.summary()
    assert _stored(recorded) == before
    assert (_count(recorded, "regime_events"), _count(recorded, "alert_events")) == (
        events,
        alerts,
    )
    # The run itself is on record: that is how a stalled feed shows.
    assert _count(recorded, "pipeline_runs") == runs + 1


def test_a_person_re_running_the_day_still_recomputes_it(
    recorded: Path, placeholder_config
) -> None:
    """The premise of the test above: the revision does move the score."""
    before = _stored(recorded)

    with SQLiteUnitOfWork(recorded) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)

    assert not result.kept
    assert result.state.composite_score != pytest.approx(before[1])
    assert _stored(recorded)[1] == pytest.approx(result.state.composite_score)


def test_a_new_day_is_recorded_as_usual(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance)
    with SQLiteUnitOfWork(db_path) as uow:
        _pipeline(placeholder_config).run(uow, as_of=_day(1), keep_recorded=True)
    with SQLiteUnitOfWork(db_path) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY, keep_recorded=True)

    assert not result.kept
    assert result.state.observation_date == TODAY
    assert "kept" not in result.summary()

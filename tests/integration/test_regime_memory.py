"""TASK-188 — the daily worker resumes the regime replay instead of restarting it.

The transition rules remember the day they started on, and the worker used to
start them over every run from the first day of whatever history it had loaded
— a window that begins a day later every day. On the operational database the
same date came out Neutral, Optimism or Fear depending only on where that
window began. These tests hold the worker to the opposite: the regime for a
date is fixed by the state carried into it, and by nothing further back.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from fear_ladder.alerts.engine import NullNotifier
from fear_ladder.config.schema import AppConfig
from fear_ladder.constants import UNKNOWN_REGIME, EventType
from fear_ladder.data.models import Provenance, RegimeMemory
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork, create_database
from fear_ladder.pipeline.daily import DailyPipeline, DailyResult
from fear_ladder.regime.anchor import RegimeAnchor
from fear_ladder.regime.classifier import RegimeError, RegimeScale
from fear_ladder.regime.transition import ReasonCode, TransitionEngine, TransitionState
from tests.integration.test_daily_pipeline import TODAY, _NoCollection, _seed

#: The placeholder profile holds a rung for five days, so a state that entered
#: its regime within the last five cannot move whatever the scores say. Several
#: tests lean on that to tell "resumed from here" from "replayed from scratch".
LOCKED = "Overheated"


def _day(offset: int) -> date:
    return TODAY - timedelta(days=offset)


def _pipeline(config: AppConfig, anchor: RegimeAnchor | None = None) -> DailyPipeline:
    return DailyPipeline(
        config,
        notifier=NullNotifier(),
        clock=lambda: datetime(2024, 6, 28, 22, tzinfo=UTC),
        collection_service=_NoCollection(),
        anchor=anchor,
    )


def _run(
    db: Path, config: AppConfig, day: date, anchor: RegimeAnchor | None = None
) -> DailyResult:
    with SQLiteUnitOfWork(db) as uow:
        return _pipeline(config, anchor).run(uow, as_of=day)


def _memory(db: Path, config: AppConfig, day: date) -> RegimeMemory | None:
    with SQLiteUnitOfWork(db) as uow:
        return uow.regime_memory.get(
            day, strategy_version=config.strategy.strategy_version
        )


def _memory_days(db: Path) -> list[str]:
    with SQLiteUnitOfWork(db) as uow:
        rows = uow.connection.execute(
            "SELECT observation_date FROM regime_memory ORDER BY observation_date"
        ).fetchall()
    return [row[0] for row in rows]


def _anchor(config: AppConfig, *, as_of: date, regime: str = LOCKED) -> RegimeAnchor:
    """A state that entered ``regime`` on ``as_of`` itself — locked for five days."""
    return RegimeAnchor(
        strategy_version=config.strategy.strategy_version,
        as_of=as_of,
        state=TransitionState(regime=regime, since=as_of, days_held=1),
    )


@pytest.fixture
def seeded(db_path: Path, provenance: Provenance) -> Path:
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance)
    return db_path


@pytest.fixture
def twin(tmp_path: Path, provenance: Provenance) -> Path:
    """A second database holding the same market history as ``seeded``."""
    path = create_database(tmp_path / "twin.db")
    with SQLiteUnitOfWork(path) as uow:
        _seed(uow, provenance)
    return path


# ------------------------------------------------------------ what is stored


def test_a_run_stores_the_state_it_ended_in(seeded: Path, placeholder_config) -> None:
    result = _run(seeded, placeholder_config, TODAY)
    memory = _memory(seeded, placeholder_config, result.state.observation_date)

    assert memory is not None
    assert memory.regime == result.state.regime
    assert memory.since <= memory.observation_date
    assert memory.days_held >= 1


def test_the_next_day_steps_from_the_stored_row(seeded: Path, placeholder_config) -> None:
    _run(seeded, placeholder_config, _day(1))
    yesterday = _memory(seeded, placeholder_config, _day(1))
    assert yesterday is not None

    result = _run(seeded, placeholder_config, TODAY)
    today = _memory(seeded, placeholder_config, TODAY)

    strategy = placeholder_config.strategy
    engine = TransitionEngine.from_spec(
        RegimeScale.from_spec(strategy.regime), strategy.transition
    )
    expected = engine.step(
        TransitionState.from_memory(yesterday), TODAY, result.state.composite_score
    )
    assert today == expected.state.to_memory(
        TODAY, strategy_version=strategy.strategy_version
    )
    assert result.state.regime == expected.regime


def test_re_running_a_day_leaves_its_memory_as_it_was(
    seeded: Path, placeholder_config
) -> None:
    _run(seeded, placeholder_config, _day(1))
    _run(seeded, placeholder_config, TODAY)
    first = _memory(seeded, placeholder_config, TODAY)
    days = _memory_days(seeded)

    _run(seeded, placeholder_config, TODAY)

    assert _memory(seeded, placeholder_config, TODAY) == first
    assert _memory_days(seeded) == days


# ------------------------------------------------------------- the property


def test_the_regime_does_not_depend_on_where_the_loaded_history_starts(
    seeded: Path, twin: Path, placeholder_config
) -> None:
    # The regression test for TASK-188. Same market, same anchor; one database
    # is walked a day at a time, the other has lost its first 100 days and is
    # asked about today in a single run. Before the fix the two replays started
    # in different places and were free to disagree.
    #
    # 800 days are left on purpose: the longest indicator needs 252 days and is
    # then ranked over 504 more, so a shorter window would change the *scores*
    # as well, which is a different matter from the one tested here.
    anchor = _anchor(placeholder_config, as_of=_day(10), regime="Neutral")
    with SQLiteUnitOfWork(twin) as uow:
        uow.connection.execute(
            "DELETE FROM market_observations WHERE observation_date < ?",
            (_day(800).isoformat(),),
        )

    for offset in range(9, -1, -1):
        walked = _run(seeded, placeholder_config, _day(offset), anchor)
    jumped = _run(twin, placeholder_config, TODAY, anchor)

    # The premise: a shorter window still gives today the same score.
    assert jumped.state.composite_score == pytest.approx(walked.state.composite_score)

    assert jumped.state.regime == walked.state.regime
    assert jumped.state.target_leverage == pytest.approx(walked.state.target_leverage)
    assert _memory(twin, placeholder_config, TODAY) == _memory(
        seeded, placeholder_config, TODAY
    )


def test_days_the_worker_missed_are_stepped_on_its_next_run(
    seeded: Path, twin: Path, placeholder_config
) -> None:
    # One worker runs every day; the other is down for two. They must end in
    # the same place, and the one that was down must have left a row for each
    # day it walked through — tomorrow it resumes from the last of them.
    for offset in (3, 2, 1, 0):
        _run(seeded, placeholder_config, _day(offset))
    _run(twin, placeholder_config, _day(3))
    _run(twin, placeholder_config, TODAY)

    assert _memory(twin, placeholder_config, TODAY) == _memory(
        seeded, placeholder_config, TODAY
    )
    assert _memory_days(twin) == [_day(offset).isoformat() for offset in (3, 2, 1, 0)]


# ----------------------------------------------------------------- the anchor


def test_the_first_run_resumes_from_the_anchor(seeded: Path, placeholder_config) -> None:
    # Nothing is stored yet. A replay from the first scored day would follow
    # the scores; the anchor says the engine entered Overheated three days ago,
    # and a rung that young cannot be left.
    result = _run(seeded, placeholder_config, TODAY, _anchor(placeholder_config, as_of=_day(3)))

    assert result.state.regime == LOCKED
    memory = _memory(seeded, placeholder_config, TODAY)
    assert memory is not None
    assert (memory.since, memory.days_held) == (_day(3), 4)
    assert _memory_days(seeded) == [_day(offset).isoformat() for offset in (2, 1, 0)]


def test_a_stored_row_newer_than_the_anchor_is_what_the_next_run_resumes_from(
    seeded: Path, placeholder_config
) -> None:
    _run(seeded, placeholder_config, _day(1), _anchor(placeholder_config, as_of=_day(4)))

    # A different anchor for the same old date must now be ignored: the
    # database has moved past it.
    stale = _anchor(placeholder_config, as_of=_day(4), regime="Capitulation")
    result = _run(seeded, placeholder_config, TODAY, stale)

    assert result.state.regime == LOCKED
    memory = _memory(seeded, placeholder_config, TODAY)
    assert memory is not None
    assert (memory.since, memory.days_held) == (_day(4), 5)


def test_the_anchor_outranks_a_stored_row_from_before_it_and_the_step_is_recorded(
    seeded: Path, placeholder_config
) -> None:
    # The situation on the day this fix goes live: rows already stored by a
    # replay that started somewhere else, and an anchor that says otherwise.
    before = _run(seeded, placeholder_config, _day(1))
    assert before.state.regime not in (LOCKED, UNKNOWN_REGIME), "premise of the test"

    result = _run(seeded, placeholder_config, TODAY, _anchor(placeholder_config, as_of=_day(1)))

    assert result.state.regime == LOCKED
    # The stored history steps from one regime to another today although the
    # engine changed nothing today. That step is an event, dated where the
    # history shows it and naming the day the engine actually moved …
    event = result.regime_event
    assert event is not None
    assert event.event_date == TODAY
    assert (event.previous_regime, event.new_regime) == (before.state.regime, LOCKED)
    assert event.reason_codes[0] == f"{ReasonCode.CHANGED_EARLIER}:{_day(1)}"
    # … and it reaches the reader the same way any regime change does.
    assert EventType.REGIME_CHANGED in {alert.event_type for alert in result.alerts.created}

    with SQLiteUnitOfWork(seeded) as uow:
        stored = uow.events.get_regime_events(
            strategy_version=placeholder_config.strategy.strategy_version
        )
    assert [e.event_date for e in stored if e.new_regime == LOCKED] == [TODAY]


def test_a_resume_point_older_than_the_loaded_history_stops_the_day(
    seeded: Path, placeholder_config
) -> None:
    # The days between the anchor and the first loaded day cannot be stepped.
    # Guessing them would produce a regime that looks fine and is not.
    ancient = _anchor(placeholder_config, as_of=_day(5000))
    result = _run(seeded, placeholder_config, TODAY, ancient)

    assert result.is_data_failure
    assert result.state.regime == UNKNOWN_REGIME
    assert "REGIME_MEMORY_OUT_OF_WINDOW" in result.state.reason_codes
    assert result.allocation is None
    assert _memory_days(seeded) == []


def test_an_anchor_for_another_strategy_is_refused_at_construction(
    placeholder_config,
) -> None:
    foreign = RegimeAnchor(
        strategy_version="v9.9-other",
        as_of=_day(3),
        state=TransitionState(regime=LOCKED, since=_day(3)),
    )
    with pytest.raises(RegimeError, match=r"v9\.9-other"):
        _pipeline(placeholder_config, foreign)

    unknown_label = RegimeAnchor(
        strategy_version=placeholder_config.strategy.strategy_version,
        as_of=_day(3),
        state=TransitionState(regime="Euphoria", since=_day(3)),
    )
    with pytest.raises(RegimeError, match="Euphoria"):
        _pipeline(placeholder_config, unknown_label)

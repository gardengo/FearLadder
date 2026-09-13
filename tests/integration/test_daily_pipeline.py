"""TASK-110 .. TASK-112, TASK-133 — the daily worker.

These run against a real SQLite database and a fake market, so the stages, the
persistence and the failure paths are exercised together rather than mocked.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from fear_ladder.alerts.engine import NullNotifier
from fear_ladder.constants import (
    UNKNOWN_REGIME,
    Asset,
    DataQualityStatus,
    EventType,
    PipelineStatus,
)
from fear_ladder.data.collection import CollectionReport
from fear_ladder.data.models import Provenance
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.pipeline.daily import DailyPipeline
from tests.conftest import make_price_observation, make_scalar_observation

TODAY = date(2024, 6, 28)
HISTORY_DAYS = 900


def _seed(uow: SQLiteUnitOfWork, provenance: Provenance, *, through: date = TODAY) -> None:
    """A synthetic but complete market history ending at ``through``."""
    rng = np.random.default_rng(31)
    days = [through - timedelta(days=offset) for offset in range(HISTORY_DAYS)][::-1]
    daily = rng.normal(0.0004, 0.012, HISTORY_DAYS)

    qqq = 100.0 * np.cumprod(1 + daily)
    qld = 100.0 * np.cumprod(1 + 2 * daily)
    tqqq = 100.0 * np.cumprod(1 + 3 * daily)
    vix = np.abs(rng.normal(18, 5, HISTORY_DAYS)) + np.abs(daily) * 300
    fng = np.clip(50 + daily.cumsum() * 200 + rng.normal(0, 10, HISTORY_DAYS), 0, 100)

    observations = []
    for index, day in enumerate(days):
        observations.append(make_price_observation("QQQ", day, float(qqq[index]), provenance))
        observations.append(make_price_observation("QLD", day, float(qld[index]), provenance))
        observations.append(make_price_observation("TQQQ", day, float(tqqq[index]), provenance))
        observations.append(make_scalar_observation("VIX", day, float(vix[index]), provenance))
        observations.append(
            make_scalar_observation("CNN_FEAR_GREED", day, float(fng[index]), provenance)
        )
    uow.observations.save_observations(observations)


class _NoCollection:
    """Collection service stand-in: the history is already seeded."""

    def collect(self, repository: Any, *, start: date, end: date) -> CollectionReport:
        return CollectionReport(collected={"seeded": 0})


def _pipeline(config, notifier=None) -> DailyPipeline:
    return DailyPipeline(
        config,
        notifier=notifier or NullNotifier(),
        clock=lambda: datetime(2024, 6, 28, 22, tzinfo=UTC),
        collection_service=_NoCollection(),
    )


@pytest.fixture
def seeded(db_path: Path, provenance: Provenance) -> Path:
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance)
    return db_path


# ------------------------------------------------------------------ TASK-110


def test_a_full_run_produces_a_state_and_an_allocation(
    seeded: Path, placeholder_config
) -> None:
    with SQLiteUnitOfWork(seeded) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)

    assert result.run.status is PipelineStatus.SUCCESS
    assert not result.is_data_failure
    assert result.state.regime in placeholder_config.strategy.regime.labels
    assert result.state.composite_score is not None
    assert result.allocation is not None
    assert sum(result.allocation.weights.values()) == pytest.approx(1.0)
    assert result.state.target_leverage == pytest.approx(result.allocation.target_leverage)


def test_the_run_persists_everything_it_computed(seeded: Path, placeholder_config) -> None:
    with SQLiteUnitOfWork(seeded) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)
        day = result.state.observation_date
        version = placeholder_config.strategy.strategy_version

        assert uow.states.get_state(day, strategy_version=version) is not None
        assert uow.states.get_allocation(day, strategy_version=version) is not None
        assert uow.indicators.get_scores(day, strategy_version=version)
        assert uow.strategies.get_run(result.run.run_id) is not None


def test_the_run_records_each_stage_it_reached(seeded: Path, placeholder_config) -> None:
    with SQLiteUnitOfWork(seeded) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)
        stored = uow.strategies.get_run(result.run.run_id)

    assert stored is not None
    assert stored.stage == "alert", "the last stage reached is recorded"
    assert stored.finished_at is not None


def test_the_state_explains_itself(seeded: Path, placeholder_config) -> None:
    # CLAUDE_CODE_INITIAL_PROMPT.md 12 — reason codes and a score breakdown.
    with SQLiteUnitOfWork(seeded) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)

    assert result.state.reason_codes
    assert any("REGIME_MAPPING" in code for code in result.state.reason_codes)
    assert result.state.score_breakdown
    assert result.breakdown is not None
    assert result.breakdown.top_contributors(3)


def test_the_pipeline_uses_the_latest_scored_trading_day(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    # Data ends on Friday; a Sunday run must describe Friday, not invent Sunday.
    friday = date(2024, 6, 28)
    sunday = date(2024, 6, 30)
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance, through=friday)
    with SQLiteUnitOfWork(db_path) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=sunday)

    assert result.state.observation_date == friday


# ------------------------------------------------------------------ TASK-111


def test_re_running_the_same_day_changes_nothing(seeded: Path, placeholder_config) -> None:
    """The central idempotency guarantee (``ARCHITECTURE.md`` §11)."""
    notifier = NullNotifier()
    pipeline = _pipeline(placeholder_config, notifier)

    with SQLiteUnitOfWork(seeded) as uow:
        first = pipeline.run(uow, as_of=TODAY)
    sent_after_first = len(notifier.sent)

    with SQLiteUnitOfWork(seeded) as uow:
        second = pipeline.run(uow, as_of=TODAY)

        version = placeholder_config.strategy.strategy_version
        states = uow.states.get_state_history(strategy_version=version)
        assert len(states) == 1, "no duplicate state"

        events = uow.events.get_regime_events(strategy_version=version)
        assert len(events) <= 1, "no duplicate regime event"

        alerts = uow.events.get_alerts()
        keys = [alert.dedupe_key for alert in alerts]
        assert len(keys) == len(set(keys)), "no duplicate alert"

    assert second.state.regime == first.state.regime
    assert second.state.composite_score == pytest.approx(first.state.composite_score)
    assert len(notifier.sent) == sent_after_first, "no duplicate notification"


def test_two_runs_produce_identical_advice(seeded: Path, placeholder_config) -> None:
    pipeline = _pipeline(placeholder_config)
    with SQLiteUnitOfWork(seeded) as uow:
        first = pipeline.run(uow, as_of=TODAY)
    with SQLiteUnitOfWork(seeded) as uow:
        second = pipeline.run(uow, as_of=TODAY)

    assert first.allocation is not None and second.allocation is not None
    assert first.allocation.weights == second.allocation.weights
    assert first.state.reason_codes == second.state.reason_codes


# ------------------------------------------------------------------ TASK-112


def test_stale_mandatory_data_produces_unknown_not_a_signal(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    # CLAUDE_CODE_INITIAL_PROMPT.md 14 — no signal on untrustworthy data.
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance, through=TODAY - timedelta(days=60))

    with SQLiteUnitOfWork(db_path) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)

    assert result.is_data_failure
    assert result.state.regime == UNKNOWN_REGIME
    assert result.state.composite_score is None
    assert result.state.target_leverage is None
    assert result.allocation is None
    assert result.run.status is PipelineStatus.DATA_FAILURE


def test_a_data_failure_raises_exactly_one_alert(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance, through=TODAY - timedelta(days=60))

    with SQLiteUnitOfWork(db_path) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)
        alerts = uow.events.get_alerts()

    assert [alert.event_type for alert in alerts] == [EventType.DATA_FAILURE]
    assert result.alerts.created
    body = alerts[0].body
    assert "Target Leverage" not in body, "a failed day must not look like advice"


def test_missing_mandatory_data_is_caught(db_path: Path, placeholder_config) -> None:
    with SQLiteUnitOfWork(db_path) as uow:  # entirely empty database
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)

    assert result.is_data_failure
    assert result.freshness is not None
    assert not result.freshness.usable


def test_data_dated_in_the_future_is_treated_as_corrupt(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    """A source handing back a date past today is a clock or source error.

    The history runs 30 days beyond the day the pipeline believes it is, so
    there is no honest reading of it: acting would be acting on a date mismatch.
    """
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance, through=TODAY + timedelta(days=30))

    with SQLiteUnitOfWork(db_path) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)

    assert result.is_data_failure
    assert result.freshness is not None
    statuses = {source.status for source in result.freshness.failures}
    assert DataQualityStatus.CORRUPT in statuses


def test_replaying_an_earlier_day_is_not_corruption(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    """``daily_runner.py --date`` promises a past day can be re-run.

    It could not: the freshness check saw the days collected since and called
    every source corrupt, so a replay produced a DATA_FAILURE instead of the
    signal that day actually carried.
    """
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance, through=TODAY)

    with SQLiteUnitOfWork(db_path) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY - timedelta(days=30))

    assert not result.is_data_failure
    assert result.allocation is not None


def test_a_degraded_re_run_clears_the_previous_allocation(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    """Bad data must not leave yesterday's advice standing for that date."""
    version = placeholder_config.strategy.strategy_version
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance, through=TODAY)
    with SQLiteUnitOfWork(db_path) as uow:
        good = _pipeline(placeholder_config).run(uow, as_of=TODAY)
        assert uow.states.get_allocation(
            good.state.observation_date, strategy_version=version
        )

    # Now the same date is re-run with the mandatory series wiped.
    with SQLiteUnitOfWork(db_path) as uow:
        uow.connection.execute("DELETE FROM market_observations WHERE symbol = 'VIX'")
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)

        assert result.is_data_failure
        assert uow.states.get_allocation(TODAY, strategy_version=version) is None
        state = uow.states.get_state(TODAY, strategy_version=version)
        assert state is not None
        assert state.target_leverage is None


def test_an_optional_source_going_missing_does_not_stop_the_day(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance, through=TODAY)
        uow.connection.execute(
            "DELETE FROM market_observations WHERE symbol = 'CNN_FEAR_GREED'"
        )

    with SQLiteUnitOfWork(db_path) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)

    assert not result.is_data_failure
    assert result.state.data_quality_status is DataQualityStatus.REVIEW
    assert result.state.composite_score is not None


# ------------------------------------------------------------------ TASK-133


def test_a_crash_is_recorded_and_no_state_is_written(
    seeded: Path, placeholder_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-pipeline failure must leave nothing behind.

    ``CLAUDE_CODE_INITIAL_PROMPT.md`` §5: never push a plausible-looking state
    produced by a run that did not finish.
    """
    from fear_ladder.allocation.engine import AllocationEngine

    class ExplodingError(RuntimeError):
        pass

    def explode(*args: object, **kwargs: object) -> None:
        raise ExplodingError("allocation engine exploded")

    monkeypatch.setattr(AllocationEngine, "allocate", explode)
    pipeline = _pipeline(placeholder_config)

    with pytest.raises(ExplodingError), SQLiteUnitOfWork(seeded) as uow:
        pipeline.run(uow, as_of=TODAY)

    with SQLiteUnitOfWork(seeded) as uow:
        version = placeholder_config.strategy.strategy_version
        assert uow.states.get_state_history(strategy_version=version) == []
        # The transaction rolled back, so even the run record is gone; what
        # survives is the crash in the workflow log, which is the point.
        assert uow.strategies.get_runs() == []


def test_regime_changes_are_recorded_as_events(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    version = placeholder_config.strategy.strategy_version
    with SQLiteUnitOfWork(db_path) as uow:
        _seed(uow, provenance, through=TODAY)

    pipeline = _pipeline(placeholder_config)
    # Walk several days so at least one transition is likely to confirm.
    for offset in range(40, 0, -1):
        with SQLiteUnitOfWork(db_path) as uow:
            pipeline.run(uow, as_of=TODAY - timedelta(days=offset))

    with SQLiteUnitOfWork(db_path) as uow:
        events = uow.events.get_regime_events(strategy_version=version)
        states = uow.states.get_state_history(strategy_version=version)

    assert states
    for event in events:
        assert event.previous_regime != event.new_regime
    dates = [event.event_date for event in events]
    assert len(dates) == len(set(dates)), "one event per date at most"


def test_the_pipeline_refuses_to_start_on_unresolved_parameters(
    placeholder_config,
) -> None:
    """Built here rather than loaded from config/: the shipped file is frozen
    now, so it is no longer a source of unresolved parameters."""
    from fear_ladder.config.schema import RegimeSpec
    from fear_ladder.regime.classifier import RegimeError

    strategy = placeholder_config.strategy.model_copy(
        update={"regime": RegimeSpec()}
    )
    config = placeholder_config.model_copy(update={"strategy": strategy})
    with pytest.raises(RegimeError, match="unresolved research"):
        DailyPipeline(config)


def test_a_daily_run_registers_the_strategy_it_used(
    seeded: Path, placeholder_config
) -> None:
    """The worker is the only thing that runs a version, so it registers one.

    Before this the table stayed empty and the dashboard had to guess the active
    version from whichever state row was newest.
    """
    with SQLiteUnitOfWork(seeded) as uow:
        _pipeline(placeholder_config).run(uow, as_of=TODAY)

    with SQLiteUnitOfWork(seeded) as uow:
        active = uow.strategies.get_active_version()
        assert active is not None
        assert active.strategy_version == placeholder_config.strategy.strategy_version
        assert active.manifest["regime_labels"] == list(
            placeholder_config.strategy.regime.labels or ()
        )


def test_re_running_a_day_does_not_duplicate_the_registration(
    seeded: Path, placeholder_config
) -> None:
    pipeline = _pipeline(placeholder_config)
    for _ in range(2):
        with SQLiteUnitOfWork(seeded) as uow:
            pipeline.run(uow, as_of=TODAY)

    with SQLiteUnitOfWork(seeded) as uow:
        count = uow.connection.execute(
            "SELECT COUNT(*) FROM strategy_versions"
        ).fetchone()[0]
    assert count == 1


def test_the_allocation_never_holds_tqqq_without_the_gate(
    seeded: Path, placeholder_config
) -> None:
    with SQLiteUnitOfWork(seeded) as uow:
        result = _pipeline(placeholder_config).run(uow, as_of=TODAY)

    if result.allocation and result.allocation.weight(Asset.TQQQ) > 0:
        assert result.gate_passed

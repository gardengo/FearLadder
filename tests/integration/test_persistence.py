"""TASK-012 — persistence: insert, update, unique constraints, duplicate
prevention and transaction rollback."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from fear_ladder.constants import (
    UNKNOWN_REGIME,
    Asset,
    DataQualityStatus,
    EventType,
    PipelineStatus,
)
from fear_ladder.data.interfaces import (
    EventRepository,
    IndicatorRepository,
    MarketObservationRepository,
    MarketStateRepository,
    StrategyRepository,
)
from fear_ladder.data.models import (
    AlertEvent,
    DataQualityFinding,
    IndicatorScore,
    IndicatorValue,
    MarketState,
    PipelineRun,
    Provenance,
    RegimeEvent,
    StrategyVersionRecord,
    TargetAllocation,
)
from fear_ladder.data.repositories.connection import (
    SCHEMA_VERSION,
    connect,
    current_schema_version,
)
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from tests.conftest import make_price_observation, make_scalar_observation

DAY = date(2024, 1, 3)
NEXT = date(2024, 1, 4)
VERSION = "v0.0-placeholder"


# ------------------------------------------------------------------- schema


def test_database_is_created_with_every_required_table(db_path: Path) -> None:
    connection = connect(db_path)
    try:
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "market_observations",
            "indicator_values",
            "indicator_scores",
            "market_states",
            "target_allocations",
            "regime_events",
            "alert_events",
            "pipeline_runs",
            "strategy_versions",
        } <= names
        assert current_schema_version(connection) == SCHEMA_VERSION
    finally:
        connection.close()


def test_schema_creation_is_idempotent(db_path: Path) -> None:
    from fear_ladder.data.repositories.sqlite import create_database

    create_database(db_path)
    create_database(db_path)
    connection = connect(db_path)
    try:
        count = connection.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
        assert count == 1
    finally:
        connection.close()


def test_repositories_satisfy_their_protocols(uow: SQLiteUnitOfWork) -> None:
    # ARCHITECTURE.md 7: the application depends on the port, not on SQLite.
    assert isinstance(uow.observations, MarketObservationRepository)
    assert isinstance(uow.indicators, IndicatorRepository)
    assert isinstance(uow.states, MarketStateRepository)
    assert isinstance(uow.events, EventRepository)
    assert isinstance(uow.strategies, StrategyRepository)


# ------------------------------------------------------------- observations


def test_observation_insert_and_read_back(
    uow: SQLiteUnitOfWork, provenance: Provenance
) -> None:
    saved = uow.observations.save_observations(
        [make_price_observation("QQQ", DAY, 400.0, provenance)]
    )
    assert saved == 1

    loaded = uow.observations.get_observation("QQQ", DAY)
    assert loaded is not None
    assert loaded.close == pytest.approx(400.0)
    assert loaded.provenance.provider_library == "FinanceDataReader"
    assert loaded.availability_datetime.tzinfo is not None


def test_observation_upsert_updates_instead_of_duplicating(
    uow: SQLiteUnitOfWork, provenance: Provenance
) -> None:
    uow.observations.save_observations([make_price_observation("QQQ", DAY, 400.0, provenance)])
    uow.observations.save_observations([make_price_observation("QQQ", DAY, 405.0, provenance)])

    rows = uow.observations.get_observations("QQQ")
    assert len(rows) == 1
    assert rows[0].close == pytest.approx(405.0)


def test_observation_unique_constraint_is_enforced_at_the_database(
    uow: SQLiteUnitOfWork,
) -> None:
    uow.connection.execute(
        "INSERT INTO market_observations (symbol, observation_date, availability_datetime, "
        "close, quality_status, provider_library, provider_library_version, "
        "underlying_source, retrieved_at, created_at, updated_at) "
        "VALUES ('QQQ','2024-01-03','2024-01-03T21:00:00+00:00',400,'OK','x','1','y',"
        "'2024-01-03T21:00:00+00:00','2024-01-03T21:00:00+00:00','2024-01-03T21:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        uow.connection.execute(
            "INSERT INTO market_observations (symbol, observation_date, availability_datetime, "
            "close, quality_status, provider_library, provider_library_version, "
            "underlying_source, retrieved_at, created_at, updated_at) "
            "VALUES ('QQQ','2024-01-03','2024-01-03T21:00:00+00:00',400,'OK','x','1','y',"
            "'2024-01-03T21:00:00+00:00','2024-01-03T21:00:00+00:00','2024-01-03T21:00:00+00:00')"
        )


def test_observation_requires_a_close_or_value_at_the_database_too(
    uow: SQLiteUnitOfWork,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        uow.connection.execute(
            "INSERT INTO market_observations (symbol, observation_date, availability_datetime, "
            "quality_status, provider_library, provider_library_version, underlying_source, "
            "retrieved_at, created_at, updated_at) "
            "VALUES ('QQQ','2024-01-05','2024-01-05T21:00:00+00:00','OK','x','1','y',"
            "'2024-01-05T21:00:00+00:00','2024-01-05T21:00:00+00:00','2024-01-05T21:00:00+00:00')"
        )


def test_history_respects_the_availability_cutoff(
    uow: SQLiteUnitOfWork, provenance: Provenance
) -> None:
    # BACKTEST_SPEC.md 4 / 24.6: nothing published later may be read earlier.
    uow.observations.save_observations(
        [
            make_price_observation("QQQ", DAY, 400.0, provenance),
            make_price_observation(
                "QQQ", NEXT, 405.0, provenance, available_at=datetime(2024, 1, 4, 21, tzinfo=UTC)
            ),
        ]
    )
    as_of_day = uow.observations.get_observations(
        "QQQ", available_at=datetime(2024, 1, 3, 23, tzinfo=UTC)
    )
    assert [row.observation_date for row in as_of_day] == [DAY]

    later = uow.observations.get_observations(
        "QQQ", available_at=datetime(2024, 1, 5, 0, tzinfo=UTC)
    )
    assert [row.observation_date for row in later] == [DAY, NEXT]


def test_series_frame_pivots_price_and_scalar_series_together(
    uow: SQLiteUnitOfWork, provenance: Provenance
) -> None:
    uow.observations.save_observations(
        [
            make_price_observation("QQQ", DAY, 400.0, provenance),
            make_scalar_observation("VIX", DAY, 13.5, provenance),
            make_price_observation("QQQ", NEXT, 405.0, provenance),
        ]
    )
    frame = uow.observations.get_series_frame(["QQQ", "VIX"])
    assert list(frame.columns) == ["QQQ", "VIX"]
    assert frame.loc[DAY, "QQQ"] == pytest.approx(400.0)
    assert frame.loc[DAY, "VIX"] == pytest.approx(13.5)
    assert frame["VIX"].isna().loc[NEXT]


def test_latest_observation_date(uow: SQLiteUnitOfWork, provenance: Provenance) -> None:
    assert uow.observations.latest_observation_date("QQQ") is None
    uow.observations.save_observations(
        [
            make_price_observation("QQQ", DAY, 400.0, provenance),
            make_price_observation("QQQ", NEXT, 405.0, provenance),
        ]
    )
    assert uow.observations.latest_observation_date("QQQ") == NEXT


def test_latest_observation_date_can_be_capped(
    uow: SQLiteUnitOfWork, provenance: Provenance
) -> None:
    """Replaying a past day may only see what that day could see."""
    uow.observations.save_observations(
        [
            make_price_observation("QQQ", DAY, 400.0, provenance),
            make_price_observation("QQQ", NEXT, 405.0, provenance),
        ]
    )
    assert uow.observations.latest_observation_date("QQQ", on_or_before=DAY) == DAY
    assert uow.observations.latest_observation_date("QQQ", on_or_before=NEXT) == NEXT
    before = DAY - timedelta(days=1)
    assert uow.observations.latest_observation_date("QQQ", on_or_before=before) is None


def test_quality_findings_are_recorded_not_corrected(uow: SQLiteUnitOfWork) -> None:
    # BACKTEST_SPEC.md 5.5.
    finding = DataQualityFinding(
        symbol="QLD",
        observation_date=DAY,
        check_name="proshares_price",
        status=DataQualityStatus.REVIEW,
        detail="close differs by 3.2%",
        primary_value=70.1,
        reference_value=67.9,
        reference_source="proshares",
    )
    uow.observations.save_finding(finding)
    uow.observations.save_finding(finding)  # idempotent

    open_findings = uow.observations.get_open_findings(symbol="QLD")
    assert len(open_findings) == 1
    assert open_findings[0].status is DataQualityStatus.REVIEW


# --------------------------------------------------------------- indicators


def test_indicator_values_are_keyed_by_their_parameters(uow: SQLiteUnitOfWork) -> None:
    uow.indicators.save_values(
        [
            IndicatorValue("rsi", DAY, 55.0, "QQQ", params={"window": 14}),
            IndicatorValue("rsi", DAY, 48.0, "QQQ", params={"window": 30}),
        ]
    )
    values = uow.indicators.get_values("rsi")
    assert sorted(v.value for v in values) == [48.0, 55.0]


def test_indicator_value_upsert(uow: SQLiteUnitOfWork) -> None:
    uow.indicators.save_values([IndicatorValue("rsi", DAY, 55.0, "QQQ", params={"window": 14})])
    uow.indicators.save_values([IndicatorValue("rsi", DAY, 56.5, "QQQ", params={"window": 14})])
    values = uow.indicators.get_values("rsi")
    assert len(values) == 1
    assert values[0].value == pytest.approx(56.5)


def test_indicator_scores_are_scoped_by_strategy_version(uow: SQLiteUnitOfWork) -> None:
    score = IndicatorScore("rsi_14", DAY, 61.0, "rolling_percentile", 504, raw_value=55.0)
    uow.indicators.save_scores([score], strategy_version="A")
    uow.indicators.save_scores([score], strategy_version="B")

    assert len(uow.indicators.get_scores(DAY, strategy_version="A")) == 1
    assert len(uow.indicators.get_scores(DAY, strategy_version="B")) == 1


def test_score_outside_the_domain_is_rejected_by_the_database(uow: SQLiteUnitOfWork) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        uow.connection.execute(
            "INSERT INTO indicator_scores (indicator_name, observation_date, strategy_version, "
            "score, normalization_method, created_at, updated_at) "
            "VALUES ('x','2024-01-03','v0',120.0,'bounded','t','t')"
        )


# ------------------------------------------------------------ market states


def _state(score: float = 45.0, regime: str = "Fear", **kwargs: object) -> MarketState:
    return MarketState(
        observation_date=kwargs.pop("observation_date", DAY),  # type: ignore[arg-type]
        composite_score=score,
        regime=regime,
        strategy_version=VERSION,
        **kwargs,  # type: ignore[arg-type]
    )


def _allocation(day: date = DAY, regime: str = "Fear") -> TargetAllocation:
    return TargetAllocation(
        observation_date=day,
        weights={Asset.QLD: 0.7, Asset.CASH: 0.3},
        strategy_version=VERSION,
        regime=regime,
    )


def test_state_and_allocation_round_trip(uow: SQLiteUnitOfWork) -> None:
    uow.states.save_state(_state(target_leverage=1.4), _allocation())

    state = uow.states.get_state(DAY, strategy_version=VERSION)
    assert state is not None
    assert state.regime == "Fear"
    assert state.target_leverage == pytest.approx(1.4)

    allocation = uow.states.get_allocation(DAY, strategy_version=VERSION)
    assert allocation is not None
    assert allocation.weight(Asset.QLD) == pytest.approx(0.7)
    assert allocation.target_leverage == pytest.approx(1.4)


def test_rerunning_a_day_updates_rather_than_duplicates(uow: SQLiteUnitOfWork) -> None:
    # ARCHITECTURE.md 11 — the core idempotency guarantee.
    first_id = uow.states.save_state(_state(45.0, "Fear", target_leverage=1.4), _allocation())
    second_id = uow.states.save_state(
        _state(52.0, "Neutral", target_leverage=1.4),
        TargetAllocation(
            observation_date=DAY,
            weights={Asset.QLD: 0.7, Asset.CASH: 0.3},
            strategy_version=VERSION,
            regime="Neutral",
        ),
    )
    assert first_id == second_id

    assert len(uow.states.get_state_history(strategy_version=VERSION)) == 1
    rows = uow.connection.execute("SELECT COUNT(*) AS c FROM target_allocations").fetchone()
    assert rows["c"] == 2  # QLD + CASH, not four


def test_a_rerun_that_degrades_to_unknown_clears_the_old_allocation(
    uow: SQLiteUnitOfWork,
) -> None:
    # CLAUDE_CODE_INITIAL_PROMPT.md 14: bad data must not leave advice standing.
    uow.states.save_state(_state(45.0, "Fear", target_leverage=1.4), _allocation())
    uow.states.save_state(
        MarketState.unknown(DAY, strategy_version=VERSION, reason_codes=("STALE:VIX",))
    )

    assert uow.states.get_allocation(DAY, strategy_version=VERSION) is None
    state = uow.states.get_state(DAY, strategy_version=VERSION)
    assert state is not None
    assert state.regime == UNKNOWN_REGIME
    assert state.target_leverage is None


def test_an_unknown_state_may_not_be_given_an_allocation(uow: SQLiteUnitOfWork) -> None:
    with pytest.raises(ValueError, match="UNKNOWN state"):
        uow.states.save_state(
            MarketState.unknown(DAY, strategy_version=VERSION, reason_codes=("x",)),
            _allocation(),
        )


def test_allocation_date_must_match_the_state_date(uow: SQLiteUnitOfWork) -> None:
    with pytest.raises(ValueError, match="does not match"):
        uow.states.save_state(_state(), _allocation(day=NEXT))


def test_previous_state_skips_unknown_days(uow: SQLiteUnitOfWork) -> None:
    uow.states.save_state(_state(45.0, "Fear", observation_date=date(2024, 1, 2)))
    uow.states.save_state(
        MarketState.unknown(DAY, strategy_version=VERSION, reason_codes=("STALE:VIX",))
    )

    previous = uow.states.get_previous_state(NEXT, strategy_version=VERSION)
    assert previous is not None
    assert previous.observation_date == date(2024, 1, 2)

    including_unknown = uow.states.get_previous_state(
        NEXT, strategy_version=VERSION, exclude_unknown=False
    )
    assert including_unknown is not None
    assert including_unknown.observation_date == DAY


def test_latest_state_and_history_window(uow: SQLiteUnitOfWork) -> None:
    for offset in range(5):
        day = DAY + timedelta(days=offset)
        uow.states.save_state(_state(40.0 + offset, "Fear", observation_date=day))

    latest = uow.states.get_latest_state(strategy_version=VERSION)
    assert latest is not None
    assert latest.observation_date == DAY + timedelta(days=4)

    window = uow.states.get_state_history(
        strategy_version=VERSION, start=DAY + timedelta(days=1), end=DAY + timedelta(days=3)
    )
    assert len(window) == 3


def test_database_forbids_leverage_on_an_unknown_regime(uow: SQLiteUnitOfWork) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        uow.connection.execute(
            "INSERT INTO market_states (observation_date, strategy_version, regime, "
            "target_leverage, created_at, updated_at) "
            "VALUES ('2024-01-03','v0','UNKNOWN',1.4,'t','t')"
        )


def test_allocation_rejects_unknown_assets(uow: SQLiteUnitOfWork) -> None:
    uow.states.save_state(_state())
    state_id = uow.connection.execute("SELECT id FROM market_states").fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        uow.connection.execute(
            "INSERT INTO target_allocations (market_state_id, observation_date, "
            "strategy_version, asset, weight, created_at, updated_at) "
            "VALUES (?, '2024-01-03', ?, 'SPY', 1.0, 't', 't')",
            (state_id, VERSION),
        )


def test_allocations_are_cascade_deleted_with_their_state(uow: SQLiteUnitOfWork) -> None:
    state_id = uow.states.save_state(_state(), _allocation())
    uow.connection.execute("DELETE FROM market_states WHERE id = ?", (state_id,))
    remaining = uow.connection.execute(
        "SELECT COUNT(*) AS c FROM target_allocations"
    ).fetchone()["c"]
    assert remaining == 0


# ------------------------------------------------------------------- events


def test_regime_event_is_inserted_once(uow: SQLiteUnitOfWork) -> None:
    event = RegimeEvent(
        event_date=DAY,
        previous_regime="Neutral",
        new_regime="Fear",
        previous_score=55.0,
        new_score=45.0,
        reason_codes=("SCORE_CROSSED_BOUNDARY",),
        strategy_version=VERSION,
    )
    assert uow.events.save_regime_event(event) is True
    assert uow.events.save_regime_event(event) is False
    assert len(uow.events.get_regime_events(strategy_version=VERSION)) == 1


def test_alert_deduplication_prevents_a_second_send(uow: SQLiteUnitOfWork) -> None:
    # PRD.md 16 / TASK-122.
    alert = AlertEvent(
        event_date=DAY,
        event_type=EventType.REGIME_CHANGED,
        severity="WARNING",
        title="Regime changed",
        body="Neutral -> Fear",
        strategy_version=VERSION,
    )
    assert uow.events.save_alert(alert) is True
    assert uow.events.save_alert(alert) is False
    assert len(uow.events.get_pending_alerts()) == 1


def test_a_different_day_is_a_different_alert(uow: SQLiteUnitOfWork) -> None:
    for day in (DAY, NEXT):
        assert uow.events.save_alert(
            AlertEvent(
                event_date=day,
                event_type=EventType.REGIME_CHANGED,
                severity="WARNING",
                title="t",
                body="b",
                strategy_version=VERSION,
            )
        )
    assert len(uow.events.get_alerts()) == 2


def test_alert_delivery_is_recorded(uow: SQLiteUnitOfWork) -> None:
    alert = AlertEvent(
        event_date=DAY,
        event_type=EventType.EXTREME_FEAR,
        severity="WARNING",
        title="t",
        body="b",
        strategy_version=VERSION,
    )
    uow.events.save_alert(alert)
    uow.events.mark_alert_delivered(alert.delivered("telegram"))

    stored = uow.events.get_alert(alert.dedupe_key)
    assert stored is not None
    assert stored.delivery_status == "SENT"
    assert stored.provider == "telegram"
    assert not uow.events.get_pending_alerts()
    assert uow.events.last_alert_date(EventType.EXTREME_FEAR, strategy_version=VERSION) == DAY


def test_last_alert_date_ignores_undelivered_alerts(uow: SQLiteUnitOfWork) -> None:
    uow.events.save_alert(
        AlertEvent(
            event_date=DAY,
            event_type=EventType.DATA_FAILURE,
            severity="CRITICAL",
            title="t",
            body="b",
            strategy_version=VERSION,
        )
    )
    assert uow.events.last_alert_date(EventType.DATA_FAILURE, strategy_version=VERSION) is None


# --------------------------------------------------------------- strategies


def test_only_one_strategy_version_can_be_active(uow: SQLiteUnitOfWork) -> None:
    for name in ("v0.0-placeholder", "v0.1-research"):
        uow.strategies.save_version(
            StrategyVersionRecord(strategy_version=name, parameter_status="RESEARCH")
        )
    uow.strategies.activate("v0.0-placeholder")
    uow.strategies.activate("v0.1-research")

    active = uow.strategies.get_active_version()
    assert active is not None
    assert active.strategy_version == "v0.1-research"
    count = uow.connection.execute(
        "SELECT COUNT(*) AS c FROM strategy_versions WHERE is_active = 1"
    ).fetchone()["c"]
    assert count == 1


def test_activating_an_unknown_version_fails(uow: SQLiteUnitOfWork) -> None:
    with pytest.raises(LookupError, match="unknown strategy_version"):
        uow.strategies.activate("nope")


def test_frozen_version_must_declare_frozen_at(uow: SQLiteUnitOfWork) -> None:
    # TASK-101 — enforced by the database, not only by the config schema.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        uow.connection.execute(
            "INSERT INTO strategy_versions (strategy_version, parameter_status, manifest, "
            "created_at, updated_at) VALUES ('v1.0-frozen','FROZEN','{}','t','t')"
        )


def test_strategy_manifest_round_trips(uow: SQLiteUnitOfWork) -> None:
    uow.strategies.save_version(
        StrategyVersionRecord(
            strategy_version="v1.0-frozen",
            parameter_status="FROZEN",
            frozen_at=datetime(2026, 1, 1, tzinfo=UTC),
            manifest={"regime": {"count": 5}},
            is_active=True,
        )
    )
    record = uow.strategies.get_version("v1.0-frozen")
    assert record is not None
    assert record.manifest["regime"]["count"] == 5
    assert record.is_active
    assert record.frozen_at is not None


def test_pipeline_runs_are_upserted_by_run_id(uow: SQLiteUnitOfWork) -> None:
    run = PipelineRun(
        run_id="run-1", run_date=DAY, started_at=datetime(2024, 1, 3, 22, tzinfo=UTC)
    )
    uow.strategies.save_run(run)
    uow.strategies.save_run(run.completed(PipelineStatus.DATA_FAILURE, error="VIX missing"))

    runs = uow.strategies.get_runs(run_date=DAY)
    assert len(runs) == 1
    assert runs[0].status is PipelineStatus.DATA_FAILURE
    assert runs[0].error_message == "VIX missing"
    assert uow.strategies.last_successful_run() is None

    uow.strategies.save_run(
        PipelineRun(
            run_id="run-2",
            run_date=NEXT,
            started_at=datetime(2024, 1, 4, 22, tzinfo=UTC),
            status=PipelineStatus.SUCCESS,
        )
    )
    last = uow.strategies.last_successful_run()
    assert last is not None
    assert last.run_id == "run-2"


# ------------------------------------------------------------ transactions


def test_a_failed_unit_of_work_rolls_everything_back(
    db_path: Path, provenance: Provenance
) -> None:
    class BoomError(RuntimeError):
        pass

    with pytest.raises(BoomError), SQLiteUnitOfWork(db_path) as unit:
        unit.observations.save_observations(
            [make_price_observation("QQQ", DAY, 400.0, provenance)]
        )
        unit.states.save_state(_state())
        raise BoomError

    with SQLiteUnitOfWork(db_path) as unit:
        assert unit.observations.get_observation("QQQ", DAY) is None
        assert unit.states.get_state(DAY, strategy_version=VERSION) is None


def test_explicit_rollback_keeps_earlier_committed_work(
    db_path: Path, provenance: Provenance
) -> None:
    with SQLiteUnitOfWork(db_path) as unit:
        unit.observations.save_observations(
            [make_price_observation("QQQ", DAY, 400.0, provenance)]
        )
        unit.commit()

        unit.observations.save_observations(
            [make_price_observation("QQQ", NEXT, 405.0, provenance)]
        )
        unit.rollback()

    with SQLiteUnitOfWork(db_path) as unit:
        dates = [row.observation_date for row in unit.observations.get_observations("QQQ")]
        assert dates == [DAY]


def test_a_successful_unit_of_work_commits(db_path: Path, provenance: Provenance) -> None:
    with SQLiteUnitOfWork(db_path) as unit:
        unit.observations.save_observations(
            [make_price_observation("QQQ", DAY, 400.0, provenance)]
        )

    with SQLiteUnitOfWork(db_path) as unit:
        assert unit.observations.get_observation("QQQ", DAY) is not None


def test_unit_of_work_is_inert_once_closed(db_path: Path) -> None:
    unit = SQLiteUnitOfWork(db_path)
    with unit:
        pass
    with pytest.raises(RuntimeError, match="not active"):
        _ = unit.connection

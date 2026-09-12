"""TASK-020 — domain model invariants."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from regime_monitor.constants import (
    UNKNOWN_REGIME,
    Asset,
    DataQualityStatus,
    EventType,
    PipelineStatus,
)
from regime_monitor.data.models import (
    AlertEvent,
    DataQualityFinding,
    DomainError,
    IndicatorValue,
    MarketObservation,
    MarketState,
    PipelineRun,
    Provenance,
    RegimeEvent,
    TargetAllocation,
)
from tests.conftest import make_price_observation

DAY = date(2024, 1, 3)


# ---------------------------------------------------------------- provenance


def test_provenance_requires_utc(provenance: Provenance) -> None:
    assert provenance.retrieved_at.tzinfo is not None
    with pytest.raises(DomainError, match="timezone-aware"):
        Provenance(
            provider_library="x",
            provider_library_version="1",
            underlying_source="y",
            retrieved_at=datetime(2024, 1, 3, 23, 0),
        )


def test_provenance_separates_library_from_source(provenance: Provenance) -> None:
    # PRD.md 6.5: the collection library is not the data source.
    assert provenance.provider_library == "FinanceDataReader"
    assert provenance.underlying_source != provenance.provider_library


# --------------------------------------------------------------- observations


def test_an_observation_needs_a_close_or_a_value(provenance: Provenance) -> None:
    with pytest.raises(DomainError, match=r"close .* or a value"):
        MarketObservation(
            symbol="QQQ",
            observation_date=DAY,
            availability_datetime=datetime(2024, 1, 3, 21, tzinfo=UTC),
            provenance=provenance,
        )


def test_observation_rejects_impossible_prices(provenance: Provenance) -> None:
    with pytest.raises(DomainError, match="close=-1"):
        MarketObservation(
            symbol="QQQ",
            observation_date=DAY,
            availability_datetime=datetime(2024, 1, 3, 21, tzinfo=UTC),
            provenance=provenance,
            close=-1.0,
        )
    with pytest.raises(DomainError, match="high < low"):
        MarketObservation(
            symbol="QQQ",
            observation_date=DAY,
            availability_datetime=datetime(2024, 1, 3, 21, tzinfo=UTC),
            provenance=provenance,
            close=100.0,
            high=90.0,
            low=110.0,
        )


def test_availability_gate(provenance: Provenance) -> None:
    # BACKTEST_SPEC.md 4: a decision at T may only read what was knowable at T.
    obs = make_price_observation("QQQ", DAY, 400.0, provenance)
    assert not obs.is_available_at(datetime(2024, 1, 3, 20, tzinfo=UTC))
    assert obs.is_available_at(datetime(2024, 1, 3, 21, tzinfo=UTC))
    assert obs.is_available_at(datetime(2024, 1, 4, 12, tzinfo=UTC))


def test_flagging_returns_a_copy(provenance: Provenance) -> None:
    obs = make_price_observation("QQQ", DAY, 400.0, provenance)
    flagged = obs.flagged(DataQualityStatus.REVIEW, "ProShares mismatch")
    assert obs.quality_status is DataQualityStatus.OK
    assert flagged.quality_status is DataQualityStatus.REVIEW
    assert flagged.note == "ProShares mismatch"


def test_primary_value_prefers_close_then_value(provenance: Provenance) -> None:
    price = make_price_observation("QQQ", DAY, 400.0, provenance)
    assert price.primary_value == 400.0
    assert price.is_price

    scalar = MarketObservation(
        symbol="VIX",
        observation_date=DAY,
        availability_datetime=datetime(2024, 1, 3, 21, tzinfo=UTC),
        provenance=provenance,
        value=13.5,
    )
    assert scalar.primary_value == 13.5
    assert not scalar.is_price


# ----------------------------------------------------------------- indicators


def test_indicator_params_key_is_canonical() -> None:
    a = IndicatorValue("rsi", DAY, 55.0, "QQQ", params={"window": 14, "kind": "wilder"})
    b = IndicatorValue("rsi", DAY, 55.0, "QQQ", params={"kind": "wilder", "window": 14})
    assert a.params_key == b.params_key

    c = IndicatorValue("rsi", DAY, 55.0, "QQQ", params={"window": 30})
    assert c.params_key != a.params_key


# --------------------------------------------------------------- market state


def test_state_score_must_be_inside_the_score_domain() -> None:
    with pytest.raises(DomainError, match="outside"):
        MarketState(
            observation_date=DAY,
            composite_score=140.0,
            regime="Bull",
            strategy_version="v0",
        )


def test_unknown_state_carries_no_advice() -> None:
    # ARCHITECTURE.md 15 / CLAUDE_CODE_INITIAL_PROMPT.md 14.
    state = MarketState.unknown(
        DAY, strategy_version="v0", reason_codes=("MANDATORY_DATA_MISSING:VIX",)
    )
    assert state.regime == UNKNOWN_REGIME
    assert state.composite_score is None
    assert state.target_leverage is None
    assert state.is_unknown
    assert state.data_quality_status is DataQualityStatus.MISSING

    with pytest.raises(DomainError, match="must not carry a target leverage"):
        MarketState(
            observation_date=DAY,
            composite_score=None,
            regime=UNKNOWN_REGIME,
            target_leverage=1.4,
            strategy_version="v0",
        )


def test_a_known_regime_requires_a_score() -> None:
    with pytest.raises(DomainError, match="requires a composite score"):
        MarketState(
            observation_date=DAY, composite_score=None, regime="Bull", strategy_version="v0"
        )


def test_state_reports_change_and_delta() -> None:
    state = MarketState(
        observation_date=DAY,
        composite_score=42.0,
        regime="Fear",
        previous_regime="Neutral",
        previous_score=55.0,
        strategy_version="v0",
    )
    assert state.regime_changed
    assert state.score_change == pytest.approx(-13.0)


# ---------------------------------------------------------------- allocation


def test_target_leverage_is_the_weighted_sleeve_multiple() -> None:
    # PRD.md 9: 20% QQQ + 60% QLD = 1.40x
    allocation = TargetAllocation(
        observation_date=DAY,
        weights={Asset.QQQ: 0.20, Asset.QLD: 0.60, Asset.CASH: 0.20},
        strategy_version="v0",
        regime="Neutral",
    )
    assert allocation.target_leverage == pytest.approx(1.40)
    assert allocation.market_exposure == pytest.approx(0.80)
    assert allocation.weight(Asset.TQQQ) == 0.0


def test_allocation_must_be_a_portfolio() -> None:
    with pytest.raises(DomainError, match="sum to"):
        TargetAllocation(
            observation_date=DAY,
            weights={Asset.QLD: 0.7},
            strategy_version="v0",
            regime="Neutral",
        )
    with pytest.raises(DomainError, match="negative weight"):
        TargetAllocation(
            observation_date=DAY,
            weights={Asset.QLD: 1.2, Asset.CASH: -0.2},
            strategy_version="v0",
            regime="Neutral",
        )


# -------------------------------------------------------------------- events


def test_regime_event_requires_an_actual_change() -> None:
    with pytest.raises(DomainError, match="actual change"):
        RegimeEvent(
            event_date=DAY,
            previous_regime="Fear",
            new_regime="Fear",
            previous_score=30.0,
            new_score=31.0,
            reason_codes=(),
            strategy_version="v0",
        )


def test_alert_dedupe_key_is_date_type_and_strategy() -> None:
    # ARCHITECTURE.md 11.
    alert = AlertEvent(
        event_date=DAY,
        event_type=EventType.REGIME_CHANGED,
        severity="WARNING",
        title="t",
        body="b",
        strategy_version="v1.0-frozen",
    )
    assert alert.dedupe_key == "2024-01-03|REGIME_CHANGED|v1.0-frozen"

    other_day = AlertEvent(
        event_date=date(2024, 1, 4),
        event_type=EventType.REGIME_CHANGED,
        severity="WARNING",
        title="t",
        body="b",
        strategy_version="v1.0-frozen",
    )
    assert alert.dedupe_key != other_day.dedupe_key


def test_alert_delivery_transitions_are_immutable() -> None:
    alert = AlertEvent(
        event_date=DAY,
        event_type=EventType.DATA_FAILURE,
        severity="CRITICAL",
        title="t",
        body="b",
        strategy_version="v0",
    )
    sent = alert.delivered("telegram")
    assert alert.delivery_status == "PENDING"
    assert sent.delivery_status == "SENT"
    assert sent.sent_at is not None

    failed = alert.failed("telegram", "429 too many requests")
    assert failed.delivery_status == "FAILED"
    assert failed.error


# ------------------------------------------------------------- pipeline runs


def test_pipeline_run_completion() -> None:
    run = PipelineRun(
        run_id="r1", run_date=DAY, started_at=datetime(2024, 1, 3, 22, tzinfo=UTC)
    )
    assert run.succeeded
    failed = run.completed(PipelineStatus.DATA_FAILURE, error="VIX missing")
    assert not failed.succeeded
    assert failed.finished_at is not None
    assert failed.error_message == "VIX missing"


def test_a_finding_records_a_problem_only() -> None:
    with pytest.raises(DomainError, match="OK is not a finding"):
        DataQualityFinding(
            symbol="QLD",
            observation_date=DAY,
            check_name="proshares_price",
            status=DataQualityStatus.OK,
            detail="fine",
        )

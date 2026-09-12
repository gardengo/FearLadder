"""TASK-050 .. TASK-052 — classification, transition logic and regime events."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
from pandas import Series

from regime_monitor.config.schema import RegimeSpec, TransitionSpec
from regime_monitor.constants import UNKNOWN_REGIME
from regime_monitor.regime.classifier import RegimeClassifier, RegimeError, RegimeScale
from regime_monitor.regime.transition import (
    ReasonCode,
    TransitionEngine,
    TransitionState,
    build_regime_event,
)

START = date(2024, 1, 1)

FIVE = RegimeScale(
    labels=("Capitulation", "Fear", "Neutral", "Bull", "Overheated"),
    boundaries=(20.0, 40.0, 60.0, 80.0),
)
THREE = RegimeScale(labels=("Fear", "Neutral", "Greed"), boundaries=(33.0, 67.0))


def _days(count: int) -> list[date]:
    return [START + timedelta(days=offset) for offset in range(count)]


def _scores(values: list[float | None]) -> Series:
    return Series(values, index=pd.Index(_days(len(values)), name="observation_date"))


# ------------------------------------------------------------------ TASK-050


def test_every_score_lands_in_exactly_one_regime() -> None:
    for score in range(0, 101):
        assert FIVE.classify(float(score)) in FIVE.labels


def test_boundaries_belong_to_the_greedier_side() -> None:
    # Lower-inclusive / upper-exclusive, stated once so nothing has to guess.
    assert FIVE.classify(19.99) == "Capitulation"
    assert FIVE.classify(20.0) == "Fear"
    assert FIVE.classify(39.99) == "Fear"
    assert FIVE.classify(40.0) == "Neutral"


def test_the_extremes_are_reachable() -> None:
    assert FIVE.classify(0.0) == "Capitulation"
    assert FIVE.classify(100.0) == "Overheated"


def test_a_missing_score_is_unknown_not_a_regime() -> None:
    # ARCHITECTURE.md 15 — no score, no advice.
    assert FIVE.classify(None) == UNKNOWN_REGIME
    assert FIVE.classify(float("nan")) == UNKNOWN_REGIME


def test_a_score_outside_the_domain_is_an_error_not_a_guess() -> None:
    with pytest.raises(RegimeError, match="outside"):
        FIVE.classify(101.0)


@pytest.mark.parametrize("count", [3, 5, 7, 9])
def test_every_configured_stage_count_is_supported(count: int) -> None:
    # BACKTEST_SPEC.md 12 — all four candidates must work.
    labels = tuple(f"R{i}" for i in range(count))
    step = 100.0 / count
    boundaries = tuple(step * (i + 1) for i in range(count - 1))
    scale = RegimeScale(labels=labels, boundaries=boundaries)
    assert scale.count == count
    assert scale.classify(0.0) == labels[0]
    assert scale.classify(100.0) == labels[-1]


def test_scale_rejects_inconsistent_shapes() -> None:
    with pytest.raises(RegimeError, match="need 2 boundaries"):
        RegimeScale(labels=("a", "b", "c"), boundaries=(50.0,))
    with pytest.raises(RegimeError, match="ascending"):
        RegimeScale(labels=("a", "b", "c"), boundaries=(60.0, 40.0))
    with pytest.raises(RegimeError, match="reserved"):
        RegimeScale(labels=("a", UNKNOWN_REGIME), boundaries=(50.0,))


def test_a_boundary_on_the_edge_would_make_a_band_unreachable() -> None:
    with pytest.raises(RegimeError, match="unreachable"):
        RegimeScale(labels=("a", "b"), boundaries=(0.0,))


def test_bands_report_their_own_range() -> None:
    assert FIVE.band("Capitulation") == (0.0, 20.0)
    assert FIVE.band("Neutral") == (40.0, 60.0)
    assert FIVE.band("Overheated") == (80.0, 100.0)


def test_scale_knows_which_way_is_fear() -> None:
    assert FIVE.is_more_fearful("Fear", "Bull")
    assert not FIVE.is_more_fearful("Bull", "Fear")
    assert FIVE.most_fearful == "Capitulation"
    assert FIVE.most_greedy == "Overheated"


def test_unresolved_regime_config_blocks_classification() -> None:
    with pytest.raises(RegimeError, match="unresolved research"):
        RegimeScale.from_spec(RegimeSpec())


def test_classifier_maps_a_whole_series(placeholder_config) -> None:
    classifier = RegimeClassifier.from_spec(placeholder_config.strategy.regime)
    regimes = classifier.classify_series(_scores([10.0, 50.0, None, 90.0]))
    assert list(regimes) == ["Capitulation", "Neutral", UNKNOWN_REGIME, "Overheated"]


# ------------------------------------------------------------------ TASK-051


def _engine(confirmation: int = 1, hysteresis: float = 0.0, duration: int = 1) -> TransitionEngine:
    return TransitionEngine(
        scale=FIVE,
        confirmation_days=confirmation,
        hysteresis=hysteresis,
        minimum_duration_days=duration,
    )


def test_the_first_usable_score_establishes_the_regime() -> None:
    decision = _engine().step(None, START, 50.0)
    assert decision.regime == "Neutral"
    assert not decision.changed
    assert decision.reason_codes == (ReasonCode.INITIAL,)


def test_with_no_brakes_a_new_band_changes_the_regime_at_once() -> None:
    engine = _engine()
    decisions = engine.run(_scores([50.0, 50.0, 10.0]))
    assert [d.regime for d in decisions] == ["Neutral", "Neutral", "Capitulation"]
    assert decisions[-1].changed
    assert ReasonCode.CONFIRMED in decisions[-1].reason_codes
    assert "MOVED_TOWARD_FEAR" in decisions[-1].reason_codes


def test_confirmation_ignores_a_one_day_spike() -> None:
    engine = _engine(confirmation=3)
    decisions = engine.run(_scores([50.0, 50.0, 10.0, 50.0, 50.0]))
    assert all(not d.changed for d in decisions)
    assert [d.regime for d in decisions] == ["Neutral"] * 5
    assert any(ReasonCode.PENDING in code for code in decisions[2].reason_codes)


def test_confirmation_accepts_a_persistent_move() -> None:
    engine = _engine(confirmation=3)
    decisions = engine.run(_scores([50.0, 10.0, 10.0, 10.0]))
    assert [d.changed for d in decisions] == [False, False, False, True]
    assert decisions[-1].regime == "Capitulation"


def test_an_interruption_restarts_the_confirmation_count() -> None:
    engine = _engine(confirmation=3)
    decisions = engine.run(_scores([50.0, 10.0, 10.0, 50.0, 10.0, 10.0]))
    assert all(not d.changed for d in decisions), "the streak was broken and must restart"


def test_hysteresis_blocks_a_move_that_only_grazes_the_boundary() -> None:
    # 39.0 is inside Fear, but only 1 point past the 40.0 cut.
    engine = _engine(hysteresis=5.0)
    decisions = engine.run(_scores([50.0, 39.0, 39.0, 39.0]))
    assert all(d.regime == "Neutral" for d in decisions)
    assert any(ReasonCode.BLOCKED_HYSTERESIS in code for code in decisions[1].reason_codes)


def test_hysteresis_allows_a_decisive_move() -> None:
    engine = _engine(hysteresis=5.0)
    decisions = engine.run(_scores([50.0, 34.0]))
    assert decisions[-1].changed
    assert decisions[-1].regime == "Fear"


def test_hysteresis_is_symmetric() -> None:
    engine = _engine(hysteresis=5.0)
    # Toward greed: must clear the upper cut (60.0) by 5.
    assert not engine.run(_scores([50.0, 61.0]))[-1].changed
    assert engine.run(_scores([50.0, 66.0]))[-1].changed


def test_hysteresis_stops_the_oscillation_it_exists_for() -> None:
    engine = _engine(hysteresis=5.0)
    # A score hovering either side of 40.0 must not flip the regime each day.
    decisions = engine.run(_scores([50.0, 39.0, 41.0, 39.0, 41.0, 39.0]))
    assert {d.regime for d in decisions} == {"Neutral"}


def test_minimum_duration_holds_a_fresh_regime() -> None:
    engine = _engine(duration=5)
    decisions = engine.run(_scores([50.0, 10.0, 50.0, 50.0]))
    # Day 2 would otherwise change; the regime is only 2 days old.
    assert not decisions[1].changed
    assert any(
        ReasonCode.BLOCKED_MIN_DURATION in code for code in decisions[1].reason_codes
    )


def test_minimum_duration_releases_after_long_enough() -> None:
    engine = _engine(duration=3)
    decisions = engine.run(_scores([50.0, 50.0, 50.0, 50.0, 10.0]))
    assert decisions[-1].changed


def test_the_three_brakes_compose() -> None:
    engine = _engine(confirmation=2, hysteresis=5.0, duration=3)
    scores = _scores([50.0, 50.0, 50.0, 39.0, 39.0, 30.0, 30.0])
    decisions = engine.run(scores)
    changed = [d for d in decisions if d.changed]
    assert len(changed) == 1
    assert changed[0].regime == "Fear"
    # 39.0 never clears hysteresis, so only the decisive 30.0 days count.
    assert changed[0].observation_date == START + timedelta(days=6)


def test_an_unknown_day_holds_the_regime_without_confirming_anything() -> None:
    engine = _engine(confirmation=2)
    decisions = engine.run(_scores([50.0, 10.0, None, 10.0]))
    assert decisions[2].regime == "Neutral"
    assert decisions[2].is_unknown_input
    assert ReasonCode.UNKNOWN_INPUT in decisions[2].reason_codes
    assert not any(d.changed for d in decisions[:3])
    # Confirmation resumes rather than being cancelled by the gap.
    assert decisions[3].changed


def test_a_run_that_starts_with_unknown_days_waits_for_a_score() -> None:
    engine = _engine()
    decisions = engine.run(_scores([None, None, 50.0]))
    assert [d.regime for d in decisions] == [UNKNOWN_REGIME, UNKNOWN_REGIME, "Neutral"]
    assert decisions[-1].reason_codes == (ReasonCode.INITIAL,)


def test_replay_is_deterministic() -> None:
    # TASK-102's frozen regression depends on this.
    engine = _engine(confirmation=2, hysteresis=3.0, duration=4)
    scores = _scores([50.0, 45.0, 30.0, 30.0, 30.0, 70.0, 85.0, 85.0, 85.0, 20.0])
    first = engine.to_frame(engine.run(scores))
    second = engine.to_frame(engine.run(scores))
    pd.testing.assert_frame_equal(first, second)


def test_stepping_day_by_day_equals_replaying_the_history() -> None:
    engine = _engine(confirmation=2, hysteresis=3.0, duration=2)
    scores = _scores([50.0, 30.0, 30.0, 30.0, 75.0, 85.0, 85.0])

    state: TransitionState | None = None
    stepwise = []
    for day, value in scores.items():
        decision = engine.step(state, day, float(value))
        stepwise.append(decision.regime)
        state = decision.state

    assert stepwise == [d.regime for d in engine.run(scores)]


def test_transition_engine_rejects_unresolved_parameters() -> None:
    with pytest.raises(RegimeError, match="unresolved research"):
        TransitionEngine.from_spec(FIVE, TransitionSpec())


def test_transition_engine_rejects_nonsense_parameters() -> None:
    with pytest.raises(RegimeError, match="confirmation_days"):
        TransitionEngine(scale=FIVE, confirmation_days=0, hysteresis=0, minimum_duration_days=1)
    with pytest.raises(RegimeError, match="hysteresis"):
        TransitionEngine(scale=FIVE, confirmation_days=1, hysteresis=-1, minimum_duration_days=1)


def test_engine_builds_from_the_placeholder_profile(placeholder_config) -> None:
    scale = RegimeScale.from_spec(placeholder_config.strategy.regime)
    engine = TransitionEngine.from_spec(scale, placeholder_config.strategy.transition)
    assert engine.confirmation_days == 2
    assert engine.hysteresis == pytest.approx(5.0)
    assert engine.minimum_duration_days == 5


def test_a_three_stage_scale_works_the_same_way() -> None:
    engine = TransitionEngine(
        scale=THREE, confirmation_days=1, hysteresis=0.0, minimum_duration_days=1
    )
    decisions = engine.run(_scores([50.0, 20.0, 80.0]))
    assert [d.regime for d in decisions] == ["Neutral", "Fear", "Greed"]


# ------------------------------------------------------------------ TASK-052


def test_a_confirmed_change_produces_an_event() -> None:
    engine = _engine()
    decisions = engine.run(_scores([50.0, 10.0]))

    event = build_regime_event(
        decisions[-1], strategy_version="v0.0-placeholder", previous_score=50.0
    )
    assert event is not None
    assert event.event_date == START + timedelta(days=1)
    assert event.previous_regime == "Neutral"
    assert event.new_regime == "Capitulation"
    assert event.previous_score == pytest.approx(50.0)
    assert event.new_score == pytest.approx(10.0)
    assert ReasonCode.CONFIRMED in event.reason_codes
    assert event.strategy_version == "v0.0-placeholder"


def test_no_change_produces_no_event() -> None:
    engine = _engine()
    decisions = engine.run(_scores([50.0, 55.0]))
    assert build_regime_event(decisions[-1], strategy_version="v0") is None


def test_the_bootstrap_day_is_not_an_event() -> None:
    # Establishing the first regime is not a transition anyone should be alerted to.
    decision = _engine().step(None, START, 50.0)
    assert build_regime_event(decision, strategy_version="v0") is None

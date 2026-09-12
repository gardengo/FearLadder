"""TASK-060 .. TASK-063 — allocation, target leverage and the TQQQ gate."""

from __future__ import annotations

from datetime import date

import pytest

from regime_monitor.allocation.engine import (
    AllocationDecision,
    AllocationEngine,
    AllocationError,
)
from regime_monitor.allocation.tqqq_gate import (
    BottomConfirmationRule,
    GateConfigurationError,
    GateContext,
    ThresholdRule,
    TqqqGate,
)
from regime_monitor.config.schema import AllocationConstraints, TqqqGateSpec
from regime_monitor.constants import UNKNOWN_REGIME, Asset

DAY = date(2024, 3, 16)


def _gate(**overrides) -> TqqqGate:
    rules = overrides.pop(
        "rules",
        (
            ThresholdRule("deep_drawdown", "drawdown_52w", 0.20, at_least=True),
            ThresholdRule("extreme_fear", "cnn_fear_greed", 25.0, at_least=False),
            ThresholdRule("high_vix", "vix_percentile", 0.90, at_least=True),
            ThresholdRule("oversold_rsi", "rsi_14", 30.0, at_least=False),
        ),
    )
    return TqqqGate(
        rules=rules,
        required_rules=overrides.pop("required_rules", ("deep_drawdown", "extreme_fear")),
        min_confirmations=overrides.pop("min_confirmations", 3),
        **overrides,
    )


def _context(**values: float | None) -> GateContext:
    return GateContext(observation_date=DAY, indicator_values=values, regime="Capitulation")


def _engine(**overrides) -> AllocationEngine:
    return AllocationEngine(
        mappings=overrides.pop(
            "mappings",
            {
                "Capitulation": {Asset.QLD: 0.7, Asset.TQQQ: 0.3},
                "Neutral": {Asset.QLD: 0.7, Asset.CASH: 0.3},
                "Overheated": {Asset.QQQ: 0.5, Asset.QLD: 0.2, Asset.CASH: 0.3},
            },
        ),
        constraints=overrides.pop("constraints", AllocationConstraints()),
        gate=overrides.pop("gate", _gate()),
        strategy_version=overrides.pop("strategy_version", "v0.0-placeholder"),
        **overrides,
    )


# ------------------------------------------------------------------ TASK-063


def test_threshold_rules_satisfy_the_interface() -> None:
    rule = ThresholdRule("deep_drawdown", "drawdown_52w", 0.20, at_least=True)
    assert isinstance(rule, BottomConfirmationRule)


def test_a_rule_passes_and_explains_itself() -> None:
    rule = ThresholdRule("deep_drawdown", "drawdown_52w", 0.20, at_least=True)
    outcome = rule.evaluate(_context(drawdown_52w=0.35))
    assert outcome.passed
    assert "0.35 >= 0.2" in outcome.detail
    assert outcome.observed == pytest.approx(0.35)


def test_a_rule_fails_and_explains_itself() -> None:
    rule = ThresholdRule("oversold_rsi", "rsi_14", 30.0, at_least=False)
    outcome = rule.evaluate(_context(rsi_14=55.0))
    assert not outcome.passed
    assert "fails <=" in outcome.detail


def test_a_rule_whose_indicator_is_missing_cannot_confirm() -> None:
    """Absent evidence is not evidence.

    Buying the 3x sleeve because an indicator failed to load is the single
    worst outcome available, so a missing input never counts as a pass.
    """
    rule = ThresholdRule("high_vix", "vix_percentile", 0.90, at_least=True)
    outcome = rule.evaluate(_context())
    assert not outcome.passed
    assert not outcome.evaluable
    assert "unavailable" in outcome.detail


def test_fear_alone_does_not_open_the_gate() -> None:
    # PRD.md 10 — the whole point of separating fear from bottom confirmation.
    decision = _gate().evaluate(_context(cnn_fear_greed=5.0))
    assert not decision.allowed
    assert "TQQQ_GATE_BLOCKED" in decision.reason_codes()


def test_the_gate_opens_on_broad_confirmation() -> None:
    decision = _gate().evaluate(
        _context(drawdown_52w=0.35, cnn_fear_greed=8.0, vix_percentile=0.97, rsi_14=22.0)
    )
    assert decision.allowed
    assert decision.confirmations == 4
    assert "TQQQ_GATE_PASSED" in decision.reason_codes()


def test_a_failing_required_rule_vetoes_regardless_of_the_count() -> None:
    decision = _gate(min_confirmations=2).evaluate(
        _context(drawdown_52w=0.05, cnn_fear_greed=8.0, vix_percentile=0.97, rsi_14=22.0)
    )
    assert not decision.allowed
    assert decision.confirmations == 3
    assert decision.required_failed == ("deep_drawdown",)
    assert any("REQUIRED_RULE_FAILED" in code for code in decision.reason_codes())


def test_too_few_confirmations_vetoes_even_with_required_rules_passing() -> None:
    decision = _gate(min_confirmations=4).evaluate(
        _context(drawdown_52w=0.35, cnn_fear_greed=8.0, vix_percentile=0.10, rsi_14=55.0)
    )
    assert not decision.allowed
    assert any("INSUFFICIENT_CONFIRMATIONS" in code for code in decision.reason_codes())


def test_a_disabled_gate_never_blocks() -> None:
    gate = TqqqGate(rules=(), required_rules=(), min_confirmations=1, enabled=False)
    decision = gate.evaluate(_context())
    assert decision.allowed
    assert decision.disabled


def test_gate_rejects_an_unreachable_configuration() -> None:
    with pytest.raises(GateConfigurationError, match="could never open"):
        _gate(min_confirmations=99)
    with pytest.raises(GateConfigurationError, match="do not exist"):
        _gate(required_rules=("imaginary_rule",))


def test_gate_builds_from_the_placeholder_profile(placeholder_config) -> None:
    gate = TqqqGate.from_spec(placeholder_config.strategy.tqqq_gate)
    assert {rule.name for rule in gate.rules} == set(
        placeholder_config.strategy.tqqq_gate.candidate_rules
    )
    assert gate.required_rules == ("deep_drawdown", "extreme_fear")


def test_gate_refuses_unresolved_research_parameters() -> None:
    with pytest.raises(GateConfigurationError, match="unresolved research"):
        TqqqGate.from_spec(TqqqGateSpec(enabled=True))


def test_a_rule_needs_exactly_one_bound() -> None:
    spec = TqqqGateSpec(
        enabled=True,
        required_rules=("x",),
        min_confirmations=1,
        candidate_rules=("x",),
        rule_params={"x": {"indicator": "rsi_14", "min_value": 1.0, "max_value": 2.0}},
    )
    with pytest.raises(GateConfigurationError, match="exactly one"):
        TqqqGate.from_spec(spec)


def test_a_rule_with_a_null_threshold_is_rejected() -> None:
    spec = TqqqGateSpec(
        enabled=True,
        required_rules=("x",),
        min_confirmations=1,
        candidate_rules=("x",),
        rule_params={"x": {"indicator": "rsi_14", "min_value": None}},
    )
    with pytest.raises(GateConfigurationError, match="unresolved research parameter"):
        TqqqGate.from_spec(spec)


# ------------------------------------------------------------ TASK-060 / 061


def test_allocation_returns_the_configured_weights() -> None:
    decision = _engine().allocate("Neutral", DAY)
    assert decision.allocation is not None
    assert decision.allocation.weights == {Asset.QLD: 0.7, Asset.CASH: 0.3}
    assert "REGIME_MAPPING:Neutral" in decision.reason_codes


def test_target_leverage_follows_the_stated_formula() -> None:
    # L = 1*w_QQQ + 2*w_QLD + 3*w_TQQQ  (BACKTEST_SPEC.md 14)
    engine = _engine()
    assert engine.target_leverage("Neutral") == pytest.approx(1.4)
    assert engine.target_leverage("Capitulation") == pytest.approx(2.3)
    assert engine.target_leverage("Overheated") == pytest.approx(0.9)


def test_leverage_is_reported_on_the_decision() -> None:
    decision = _engine().allocate("Neutral", DAY)
    assert decision.target_leverage == pytest.approx(1.4)
    assert "TARGET_LEVERAGE:1.40" in decision.reason_codes


def test_an_unknown_regime_produces_no_allocation() -> None:
    # ARCHITECTURE.md 15 — the failure-safe path carries no advice.
    decision = _engine().allocate(UNKNOWN_REGIME, DAY)
    assert decision.allocation is None
    assert decision.target_leverage is None
    assert decision.reason_codes == ("NO_ALLOCATION:UNKNOWN_REGIME",)


def test_an_unmapped_regime_is_an_error_not_a_guess() -> None:
    with pytest.raises(AllocationError, match="no allocation configured"):
        _engine().allocate("Euphoria", DAY)


# ------------------------------------------------------------------ TASK-062


def test_mappings_must_be_portfolios() -> None:
    with pytest.raises(AllocationError, match="sums to"):
        _engine(mappings={"Neutral": {Asset.QLD: 0.7}}).validate_mappings()


def test_the_market_exposure_floor_is_enforced() -> None:
    # PRD.md 2.1: "아무리 과열되었다고 판단하더라도 시장을 완전히 이탈하지 않는다"
    engine = _engine(
        mappings={"Overheated": {Asset.QQQ: 0.2, Asset.CASH: 0.8}},
        constraints=AllocationConstraints(min_market_exposure=0.5),
    )
    with pytest.raises(AllocationError, match="market exposure"):
        engine.validate_mappings()


def test_a_full_cash_allocation_is_rejected() -> None:
    engine = _engine(
        mappings={"Overheated": {Asset.CASH: 1.0}},
        constraints=AllocationConstraints(min_market_exposure=0.1),
    )
    with pytest.raises(AllocationError, match=r"0\.00% market exposure"):
        engine.validate_mappings()


def test_the_leverage_ceiling_is_enforced() -> None:
    engine = _engine(
        mappings={"Capitulation": {Asset.TQQQ: 1.0}},
        constraints=AllocationConstraints(max_target_leverage=2.5),
    )
    with pytest.raises(AllocationError, match=r"above the 2\.50x ceiling"):
        engine.validate_mappings()


def test_unresolved_mappings_block_the_engine(placeholder_config) -> None:
    from regime_monitor.config.loader import load_config

    with pytest.raises(AllocationError, match="unresolved research parameter"):
        AllocationEngine.from_config(load_config())


def test_engine_builds_from_the_placeholder_profile(placeholder_config) -> None:
    engine = AllocationEngine.from_config(placeholder_config)
    labels = placeholder_config.strategy.regime.labels
    assert labels is not None
    ladder = engine.leverage_ladder(labels)
    assert list(ladder.values()) == sorted(ladder.values(), reverse=True)


# ------------------------------------------------- the gate inside allocation


def test_a_vetoed_tqqq_sleeve_steps_down_the_leverage_ladder() -> None:
    engine = _engine()
    decision = engine.allocate("Capitulation", DAY, context=_context(cnn_fear_greed=5.0))

    assert decision.allocation is not None
    assert decision.allocation.weight(Asset.TQQQ) == 0.0
    assert decision.allocation.weight(Asset.QLD) == pytest.approx(1.0)
    assert decision.target_leverage == pytest.approx(2.0)
    assert "TQQQ_REALLOCATED_TO:QLD" in decision.reason_codes


def test_a_confirmed_bottom_keeps_the_tqqq_sleeve() -> None:
    engine = _engine()
    decision = engine.allocate(
        "Capitulation",
        DAY,
        context=_context(
            drawdown_52w=0.35, cnn_fear_greed=8.0, vix_percentile=0.97, rsi_14=22.0
        ),
    )
    assert decision.allocation is not None
    assert decision.allocation.weight(Asset.TQQQ) == pytest.approx(0.3)
    assert decision.target_leverage == pytest.approx(2.3)


def test_the_gate_is_not_consulted_when_the_mapping_has_no_tqqq() -> None:
    engine = _engine()
    decision = engine.allocate("Neutral", DAY)
    assert decision.gate is None


def test_a_vetoed_allocation_is_still_a_valid_portfolio() -> None:
    engine = _engine(constraints=AllocationConstraints(min_market_exposure=0.5))
    decision = engine.allocate("Capitulation", DAY, context=_context())
    assert decision.allocation is not None
    assert sum(decision.allocation.weights.values()) == pytest.approx(1.0)
    assert decision.allocation.market_exposure >= 0.5


def test_missing_indicators_default_to_vetoing_tqqq() -> None:
    # A data outage must reduce risk, never silently keep the 3x sleeve.
    decision = _engine().allocate("Capitulation", DAY, context=None)
    assert isinstance(decision, AllocationDecision)
    assert decision.allocation is not None
    assert decision.allocation.weight(Asset.TQQQ) == 0.0

"""The trend filter — leverage into fear, but not into a falling market.

The rule this encodes was not a preference: a fear-only ladder loses money over
1999-2015 with a 98% drawdown, because the score reaches capitulation months
into a long decline. These tests pin the behaviour that changes that.
"""

from __future__ import annotations

from datetime import date

import pytest

from regime_monitor.allocation.engine import AllocationEngine, AllocationError
from regime_monitor.allocation.sleeves import (
    SleeveError,
    leverage_of,
    market_exposure,
    portfolio_for,
)
from regime_monitor.allocation.tqqq_gate import GateContext, ThresholdRule, TqqqGate
from regime_monitor.allocation.trend_filter import (
    TrendFilter,
    TrendFilterError,
)
from regime_monitor.config.schema import (
    AllocationConstraints,
    ConfigError,
    TrendFilterSpec,
)
from regime_monitor.constants import Asset

DAY = date(2001, 9, 21)  # deep inside the dot-com decline


# ------------------------------------------------------------------- sleeves


@pytest.mark.parametrize(
    ("leverage", "expected"),
    [
        (0.0, {Asset.CASH: 1.0}),
        (0.5, {Asset.QQQ: 0.5, Asset.CASH: 0.5}),
        (1.0, {Asset.QQQ: 1.0}),
        (1.5, {Asset.QLD: 0.5, Asset.QQQ: 0.5}),
        (2.0, {Asset.QLD: 1.0}),
        (2.5, {Asset.TQQQ: 0.5, Asset.QLD: 0.5}),
        (3.0, {Asset.TQQQ: 1.0}),
    ],
)
def test_a_leverage_target_maps_to_adjacent_sleeves(leverage, expected) -> None:
    assert portfolio_for(leverage) == expected


@pytest.mark.parametrize("leverage", [0.0, 0.3, 1.0, 1.7, 2.0, 2.9, 3.0])
def test_the_mapping_round_trips(leverage: float) -> None:
    assert leverage_of(portfolio_for(leverage)) == pytest.approx(leverage)


@pytest.mark.parametrize("leverage", [0.0, 0.4, 1.0, 2.2, 3.0])
def test_every_mapped_portfolio_is_a_portfolio(leverage: float) -> None:
    weights = portfolio_for(leverage)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert all(weight > 0 for weight in weights.values())


def test_the_mapping_never_skips_a_rung() -> None:
    # 1.5x is "half QQQ, half QLD", not "a little TQQQ and a lot of cash".
    assert Asset.TQQQ not in portfolio_for(1.5)
    assert Asset.CASH not in portfolio_for(1.5)


def test_leverage_outside_the_ladder_is_refused() -> None:
    with pytest.raises(SleeveError, match="outside"):
        portfolio_for(3.5)
    with pytest.raises(SleeveError, match="outside"):
        portfolio_for(-0.1)


def test_market_exposure_is_everything_but_cash() -> None:
    assert market_exposure(portfolio_for(0.5)) == pytest.approx(0.5)
    assert market_exposure(portfolio_for(2.0)) == pytest.approx(1.0)


# -------------------------------------------------------------- the filter


def _filter(**overrides) -> TrendFilter:
    return TrendFilter(
        indicator=overrides.pop("indicator", "price_vs_200dma"),
        threshold=overrides.pop("threshold", 0.0),
        max_leverage_below=overrides.pop("max_leverage_below", 1.0),
        **overrides,
    )


def test_an_intact_trend_changes_nothing() -> None:
    weights = portfolio_for(3.0)
    result, verdict = _filter().apply(weights, observed=0.05)

    assert result == weights
    assert not verdict.engaged
    assert not verdict.capped
    assert "TREND_INTACT" in verdict.reason_codes()[0]


def test_a_broken_trend_caps_the_leverage() -> None:
    """The dot-com case: capitulation says 3x, the trend says no."""
    result, verdict = _filter().apply(portfolio_for(3.0), observed=-0.25)

    assert verdict.engaged
    assert verdict.capped
    assert leverage_of(result) == pytest.approx(1.0)
    assert result == {Asset.QQQ: 1.0}
    codes = verdict.reason_codes()
    assert any("TREND_BROKEN" in code for code in codes)
    assert "LEVERAGE_CAPPED:3.00->1.00" in codes


def test_a_book_already_below_the_cap_is_left_alone() -> None:
    weights = portfolio_for(0.5)
    result, verdict = _filter().apply(weights, observed=-0.25)

    assert result == weights
    assert verdict.engaged, "the trend is still broken"
    assert not verdict.capped, "but there was nothing to cap"


def test_exactly_at_the_threshold_counts_as_intact() -> None:
    _, verdict = _filter(threshold=0.0).apply(portfolio_for(2.0), observed=0.0)
    assert not verdict.engaged


def test_a_missing_trend_reading_caps_defensively() -> None:
    """Failing open would remove exactly the protection this exists for."""
    result, verdict = _filter().apply(portfolio_for(3.0), observed=None)

    assert verdict.engaged
    assert leverage_of(result) == pytest.approx(1.0)
    assert "TREND_UNKNOWN:price_vs_200dma" in verdict.reason_codes()


def test_a_disabled_filter_is_inert() -> None:
    weights = portfolio_for(3.0)
    disabled = TrendFilter(indicator="", threshold=0.0, max_leverage_below=0.0, enabled=False)
    result, verdict = disabled.apply(weights, observed=-0.9)

    assert result == weights
    assert verdict.disabled
    assert verdict.reason_codes() == ("TREND_FILTER_DISABLED",)


def test_the_verdict_explains_itself() -> None:
    _, verdict = _filter().apply(portfolio_for(3.0), observed=-0.25)
    description = verdict.describe()
    assert "price_vs_200dma" in description
    assert "below" in description
    assert "3.00x -> 1.00x" in description


def test_a_filter_with_no_indicator_is_rejected() -> None:
    with pytest.raises(TrendFilterError, match="needs an indicator"):
        TrendFilter(indicator="", threshold=0.0, max_leverage_below=1.0)


# ---------------------------------------------------------------- from config


def test_an_unresolved_filter_is_refused() -> None:
    with pytest.raises(TrendFilterError, match="unresolved research"):
        TrendFilter.from_spec(TrendFilterSpec(enabled=True))


def test_a_disabled_spec_builds_an_inert_filter() -> None:
    assert not TrendFilter.from_spec(TrendFilterSpec()).enabled


def test_a_cap_that_caps_nothing_is_rejected() -> None:
    with pytest.raises(ConfigError, match="caps nothing"):
        TrendFilterSpec(
            enabled=True,
            indicator="price_vs_200dma",
            threshold=0.0,
            max_leverage_below=3.0,
        )


def test_the_filter_must_name_a_real_indicator(placeholder_config) -> None:
    from regime_monitor.config.schema import AppConfig, StrategyConfig

    payload = placeholder_config.strategy.model_dump()
    payload["trend_filter"] = {
        "enabled": True,
        "indicator": "not_an_indicator",
        "threshold": 0.0,
        "max_leverage_below": 1.0,
    }
    with pytest.raises(ConfigError, match="not an enabled"):
        AppConfig(
            indicators=placeholder_config.indicators,
            strategy=StrategyConfig(**payload),
            alerts=placeholder_config.alerts,
            data_sources=placeholder_config.data_sources,
        )


# ------------------------------------------------------- inside the engine


def _engine(**overrides) -> AllocationEngine:
    gate = TqqqGate(
        rules=(ThresholdRule("deep_drawdown", "drawdown_52w", 0.35, at_least=True),),
        required_rules=("deep_drawdown",),
        min_confirmations=1,
    )
    return AllocationEngine(
        mappings=overrides.pop(
            "mappings",
            {
                "Capitulation": {Asset.TQQQ: 1.0},
                "Neutral": {Asset.QLD: 0.75, Asset.QQQ: 0.25},
                "Overheated": {Asset.QQQ: 0.5, Asset.CASH: 0.5},
            },
        ),
        constraints=overrides.pop("constraints", AllocationConstraints(min_market_exposure=0.5)),
        gate=overrides.pop("gate", gate),
        strategy_version="v0-test",
        trend_filter=overrides.pop("trend_filter", _filter()),
        **overrides,
    )


def _context(**values: float) -> GateContext:
    return GateContext(observation_date=DAY, indicator_values=values, regime="Capitulation")


def test_the_engine_caps_capitulation_in_a_downtrend() -> None:
    """The exact 2001-09 situation: deep fear, still falling."""
    decision = _engine().allocate(
        "Capitulation", DAY, context=_context(price_vs_200dma=-0.30, drawdown_52w=0.55)
    )
    assert decision.allocation is not None
    assert decision.target_leverage == pytest.approx(1.0)
    assert decision.trend is not None and decision.trend.capped
    assert any("LEVERAGE_CAPPED" in code for code in decision.reason_codes)


def test_the_engine_allows_capitulation_once_the_trend_recovers() -> None:
    decision = _engine().allocate(
        "Capitulation", DAY, context=_context(price_vs_200dma=0.02, drawdown_52w=0.55)
    )
    assert decision.allocation is not None
    assert decision.allocation.weight(Asset.TQQQ) == pytest.approx(1.0)
    assert decision.target_leverage == pytest.approx(3.0)


def test_the_trend_filter_runs_before_the_tqqq_gate() -> None:
    """A capped book has no TQQQ left, so the gate has nothing to rule on."""
    decision = _engine().allocate(
        "Capitulation", DAY, context=_context(price_vs_200dma=-0.30, drawdown_52w=0.55)
    )
    assert decision.gate is None, "the gate should not have been consulted"


def test_a_defensive_cap_may_go_below_the_overheat_floor() -> None:
    # PRD.md 2.1's floor is about overheating, not about a broken downtrend.
    engine = _engine(
        trend_filter=_filter(max_leverage_below=0.25),
        constraints=AllocationConstraints(min_market_exposure=0.5),
    )
    decision = engine.allocate(
        "Capitulation", DAY, context=_context(price_vs_200dma=-0.30, drawdown_52w=0.55)
    )
    assert decision.allocation is not None
    assert decision.allocation.market_exposure == pytest.approx(0.25)


def test_the_overheat_floor_still_binds_when_the_trend_is_intact() -> None:
    engine = _engine(
        mappings={"Overheated": {Asset.QQQ: 0.2, Asset.CASH: 0.8}},
        constraints=AllocationConstraints(min_market_exposure=0.5),
    )
    with pytest.raises(AllocationError, match="market exposure"):
        engine.validate_mappings()


def test_an_unknown_regime_is_untouched_by_the_filter() -> None:
    from regime_monitor.constants import UNKNOWN_REGIME

    decision = _engine().allocate(UNKNOWN_REGIME, DAY, context=_context())
    assert decision.allocation is None
    assert decision.trend is None

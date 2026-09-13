"""The trend filter — leverage into fear, but not into a falling market.

The rule this encodes was not a preference: a fear-only ladder loses money over
1999-2015 with a 98% drawdown, because the score reaches capitulation months
into a long decline. These tests pin the behaviour that changes that.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from pandas import Series

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


# ------------------------------------------------------------- hysteresis


def _values(*readings: float | None) -> Series:
    index = pd.date_range("2001-09-03", periods=len(readings), freq="B")
    return Series(readings, index=index, dtype="float64")


def test_without_a_reentry_level_the_filter_has_no_memory() -> None:
    filt = TrendFilter(indicator="t", threshold=-0.02, max_leverage_below=1.0)
    engaged = filt.engaged_series(_values(0.01, -0.03, -0.01, -0.03, 0.01))
    assert list(engaged) == [False, True, False, True, False]


def test_a_reentry_level_holds_the_cap_through_the_band() -> None:
    """The whole point: brushing the line must not flip the book.

    Measured over 1999-2015 the bare threshold engaged 82 times, a median of
    those lasting a single day, with QQQ rising in 20 of the 41 readable spells
    and falling in 21 — a coin flip paid for with two sides of trading cost.
    """
    filt = TrendFilter(
        indicator="t", threshold=-0.02, max_leverage_below=1.0, reentry_threshold=0.02
    )
    engaged = filt.engaged_series(_values(0.01, -0.03, -0.01, 0.01, 0.03, 0.01))
    #                                       in ^^^^^  held ^^^^^^^^^  out ^^^^
    assert list(engaged) == [False, True, True, True, False, False]


def test_the_state_only_ever_depends_on_the_past() -> None:
    """Truncation invariance: what is known today cannot change tomorrow."""
    filt = TrendFilter(
        indicator="t", threshold=-0.02, max_leverage_below=1.0, reentry_threshold=0.02
    )
    readings = _values(0.05, -0.03, 0.0, 0.01, 0.04, -0.01, -0.05, 0.03)
    full = filt.engaged_series(readings)
    for cut in range(1, len(readings) + 1):
        truncated = filt.engaged_series(readings.iloc[:cut])
        assert list(truncated) == list(full.iloc[:cut])


def test_an_unreadable_day_engages_the_cap_and_needs_a_recovery_to_release() -> None:
    filt = TrendFilter(
        indicator="t", threshold=-0.02, max_leverage_below=1.0, reentry_threshold=0.02
    )
    engaged = filt.engaged_series(_values(0.05, None, 0.01, 0.03))
    assert list(engaged) == [False, True, True, False]


def test_a_disabled_filter_engages_nothing() -> None:
    filt = TrendFilter(indicator="", threshold=0.0, max_leverage_below=0.0, enabled=False)
    assert not filt.engaged_series(_values(-0.5, -0.9)).any()


def test_a_reentry_level_below_the_exit_is_refused() -> None:
    with pytest.raises(TrendFilterError, match="inverts the band"):
        TrendFilter(
            indicator="t",
            threshold=-0.02,
            max_leverage_below=1.0,
            reentry_threshold=-0.05,
        )


def test_the_spec_refuses_an_inverted_band() -> None:
    with pytest.raises(ConfigError, match="inverts the band"):
        TrendFilterSpec(
            enabled=True,
            indicator="price_vs_200dma",
            threshold=-0.02,
            reentry_threshold=-0.05,
            max_leverage_below=1.0,
        )


def test_a_resolved_state_overrides_todays_reading() -> None:
    """How hysteresis reaches the per-day call: the state, not the level, decides."""
    filt = TrendFilter(
        indicator="t", threshold=-0.02, max_leverage_below=1.0, reentry_threshold=0.02
    )
    book = {Asset.TQQQ: 1.0}
    # Today reads above the exit level, but the spell has not been released yet.
    capped, verdict = filt.apply(book, 0.01, engaged=True)
    assert verdict.engaged and leverage_of(capped) == pytest.approx(1.0)
    # ... and the mirror: below the exit, but the spell is over.
    kept, verdict = filt.apply(book, -0.03, engaged=False)
    assert not verdict.engaged and kept == book


def test_the_engine_holds_the_cap_while_the_band_is_uncrossed() -> None:
    engine = _engine()
    context = GateContext(
        observation_date=DAY,
        indicator_values={"price_vs_200dma": 0.01},
        regime="Capitulation",
        trend_broken=True,
    )
    decision = engine.allocate("Capitulation", DAY, context=context)
    assert decision.allocation is not None
    assert decision.allocation.target_leverage == pytest.approx(1.0)
    assert any(code.startswith("TREND_BROKEN") for code in decision.reason_codes)


# ------------------------------------------------------- depth floor (TASK-096)


def _depth_filter(**overrides) -> TrendFilter:
    defaults = {
        "indicator": "t",
        "threshold": -0.02,
        "max_leverage_below": 0.5,
        "reentry_threshold": 0.02,
        "depth_indicator": "d",
        "min_depth_to_engage": 0.20,
    }
    return TrendFilter(**{**defaults, **overrides})


def _depth(*readings: float | None) -> Series:
    index = pd.date_range("2001-09-03", periods=len(readings), freq="B")
    return Series(readings, index=index, dtype="float64")


def test_a_shallow_dip_below_the_line_no_longer_engages() -> None:
    """The whole point: most dips below the trend line are not crises.

    Measured over 1999-2015, 391 of the days the filter engaged fell outside
    any 20% drawdown episode — 2010's European scare, 2011, 2015-08. Each one
    cost two sides of trading and a stretch at half leverage into a recovery.
    """
    filt = _depth_filter()
    engaged = filt.engaged_series(
        _values(0.01, -0.05, -0.06, -0.04), _depth(0.02, 0.08, 0.12, 0.15)
    )
    assert list(engaged) == [False, False, False, False]


def test_the_same_dip_engages_once_the_fall_is_already_deep() -> None:
    filt = _depth_filter()
    engaged = filt.engaged_series(
        _values(0.01, -0.05, -0.06, -0.04), _depth(0.02, 0.08, 0.21, 0.25)
    )
    #                          deep enough on day 3 ^^^^
    assert list(engaged) == [False, False, True, True]


def test_depth_gates_engaging_but_never_releasing() -> None:
    """A recovering depth reading must not lift the cap on its own.

    Depth climbs back under the floor while price is still beneath the line.
    Releasing there would hand the leverage back mid-decline, which is the
    failure this filter exists to prevent.
    """
    filt = _depth_filter()
    engaged = filt.engaged_series(
        _values(-0.05, -0.06, -0.05, 0.03), _depth(0.21, 0.25, 0.05, 0.01)
    )
    #                        cap held ^^^^^  released by price ^^^^
    assert list(engaged) == [True, True, True, False]


def test_an_unreadable_depth_counts_as_deep() -> None:
    """Same direction as an unreadable trend: assume the worst, keep the cap."""
    filt = _depth_filter()
    engaged = filt.engaged_series(_values(-0.05, -0.05), _depth(None, 0.01))
    assert list(engaged) == [True, True]


def test_a_missing_depth_series_falls_back_to_the_stricter_filter(caplog) -> None:
    filt = _depth_filter()
    with caplog.at_level("WARNING"):
        engaged = filt.engaged_series(_values(0.01, -0.05))
    assert list(engaged) == [False, True]
    assert "gated on d" in caplog.text


def test_the_depth_floor_is_causal() -> None:
    filt = _depth_filter()
    readings = _values(0.05, -0.03, -0.06, 0.01, -0.04, -0.05, -0.01, 0.03)
    depths = _depth(0.01, 0.05, 0.22, 0.18, 0.09, 0.24, 0.26, 0.02)
    full = filt.engaged_series(readings, depths)
    for cut in range(1, len(readings) + 1):
        truncated = filt.engaged_series(readings.iloc[:cut], depths.iloc[:cut])
        assert list(truncated) == list(full.iloc[:cut])


def test_apply_honours_the_depth_floor_without_a_resolved_state() -> None:
    filt = _depth_filter()
    book = portfolio_for(2.0)
    shallow, verdict = filt.apply(book, -0.05, depth_observed=0.05)
    assert not verdict.engaged
    assert leverage_of(shallow) == pytest.approx(2.0)
    deep, verdict = filt.apply(book, -0.05, depth_observed=0.30)
    assert verdict.engaged
    assert leverage_of(deep) == pytest.approx(0.5)


def test_half_a_depth_floor_is_refused() -> None:
    with pytest.raises(TrendFilterError, match="meaningless apart"):
        TrendFilter(
            indicator="t", threshold=-0.02, max_leverage_below=0.5, min_depth_to_engage=0.2
        )
    with pytest.raises(ConfigError, match="meaningless apart"):
        TrendFilterSpec(enabled=True, indicator="t", threshold=-0.02,
                        max_leverage_below=0.5, depth_indicator="d")


def test_the_spec_carries_the_depth_floor_into_the_filter() -> None:
    spec = TrendFilterSpec(
        enabled=True,
        indicator="price_vs_200dma",
        threshold=-0.02,
        reentry_threshold=0.02,
        max_leverage_below=0.5,
        depth_indicator="drawdown_52w",
        min_depth_to_engage=0.20,
    )
    filt = TrendFilter.from_spec(spec)
    assert filt.gated_on_depth
    assert filt.depth_indicator == "drawdown_52w"
    assert filt.min_depth_to_engage == pytest.approx(0.20)

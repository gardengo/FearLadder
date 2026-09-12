"""TASK-030 .. TASK-036 — indicator computations and the engine.

Two kinds of test live here: the arithmetic of each indicator, and the
structural guarantee that every one of them is trailing-only
(``BACKTEST_SPEC.md`` 8).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from pandas import Series

from regime_monitor.config.schema import IndicatorSpec, NormalizationSpec
from regime_monitor.indicators.computations import (
    REGISTRY,
    IndicatorError,
    change,
    compute,
    drawdown,
    ma_ratio,
    ma_slope,
    momentum,
    moving_average,
    price_vs_ma,
    rolling_volatility,
    rsi,
    trailing_percentile,
)
from regime_monitor.indicators.engine import (
    IndicatorEngine,
    MissingSourceError,
    sources_from_frame,
)

START = date(2020, 1, 1)


def _series(values: list[float]) -> Series:
    index = [START + timedelta(days=offset) for offset in range(len(values))]
    return Series(values, index=pd.Index(index, name="observation_date"), dtype="float64")


def _trending(length: int, start: float = 100.0, step: float = 1.0) -> Series:
    return _series([start + step * i for i in range(length)])


# ------------------------------------------------------------------ TASK-030


def test_rsi_is_100_when_every_move_is_a_gain() -> None:
    result = rsi(_trending(30), window=14)
    assert result.dropna().iloc[-1] == pytest.approx(100.0)


def test_rsi_is_0_when_every_move_is_a_loss() -> None:
    result = rsi(_trending(30, start=200.0, step=-1.0), window=14)
    assert result.dropna().iloc[-1] == pytest.approx(0.0)


def test_rsi_is_50_on_a_flat_series() -> None:
    result = rsi(_series([100.0] * 30), window=14)
    assert result.dropna().iloc[-1] == pytest.approx(50.0)


def test_rsi_matches_wilder_smoothing() -> None:
    # Wilder's average is ewm(alpha=1/n, adjust=False); adjust=True would
    # re-weight against the full sample and leak information backwards.
    prices = _series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08])
    delta = prices.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1 / 3, adjust=False, min_periods=3).mean()
    loss = (-delta).clip(lower=0.0).ewm(alpha=1 / 3, adjust=False, min_periods=3).mean()
    expected = 100.0 - 100.0 / (1.0 + gain / loss)

    result = rsi(prices, window=3)
    pd.testing.assert_series_equal(
        result.dropna(), expected.dropna(), check_names=False, rtol=1e-9
    )


def test_rsi_warms_up_before_producing_a_value() -> None:
    result = rsi(_trending(20), window=14)
    assert result.iloc[:14].isna().all()
    assert result.iloc[14:].notna().all()


def test_rsi_rejects_a_degenerate_window() -> None:
    with pytest.raises(IndicatorError, match="window must be >= 2"):
        rsi(_trending(30), window=1)


# ------------------------------------------------------------------ TASK-031


def test_moving_average_is_trailing_and_needs_a_full_window() -> None:
    prices = _series([1.0, 2.0, 3.0, 4.0])
    result = moving_average(prices, window=3)
    assert result.iloc[:2].isna().all()
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[3] == pytest.approx(3.0)


def test_price_vs_ma_is_the_relative_distance() -> None:
    prices = _series([10.0, 10.0, 10.0, 13.0])
    result = price_vs_ma(prices, window=3)
    # MA over the last three closes = (10 + 10 + 13)/3 = 11.0
    assert result.iloc[3] == pytest.approx(13.0 / 11.0 - 1.0)


def test_ma_ratio_is_positive_in_an_uptrend() -> None:
    result = ma_ratio(_trending(60), fast=5, slow=20)
    assert result.dropna().iloc[-1] > 0


def test_ma_ratio_requires_fast_shorter_than_slow() -> None:
    with pytest.raises(IndicatorError, match="must be shorter than"):
        ma_ratio(_trending(60), fast=200, slow=50)


def test_ma_slope_is_positive_when_the_average_rises() -> None:
    result = ma_slope(_trending(60), window=10, lookback=5)
    assert result.dropna().iloc[-1] > 0

    falling = ma_slope(_trending(60, start=200.0, step=-1.0), window=10, lookback=5)
    assert falling.dropna().iloc[-1] < 0


# ------------------------------------------------------------------ TASK-032


def test_momentum_is_the_trailing_total_return() -> None:
    prices = _series([100.0, 105.0, 110.0, 121.0])
    assert momentum(prices, window=3).iloc[3] == pytest.approx(0.21)


def test_momentum_needs_a_full_lookback() -> None:
    assert momentum(_trending(10), window=5).iloc[:5].isna().all()


# ------------------------------------------------------------------ TASK-033


def test_drawdown_is_a_positive_depth() -> None:
    # Depth, not a negative number: larger always means more fear.
    prices = _series([100.0, 120.0, 90.0])
    result = drawdown(prices)
    assert result.iloc[0] == pytest.approx(0.0)
    assert result.iloc[1] == pytest.approx(0.0)
    assert result.iloc[2] == pytest.approx(0.25)


def test_expanding_drawdown_remembers_the_all_time_peak() -> None:
    prices = _series([100.0, 200.0, 150.0, 120.0, 130.0])
    result = drawdown(prices, window=None)
    assert result.iloc[-1] == pytest.approx(1 - 130.0 / 200.0)


def test_windowed_drawdown_forgets_peaks_outside_the_window() -> None:
    prices = _series([200.0, 100.0, 100.0, 100.0, 90.0])
    windowed = drawdown(prices, window=3)
    expanding = drawdown(prices, window=None)
    # The 200 peak has left the 3-day window but not the expanding one.
    assert windowed.iloc[-1] == pytest.approx(1 - 90.0 / 100.0)
    assert expanding.iloc[-1] == pytest.approx(1 - 90.0 / 200.0)


def test_drawdown_never_goes_negative_at_a_new_high() -> None:
    assert (drawdown(_trending(50)) >= 0).all()


# ------------------------------------------------------------------ TASK-034


def test_change_is_the_relative_move() -> None:
    vix = _series([12.0, 13.0, 18.0])
    assert change(vix, window=2).iloc[2] == pytest.approx(0.5)


def test_trailing_percentile_uses_only_the_trailing_window() -> None:
    values = _series([10.0, 20.0, 15.0, 5.0, 30.0])
    result = trailing_percentile(values, window=3)
    assert result.iloc[:2].isna().all()
    assert result.iloc[2] == pytest.approx(2 / 3)  # 15 is 2nd of {10,20,15}
    assert result.iloc[3] == pytest.approx(1 / 3)  # 5 is lowest of {20,15,5}
    assert result.iloc[4] == pytest.approx(1.0)  # 30 is highest of {15,5,30}


def test_trailing_percentile_is_bounded() -> None:
    rng = np.random.default_rng(7)
    values = _series(list(rng.normal(20, 5, 400)))
    result = trailing_percentile(values, window=100).dropna()
    assert result.between(0.0, 1.0).all()


def test_rolling_volatility_rises_with_dispersion() -> None:
    calm = _series([100.0 * (1.001**i) for i in range(100)])
    rng = np.random.default_rng(3)
    wild = _series(list(100.0 * np.cumprod(1 + rng.normal(0, 0.05, 100))))
    assert (
        rolling_volatility(wild, window=20).dropna().iloc[-1]
        > rolling_volatility(calm, window=20).dropna().iloc[-1]
    )


# ------------------------------------------------------------------ TASK-036


def test_breadth_refuses_to_compute() -> None:
    # PRD.md 6.5 — a survivorship-biased breadth series is worse than none.
    with pytest.raises(IndicatorError, match="point-in-time"):
        compute("pct_above_ma", _trending(300), {"window": 200})


# -------------------------------------------------- the no-look-ahead property


ALL_INDICATORS = [
    ("rsi", {"window": 14}),
    ("moving_average", {"window": 20}),
    ("price_vs_ma", {"window": 20}),
    ("ma_ratio", {"fast": 10, "slow": 50}),
    ("ma_slope", {"window": 50, "lookback": 21}),
    ("momentum", {"window": 21}),
    ("drawdown", {"window": None}),
    ("drawdown", {"window": 90}),
    ("level", {}),
    ("change", {"window": 5}),
    ("difference", {"window": 5}),
    ("trailing_percentile", {"window": 60}),
    ("rolling_volatility", {"window": 20}),
]


@pytest.mark.parametrize(("name", "params"), ALL_INDICATORS)
def test_no_indicator_can_see_the_future(name: str, params: dict) -> None:
    """Truncating the future must not change any past value.

    This is the structural version of ``BACKTEST_SPEC.md`` 24.2: if an indicator
    used a centered window, a full-sample statistic or a forward fill, the values
    computed on the full history would differ from the ones computed on history
    truncated at the same date.
    """
    rng = np.random.default_rng(11)
    prices = _series(list(100.0 * np.cumprod(1 + rng.normal(0.0003, 0.012, 400))))
    cutoff = 300

    full = compute(name, prices, params).iloc[:cutoff]
    truncated = compute(name, prices.iloc[:cutoff], params)

    pd.testing.assert_series_equal(full, truncated, check_names=False)


@pytest.mark.parametrize(("name", "params"), ALL_INDICATORS)
def test_appending_a_new_day_never_rewrites_history(name: str, params: dict) -> None:
    rng = np.random.default_rng(13)
    prices = _series(list(100.0 * np.cumprod(1 + rng.normal(0.0003, 0.012, 300))))

    before = compute(name, prices.iloc[:-1], params)
    after = compute(name, prices, params).iloc[:-1]

    pd.testing.assert_series_equal(before, after, check_names=False)


def test_no_indicator_fills_gaps_forward() -> None:
    # A NaN input must stay unusable, not be quietly carried forward.
    prices = _series([100.0, 101.0, float("nan"), 103.0, 104.0])
    assert compute("level", prices, {}).isna().iloc[2]


def test_registry_covers_every_configured_compute(placeholder_config) -> None:
    configured = {
        spec.compute for spec in placeholder_config.indicators.indicators.values()
    }
    assert configured <= set(REGISTRY), configured - set(REGISTRY)


def test_unknown_compute_is_reported_with_the_alternatives() -> None:
    with pytest.raises(IndicatorError, match="unknown compute"):
        compute("teleport", _trending(10), {})


# ------------------------------------------------------------------ the engine


def _price_sources(length: int = 800) -> dict[str, Series]:
    """Long enough to warm up the longest configured window (504 days)."""
    rng = np.random.default_rng(5)
    prices = _series(list(100.0 * np.cumprod(1 + rng.normal(0.0004, 0.011, length))))
    vix = _series(list(np.abs(rng.normal(18, 4, length))))
    fng = _series(list(np.clip(rng.normal(50, 18, length), 0, 100)))
    aaii = _series(list(rng.normal(0.05, 0.12, length)))
    return {"QQQ": prices, "VIX": vix, "CNN_FEAR_GREED": fng, "AAII_SENTIMENT": aaii}


def test_engine_computes_every_enabled_indicator(placeholder_config) -> None:
    engine = IndicatorEngine(placeholder_config.indicators)
    results = engine.compute_all(_price_sources())
    assert set(results) == set(placeholder_config.indicators.enabled_indicators)
    assert all(result.latest is not None for result in results.values())


def test_engine_frame_is_one_column_per_indicator(placeholder_config) -> None:
    engine = IndicatorEngine(placeholder_config.indicators)
    frame = engine.to_frame(engine.compute_all(_price_sources()))
    assert set(frame.columns) == set(placeholder_config.indicators.enabled_indicators)
    assert frame.index.is_monotonic_increasing


def test_engine_skips_an_optional_indicator_whose_source_is_absent(
    placeholder_config,
) -> None:
    sources = _price_sources()
    del sources["AAII_SENTIMENT"]  # optional file-backed source

    results = IndicatorEngine(placeholder_config.indicators).compute_all(sources)
    assert "aaii_bull_bear_spread" not in results
    assert "rsi_14" in results, "one missing optional source must not cost the whole day"


def test_engine_raises_when_a_mandatory_source_is_absent(placeholder_config) -> None:
    sources = _price_sources()
    del sources["VIX"]  # vix_level is mandatory
    with pytest.raises(MissingSourceError, match="VIX"):
        IndicatorEngine(placeholder_config.indicators).compute_all(sources)


def test_engine_strict_mode_refuses_to_skip_anything(placeholder_config) -> None:
    sources = _price_sources()
    del sources["AAII_SENTIMENT"]
    with pytest.raises(MissingSourceError):
        IndicatorEngine(placeholder_config.indicators).compute_all(sources, strict=True)


def test_engine_refuses_an_unresolved_research_parameter(placeholder_config) -> None:
    # An indicator whose own window is still null must not silently default.
    spec = IndicatorSpec(
        family="volatility",
        compute="trailing_percentile",
        params={"window": None},
        research_params=("window",),
        source="VIX",
        direction="HIGHER_IS_FEAR",
        normalization=NormalizationSpec(method="bounded", raw_at_score_min=0, raw_at_score_max=1),
    )
    engine = IndicatorEngine(placeholder_config.indicators)
    with pytest.raises(IndicatorError, match="unresolved research parameters"):
        engine.compute_one("vix_percentile", spec, _price_sources())


def test_engine_passes_a_deliberate_null_through(placeholder_config) -> None:
    # drawdown_current's `window: null` means "expanding peak", not "undecided".
    engine = IndicatorEngine(placeholder_config.indicators)
    results = engine.compute_all(_price_sources())
    assert results["drawdown_current"].latest is not None


def test_indicator_result_converts_to_a_domain_value(placeholder_config) -> None:
    sources = _price_sources()
    engine = IndicatorEngine(placeholder_config.indicators)
    result = engine.compute_all(sources)["rsi_14"]

    day = sources["QQQ"].index[-1]
    value = result.to_domain(day)
    assert value.indicator_name == "rsi_14"
    assert value.source_symbol == "QQQ"
    assert value.params == {"window": 14}
    assert value.value is not None


def test_domain_value_records_a_missing_reading_as_missing(placeholder_config) -> None:
    sources = _price_sources()
    engine = IndicatorEngine(placeholder_config.indicators)
    result = engine.compute_all(sources)["rsi_14"]

    warmup_day = sources["QQQ"].index[0]
    value = result.to_domain(warmup_day)
    assert value.value is None
    assert value.quality_status.value == "MISSING"


def test_sources_from_frame_drops_each_series_own_gaps() -> None:
    frame = pd.DataFrame(
        {
            "QQQ": [100.0, 101.0, 102.0],
            "AAII_SENTIMENT": [0.1, float("nan"), float("nan")],
        },
        index=[START, START + timedelta(days=1), START + timedelta(days=2)],
    )
    sources = sources_from_frame(frame)
    assert len(sources["QQQ"]) == 3
    assert len(sources["AAII_SENTIMENT"]) == 1

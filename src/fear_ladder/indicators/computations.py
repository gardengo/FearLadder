"""Indicator computations (TASK-030 .. TASK-036).

Every function here is a pure, trailing-only transform of one input series.
``BACKTEST_SPEC.md`` 8 is categorical about what that rules out::

    금지: centered rolling / future fill / full sample statistic
    허용: trailing rolling window / past-only percentile / expanding statistic

The concrete consequences, applied throughout:

* ``rolling(window)`` in pandas is already right-aligned, and ``center=True`` is
  never used.
* smoothing uses ``ewm(..., adjust=False)``, which is a causal recursion; the
  ``adjust=True`` default re-weights using the whole sample.
* no ``fillna``/``bfill``/``interpolate`` — a value that is not computable yet
  stays ``NaN`` and is handled downstream as missing data.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd
from pandas import Series

#: Trading days per calendar year, the convention used by the momentum and
#: drawdown windows configured in ``config/indicators.yaml``.
TRADING_DAYS_PER_YEAR = 252


class IndicatorError(ValueError):
    """Raised when an indicator cannot be computed as configured."""


def _require_window(name: str, window: int | None, *, minimum: int = 1) -> int:
    if window is None:
        raise IndicatorError(f"{name}: window is required")
    if not isinstance(window, int) or isinstance(window, bool):
        raise IndicatorError(f"{name}: window must be an int, got {window!r}")
    if window < minimum:
        raise IndicatorError(f"{name}: window must be >= {minimum}, got {window}")
    return window


def _as_float(series: Series) -> Series:
    return pd.to_numeric(series, errors="coerce").astype("float64")


# ------------------------------------------------------------------- TASK-030


def rsi(series: Series, *, window: int) -> Series:
    """Wilder's RSI.

    Wilder smoothing is the exponential recursion ``avg_t = avg_{t-1} + (x_t -
    avg_{t-1})/n``, which is exactly ``ewm(alpha=1/n, adjust=False)``. The
    ``adjust=True`` default would renormalise every point against the full
    sample and quietly leak information backwards.
    """
    window = _require_window("rsi", window, minimum=2)
    values = _as_float(series)
    delta = values.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    alpha = 1.0 / window
    avg_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=window).mean()

    relative_strength = avg_gain / avg_loss
    result = 100.0 - (100.0 / (1.0 + relative_strength))
    # All-gain windows give avg_loss == 0 -> RSI is 100 by definition, not NaN.
    result = result.where(avg_loss != 0.0, 100.0)
    result = result.where(~((avg_loss == 0.0) & (avg_gain == 0.0)), 50.0)
    return result.where(avg_gain.notna() & avg_loss.notna())


# ------------------------------------------------------------------- TASK-031


def moving_average(series: Series, *, window: int) -> Series:
    window = _require_window("moving_average", window)
    return _as_float(series).rolling(window, min_periods=window).mean()


def price_vs_ma(series: Series, *, window: int) -> Series:
    """Relative distance of the close from its moving average."""
    values = _as_float(series)
    average = moving_average(values, window=window)
    return values / average - 1.0


def ma_ratio(series: Series, *, fast: int, slow: int) -> Series:
    """Distance between two moving averages, e.g. the 50/200 cross."""
    fast = _require_window("ma_ratio.fast", fast)
    slow = _require_window("ma_ratio.slow", slow)
    if fast >= slow:
        raise IndicatorError(f"ma_ratio: fast ({fast}) must be shorter than slow ({slow})")
    return moving_average(series, window=fast) / moving_average(series, window=slow) - 1.0


def ma_slope(series: Series, *, window: int, lookback: int) -> Series:
    """Relative change of a moving average over ``lookback`` days."""
    lookback = _require_window("ma_slope.lookback", lookback)
    average = moving_average(series, window=window)
    return average.pct_change(lookback)


# ------------------------------------------------------------------- TASK-032


def momentum(series: Series, *, window: int) -> Series:
    """Total return over the trailing ``window`` trading days."""
    window = _require_window("momentum", window)
    return _as_float(series).pct_change(window)


# ------------------------------------------------------------------- TASK-033


def drawdown(series: Series, *, window: int | None = None) -> Series:
    """Depth below the running peak, as a non-negative fraction.

    ``0.25`` means 25% below the peak. Reporting depth rather than a negative
    number keeps the fear direction unambiguous: larger is always more fear.

    ``window = None`` uses the expanding peak (all-time-to-date), which
    ``BACKTEST_SPEC.md`` 8 explicitly permits because it only ever looks
    backwards. A number uses a trailing window instead.
    """
    values = _as_float(series)
    if window is None:
        peak = values.expanding(min_periods=1).max()
    else:
        window = _require_window("drawdown", window)
        peak = values.rolling(window, min_periods=window).max()
    depth = 1.0 - values / peak
    return depth.clip(lower=0.0)


# ------------------------------------------------------------------- TASK-034


def level(series: Series) -> Series:
    """The series itself — used when the published value is the indicator."""
    return _as_float(series)


def change(series: Series, *, window: int) -> Series:
    """Relative change over ``window`` observations."""
    window = _require_window("change", window)
    return _as_float(series).pct_change(window)


def difference(series: Series, *, window: int) -> Series:
    """Absolute change over ``window`` observations."""
    window = _require_window("difference", window)
    return _as_float(series).diff(window)


def trailing_percentile(series: Series, *, window: int) -> Series:
    """Where the current value sits inside its own trailing distribution, 0..1.

    ``Rolling.rank(pct=True)`` ranks the current observation within the window
    that ends at it, so the answer at ``t`` uses only data up to ``t``.
    """
    window = _require_window("trailing_percentile", window, minimum=2)
    return _as_float(series).rolling(window, min_periods=window).rank(pct=True)


def rolling_volatility(series: Series, *, window: int, annualize: bool = True) -> Series:
    """Trailing standard deviation of daily returns."""
    window = _require_window("rolling_volatility", window, minimum=2)
    returns = _as_float(series).pct_change()
    volatility = returns.rolling(window, min_periods=window).std()
    if annualize:
        volatility = volatility * (TRADING_DAYS_PER_YEAR**0.5)
    return volatility


# ------------------------------------------------------------------- TASK-036


def pct_above_ma(series: Series, *, window: int) -> Series:  # noqa: ARG001
    """Breadth placeholder — not computable without point-in-time constituents.

    Kept registered so ``config/indicators.yaml`` stays honest about what the
    disabled ``breadth_pct_above_200dma`` indicator *would* compute
    (``PRD.md`` 6.5, TASK-025).
    """
    raise IndicatorError(
        "breadth is unavailable: point-in-time Nasdaq-100 constituents are "
        "required, and rebuilding breadth from today's members is forbidden "
        "(PRD.md 6.5)"
    )


# ------------------------------------------------------------------- registry

ComputeFn = Callable[..., Series]

#: ``compute:`` names usable in ``config/indicators.yaml``.
REGISTRY: dict[str, ComputeFn] = {
    "rsi": rsi,
    "moving_average": moving_average,
    "price_vs_ma": price_vs_ma,
    "ma_ratio": ma_ratio,
    "ma_slope": ma_slope,
    "momentum": momentum,
    "drawdown": drawdown,
    "level": level,
    "change": change,
    "difference": difference,
    "trailing_percentile": trailing_percentile,
    "rolling_volatility": rolling_volatility,
    "pct_above_ma": pct_above_ma,
}


def compute(name: str, series: Series, params: dict[str, Any] | None = None) -> Series:
    """Run a registered computation by its configuration name."""
    try:
        function = REGISTRY[name]
    except KeyError as exc:
        raise IndicatorError(
            f"unknown compute {name!r}; known: {sorted(REGISTRY)}"
        ) from exc
    return function(series, **(params or {}))

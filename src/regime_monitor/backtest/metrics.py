"""Performance metrics (``BACKTEST_SPEC.md`` 22).

The required set is explicit::

    CAGR, Total Return, Volatility, MDD, Sharpe, Sortino, Calmar,
    Worst Year, Recovery Time, Time Under Water, Turnover, Regime Change Count

``BACKTEST_SPEC.md`` 2 is equally explicit that CAGR alone is not the test, so
:class:`PerformanceMetrics` deliberately reports all of them together and
nothing summarises them into a single number.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from pandas import DataFrame, Series

TRADING_DAYS_PER_YEAR = 252
#: Excess return is measured against zero. A non-zero risk-free rate is a
#: research assumption the documents do not make, and stating it here would
#: quietly change every Sharpe ratio.
RISK_FREE_RATE = 0.0


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """One run's results. Every field is required by ``BACKTEST_SPEC.md`` 22."""

    name: str
    start: date
    end: date
    years: float
    total_return: float
    cagr: float
    volatility: float
    max_drawdown: float
    sharpe: float
    sortino: float
    calmar: float
    worst_year: float
    best_year: float
    recovery_days: int | None
    time_under_water: float
    turnover: float
    trade_count: int
    regime_change_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start"] = self.start.isoformat()
        payload["end"] = self.end.isoformat()
        return payload

    def summary(self) -> str:
        return (
            f"{self.name}: CAGR {self.cagr:.2%}, vol {self.volatility:.2%}, "
            f"MDD {self.max_drawdown:.2%}, Sharpe {self.sharpe:.2f}, "
            f"Calmar {self.calmar:.2f}, turnover {self.turnover:.1f}x"
        )


def drawdown_series(nav: Series) -> Series:
    """Depth below the running peak, as a non-negative fraction."""
    peak = nav.cummax()
    return (1.0 - nav / peak).clip(lower=0.0)


def max_drawdown(nav: Series) -> float:
    return float(drawdown_series(nav).max()) if len(nav) else 0.0


def time_under_water(nav: Series) -> float:
    """Share of days spent below a previous peak."""
    if nav.empty:
        return 0.0
    return float((drawdown_series(nav) > 0).mean())


def recovery_days(nav: Series) -> int | None:
    """Calendar days from the deepest trough back to its prior peak.

    ``None`` means the series never recovered — which is itself a result, and
    is why this is not silently reported as zero.
    """
    if nav.empty:
        return None
    depths = drawdown_series(nav)
    if depths.max() <= 0:
        return 0
    trough = depths.idxmax()
    peak_value = nav.loc[:trough].max()
    after = nav.loc[trough:]
    recovered = after[after >= peak_value]
    if recovered.empty:
        return None
    return (recovered.index[0] - trough).days


def annual_returns(nav: Series) -> Series:
    """Calendar-year returns, used for worst/best year."""
    if nav.empty:
        return Series(dtype="float64")
    frame = nav.to_frame("nav")
    frame.index = pd.to_datetime(frame.index)
    yearly = frame["nav"].resample("YE").last()
    first = frame["nav"].iloc[0]
    # The first year is measured from the starting NAV, not from its own close.
    base = pd.concat([Series([first], index=[yearly.index[0] - pd.Timedelta(days=1)]), yearly])
    returns = base.pct_change().dropna()
    returns.index = [moment.year for moment in returns.index]
    return returns


def compute_metrics(
    nav: Series,
    *,
    name: str = "strategy",
    turnover: Series | None = None,
    trade_count: int = 0,
    regime_change_count: int = 0,
) -> PerformanceMetrics:
    """Compute the full metric set for one NAV path."""
    if nav.empty:
        raise ValueError("cannot compute metrics on an empty NAV series")

    nav = nav.dropna()
    returns = nav.pct_change().dropna()
    start, end = nav.index[0], nav.index[-1]
    days = (end - start).days
    years = days / 365.25 if days > 0 else 0.0

    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    cagr = float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1.0) if years > 0 else 0.0

    volatility = (
        float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))
        if len(returns) > 1
        else 0.0
    )
    mean_excess = float(returns.mean()) - RISK_FREE_RATE / TRADING_DAYS_PER_YEAR
    sharpe = (
        float(mean_excess / returns.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else 0.0
    )

    downside = returns[returns < 0]
    downside_deviation = (
        float(downside.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))
        if len(downside) > 1
        else 0.0
    )
    sortino = (
        float(mean_excess * TRADING_DAYS_PER_YEAR / downside_deviation)
        if downside_deviation > 0
        else 0.0
    )

    mdd = max_drawdown(nav)
    calmar = float(cagr / mdd) if mdd > 0 else 0.0

    yearly = annual_returns(nav)
    worst_year = float(yearly.min()) if not yearly.empty else 0.0
    best_year = float(yearly.max()) if not yearly.empty else 0.0

    return PerformanceMetrics(
        name=name,
        start=start,
        end=end,
        years=years,
        total_return=total_return,
        cagr=cagr,
        volatility=volatility,
        max_drawdown=mdd,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        worst_year=worst_year,
        best_year=best_year,
        recovery_days=recovery_days(nav),
        time_under_water=time_under_water(nav),
        turnover=float(turnover.sum()) if turnover is not None else 0.0,
        trade_count=trade_count,
        regime_change_count=regime_change_count,
    )


def metrics_from_result(result: Any, *, regime_change_count: int = 0) -> PerformanceMetrics:
    """Convenience wrapper for a :class:`~.simulator.BacktestResult`."""
    return compute_metrics(
        result.nav,
        name=result.name,
        turnover=result.turnover,
        trade_count=result.trade_count,
        regime_change_count=regime_change_count,
    )


def comparison_frame(metrics: list[PerformanceMetrics]) -> DataFrame:
    """Side-by-side table of several runs, one row per run."""
    if not metrics:
        return DataFrame()
    return DataFrame([item.to_dict() for item in metrics]).set_index("name")

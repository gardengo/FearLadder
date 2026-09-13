"""Performance evidence for a frozen strategy, in a form the dashboard can read.

The dashboard must be able to say how the strategy has behaved without a
research database or a 30-year backtest at page load, so this computes the
answer once and writes it out. ``scripts/make_performance_report.py`` is the
entry point; ``reports/performance.json`` is the artifact.

Two things are measured, and they answer different questions.

**Static benchmarks.** Buy-and-hold mixes of one ETF and cash, rebalanced on a
fixed schedule. These are the honest alternative: anyone can hold QQQ 60% /
cash 40% without a monitor, a score or a filter, so a strategy that cannot beat
that grid is not earning its complexity.

**Rolling windows.** A single full-period number hides everything that matters
about when you started. Every window of a given length, stepped monthly, gives
the distribution instead — and for a leveraged strategy the left tail of that
distribution is the number to look at, not the median.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from pandas import Series

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
#: Rebalancing cadence for the static mixes, in trading days (about a quarter).
REBALANCE_DAYS = 63
#: Rolling windows start one month apart.
WINDOW_STEP_DAYS = 21


def cash_curve(rates: Series | None, days: list[date]) -> Series:
    """Growth of one unit of cash, ACT/365 on the previous day's quoted rate.

    Mirrors the simulator: a missing quote accrues nothing rather than carrying
    a stale rate forward.
    """
    value, out = 1.0, []
    for index, day in enumerate(days):
        if index:
            quoted = rates.get(days[index - 1]) if rates is not None else None
            if quoted is not None and not pd.isna(quoted):
                value *= 1 + float(quoted) / 100.0 * (day - days[index - 1]).days / 365.0
        out.append(value)
    return Series(out, index=days, name="CASH")


def static_mix(
    equity: Series,
    cash: Series,
    equity_weight: float,
    *,
    rebalance_days: int = REBALANCE_DAYS,
) -> Series:
    """Hold ``equity_weight`` in one ETF and the rest in cash, rebalanced.

    Between rebalances the sleeves drift, which is what a real holder would
    experience; rebalancing daily would quietly assume free trading.
    """
    if not 0.0 <= equity_weight <= 1.0:
        raise ValueError(f"equity_weight must be a fraction, got {equity_weight}")
    days = list(equity.index)
    moves_e = equity.pct_change().fillna(0.0)
    moves_c = cash.pct_change().fillna(0.0)
    held_e, held_c, out = equity_weight, 1.0 - equity_weight, [1.0]
    for index in range(1, len(days)):
        held_e *= 1 + moves_e.iloc[index]
        held_c *= 1 + moves_c.iloc[index]
        total = held_e + held_c
        out.append(total)
        if index % rebalance_days == 0:
            held_e, held_c = total * equity_weight, total * (1.0 - equity_weight)
    return Series(out, index=days)


def window_stats(nav: Series, cash: Series | None = None) -> dict[str, float]:
    """CAGR, drawdown and excess-return Sharpe over whatever ``nav`` covers.

    Sharpe is measured *net of cash*. Without that subtraction every cash-heavy
    mix scores better simply for holding a positive-yielding asset, which would
    make the comparison meaningless at exactly the point it matters.
    """
    curve = nav / nav.iloc[0]
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    returns = curve.pct_change().dropna()
    drawdown = float((curve / curve.cummax() - 1).min())
    cagr = float(curve.iloc[-1] ** (1 / years) - 1) if years > 0 else float("nan")

    sharpe = float("nan")
    if cash is not None:
        riskless = (cash / cash.iloc[0]).pct_change()
        excess = (returns - riskless).dropna()
        if len(excess) > 1 and excess.std() > 0:
            sharpe = float(excess.mean() / excess.std() * np.sqrt(TRADING_DAYS))
    return {
        "multiple": float(curve.iloc[-1]),
        "cagr": cagr,
        "max_drawdown": drawdown,
        "sharpe": sharpe,
        "calmar": cagr / abs(drawdown) if drawdown else float("nan"),
        "years": years,
    }


@dataclass(frozen=True, slots=True)
class RollingSummary:
    """What every window of one length looked like, as a distribution."""

    years: int
    windows: int
    cagr_median: float
    cagr_worst: float
    cagr_p10: float
    cagr_best: float
    mdd_median: float
    mdd_worst: float
    loss_rate: float
    beats_reference: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "years": self.years,
            "windows": self.windows,
            "cagr_median": self.cagr_median,
            "cagr_worst": self.cagr_worst,
            "cagr_p10": self.cagr_p10,
            "cagr_best": self.cagr_best,
            "mdd_median": self.mdd_median,
            "mdd_worst": self.mdd_worst,
            "loss_rate": self.loss_rate,
            "beats_reference": self.beats_reference,
        }


def rolling_cagrs(
    nav: Series, years: int, *, step: int = WINDOW_STEP_DAYS
) -> tuple[np.ndarray, np.ndarray]:
    """Annualised return and drawdown of every window of ``years``."""
    span = int(years * TRADING_DAYS)
    days = list(nav.index)
    if len(days) <= span:
        return np.array([]), np.array([])
    cagrs: list[float] = []
    drawdowns: list[float] = []
    for start in range(0, len(days) - span, step):
        window = days[start : start + span]
        curve = nav[window] / nav[window].iloc[0]
        length = (window[-1] - window[0]).days / 365.25
        cagrs.append(curve.iloc[-1] ** (1 / length) - 1)
        drawdowns.append((curve / curve.cummax() - 1).min())
    return np.array(cagrs, dtype=float), np.array(drawdowns, dtype=float)


def summarise_rolling(
    nav: Series, years: int, *, reference: np.ndarray | None = None
) -> RollingSummary | None:
    """Distribution of every ``years``-long window, or ``None`` if too short."""
    cagrs, drawdowns = rolling_cagrs(nav, years)
    if not len(cagrs):
        return None
    beats = None
    if reference is not None and len(reference) == len(cagrs):
        beats = float((cagrs > reference).mean())
    return RollingSummary(
        years=years,
        windows=len(cagrs),
        cagr_median=float(np.median(cagrs)),
        cagr_worst=float(cagrs.min()),
        cagr_p10=float(np.percentile(cagrs, 10)),
        cagr_best=float(cagrs.max()),
        mdd_median=float(np.median(drawdowns)),
        mdd_worst=float(drawdowns.min()),
        loss_rate=float((cagrs < 0).mean()),
        beats_reference=beats,
    )


@dataclass(frozen=True, slots=True)
class Episode:
    """A named stretch of market history, and what each holding did in it."""

    name: str
    shape: str
    start: date
    end: date
    returns: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "shape": self.shape,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "returns": self.returns,
        }


#: Peak-to-trough for declines, trough-to-peak for recoveries, dated from QQQ.
#: ``shape`` is the distinction that decides this strategy's result: it beats a
#: static holding when a decline is fast and loses to one when it grinds.
EPISODES: tuple[tuple[str, str, date, date], ...] = (
    ("닷컴 붕괴", "빠른 폭락", date(2000, 3, 27), date(2002, 10, 9)),
    ("닷컴 이후 회복", "회복", date(2002, 10, 10), date(2007, 10, 31)),
    ("금융위기", "느린 폭락", date(2007, 10, 31), date(2009, 3, 9)),
    ("GFC 이후 강세장", "강세", date(2009, 3, 10), date(2020, 2, 19)),
    ("코로나 급락", "급락 V", date(2020, 2, 19), date(2020, 3, 23)),
    ("코로나 회복", "회복", date(2020, 3, 24), date(2021, 11, 19)),
    ("2022 약세장", "느린 하락", date(2021, 11, 19), date(2022, 12, 28)),
    ("2023 이후 강세", "강세", date(2022, 12, 29), date(2100, 1, 1)),
    ("횡보 2004-2006", "횡보", date(2004, 1, 1), date(2006, 12, 31)),
    ("횡보 2011-2012", "횡보", date(2011, 1, 1), date(2012, 12, 31)),
    ("횡보 2015-2016", "횡보", date(2015, 1, 1), date(2016, 12, 31)),
)


def episode_returns(navs: dict[str, Series], days: list[date]) -> list[Episode]:
    """Total return of each holding over each named episode."""
    episodes: list[Episode] = []
    for name, shape, start, end in EPISODES:
        window = [day for day in days if start <= day <= min(end, days[-1])]
        if len(window) < 10:
            continue
        episodes.append(
            Episode(
                name=name,
                shape=shape,
                start=window[0],
                end=window[-1],
                returns={
                    label: float(nav[window].iloc[-1] / nav[window].iloc[0] - 1)
                    for label, nav in navs.items()
                },
            )
        )
    return episodes


def risk_matched(
    reference: dict[str, float],
    candidates: dict[str, dict[str, float]],
    *,
    take: int = 4,
) -> list[dict[str, Any]]:
    """The static mixes whose drawdown is closest to the strategy's.

    Comparing returns at equal risk is the only comparison that settles
    anything: a higher return bought with a deeper hole is not an improvement.
    """
    target = abs(reference["max_drawdown"])
    ranked = sorted(
        candidates.items(), key=lambda item: abs(abs(item[1]["max_drawdown"]) - target)
    )
    return [
        {
            "name": name,
            "cagr": stats["cagr"],
            "max_drawdown": stats["max_drawdown"],
            "multiple": stats["multiple"],
            "cagr_gap": stats["cagr"] - reference["cagr"],
        }
        for name, stats in ranked[:take]
    ]

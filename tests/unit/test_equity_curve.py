"""The backtest tab's equity curve must be the frozen backtest's, or not be written.

``reports/performance.json`` is the evidence the strategy was frozen on. The
curve drawn beside its totals is recomputed later, from a database that goes on
past the freeze and from prices a provider is free to restate. These pin the
two things that keep the picture and the numbers from drifting apart: the
curve stops where the report stops, and it is refused if it ends anywhere else.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
import scripts.make_equity_curve as curve

START = date(2026, 1, 5)  # a Monday


def _nav(values: list[float]) -> pd.Series:
    days = [stamp.date() for stamp in pd.bdate_range(START, periods=len(values))]
    return pd.Series(values, index=days)


def _report(nav: pd.Series, close: pd.Series, *, end: date) -> dict:
    window = nav[[day for day in nav.index if day <= end]]
    held = close[window.index]
    return {
        "strategy_version": "v-test",
        "window": {"start": window.index[0].isoformat(), "end": window.index[-1].isoformat()},
        "overall": {
            curve.STRATEGY: {"multiple": float(window.iloc[-1] / window.iloc[0])},
            curve.BENCHMARK: {"multiple": float(held.iloc[-1] / held.iloc[0])},
        },
    }


def test_the_curve_stops_on_the_reports_last_day() -> None:
    """The database goes on past the freeze; the frozen picture must not."""
    nav = _nav([1.0, 1.1, 1.2, 1.1, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 5.0, 6.0])
    close = _nav([100.0 + step for step in range(12)])
    end = nav.index[9]  # two weeks in; the last two days are "after the freeze"

    payload = curve.build_curves(nav, close, end=end, report=_report(nav, close, end=end))

    assert payload["dates"][-1] == end.isoformat()
    assert payload["equity"][curve.STRATEGY][-1] == pytest.approx(1.8)
    assert max(payload["equity"][curve.STRATEGY]) < 5.0


def test_a_curve_that_ends_somewhere_else_is_refused() -> None:
    """A restated price, or another configuration, is another backtest."""
    nav = _nav([1.0, 1.1, 1.2, 1.1, 1.3])
    close = _nav([100.0, 101.0, 102.0, 103.0, 104.0])
    end = nav.index[-1]
    report = _report(nav, close, end=end)
    report["overall"][curve.STRATEGY]["multiple"] = 1.31

    with pytest.raises(curve.CurveMismatchError, match="ends at"):
        curve.build_curves(nav, close, end=end, report=report)


def test_a_curve_over_a_different_window_is_refused() -> None:
    nav = _nav([1.0, 1.1, 1.2, 1.1, 1.3])
    close = _nav([100.0, 101.0, 102.0, 103.0, 104.0])
    end = nav.index[-1]
    report = _report(nav, close, end=end)
    report["window"]["start"] = "1996-01-02"

    with pytest.raises(curve.CurveMismatchError, match="window"):
        curve.build_curves(nav, close, end=end, report=report)


def test_the_weekly_drawdown_keeps_the_bottom_of_the_week() -> None:
    """Sampling the week's close would report a shallower fall than happened."""
    #          Mon   Tue   Wed   Thu   Fri   | Mon   Tue
    nav = _nav([1.0, 1.0, 0.50, 0.90, 0.95, 0.96, 0.97])

    falls = curve.weekly(curve.drawdown(nav), worst=True)
    closes = curve.weekly(nav)

    assert falls.iloc[0] == pytest.approx(-0.50)  # Wednesday's low, not Friday's -5%
    assert closes.iloc[0] == pytest.approx(0.95)
    # Each point is dated the last day its week actually had.
    assert [stamp.date() for stamp in closes.index] == [nav.index[4], nav.index[6]]

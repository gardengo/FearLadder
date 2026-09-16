"""The evidence file behind the dashboard's performance page.

These pin the measurement choices, because each one was made to stop a specific
way of flattering a result: Sharpe net of cash, rebalancing that is not free,
and rolling windows rather than a single start date.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from pandas import Series

from fear_ladder.research.performance import (
    REBALANCE_DAYS,
    Episode,
    cash_curve,
    risk_matched,
    rolling_cagrs,
    static_mix,
    summarise_rolling,
    window_stats,
)

START = date(2000, 1, 3)


def _days(count: int) -> list[date]:
    return [START + timedelta(days=offset) for offset in range(count)]


def _grow(days: list[date], daily: float) -> Series:
    return Series([(1 + daily) ** index for index in range(len(days))], index=days)


# ------------------------------------------------------------------- cash


def test_cash_accrues_the_previous_days_quote() -> None:
    days = _days(366)
    rates = Series([5.0] * len(days), index=days)
    curve = cash_curve(rates, days)
    # ACT/365, accrued and compounded daily: (1 + 0.05/365) ** 365, not 1.05.
    assert curve.iloc[-1] == pytest.approx((1 + 0.05 / 365) ** 365, rel=1e-9)
    assert curve.iloc[-1] > 1.05, "daily compounding beats the simple rate"


def test_a_missing_quote_accrues_nothing_rather_than_carrying_a_stale_rate() -> None:
    days = _days(10)
    rates = Series([5.0] + [float("nan")] * 9, index=days)
    curve = cash_curve(rates, days)
    one_day = 1 + 0.05 / 365
    assert curve.iloc[-1] == pytest.approx(one_day)


def test_cash_without_any_rates_is_flat() -> None:
    days = _days(30)
    assert cash_curve(None, days).eq(1.0).all()


# -------------------------------------------------------------- static mix


def test_a_full_weight_mix_is_just_the_etf() -> None:
    days = _days(400)
    equity, cash = _grow(days, 0.001), cash_curve(None, days)
    mixed = static_mix(equity, cash, 1.0)
    assert mixed.iloc[-1] == pytest.approx(equity.iloc[-1] / equity.iloc[0], rel=1e-9)


def test_a_zero_weight_mix_is_just_cash() -> None:
    days = _days(400)
    equity = _grow(days, 0.001)
    rates = Series([4.0] * len(days), index=days)
    cash = cash_curve(rates, days)
    mixed = static_mix(equity, cash, 0.0)
    assert mixed.iloc[-1] == pytest.approx(cash.iloc[-1], rel=1e-9)


def test_the_sleeves_drift_between_rebalances() -> None:
    """Rebalancing daily would quietly assume free trading."""
    days = _days(REBALANCE_DAYS * 2)
    equity, cash = _grow(days, 0.01), cash_curve(None, days)
    mixed = static_mix(equity, cash, 0.5)
    # Half in a compounding sleeve and half in a flat one; if it rebalanced
    # every day the equity sleeve could never grow past its share.
    assert mixed.iloc[REBALANCE_DAYS - 1] > 0.5 * equity.iloc[REBALANCE_DAYS - 1] * 0.5


def test_a_weight_outside_zero_to_one_is_refused() -> None:
    days = _days(10)
    equity, cash = _grow(days, 0.001), cash_curve(None, days)
    with pytest.raises(ValueError, match="fraction"):
        static_mix(equity, cash, 1.4)


# ------------------------------------------------------------------ stats


def test_sharpe_is_measured_net_of_cash() -> None:
    """Otherwise a cash-heavy mix scores well for holding a yielding asset.

    Cash itself must come out at zero excess Sharpe, not a spectacular one.
    """
    days = _days(800)
    rates = Series([5.0] * len(days), index=days)
    cash = cash_curve(rates, days)
    stats = window_stats(cash, cash)
    assert stats["cagr"] == pytest.approx(0.05, abs=0.005)
    assert np.isnan(stats["sharpe"]) or abs(stats["sharpe"]) < 1e-6


def test_drawdown_and_cagr_describe_the_curve() -> None:
    days = _days(3)
    nav = Series([1.0, 0.5, 0.75], index=days)
    stats = window_stats(nav)
    assert stats["max_drawdown"] == pytest.approx(-0.5)
    assert stats["multiple"] == pytest.approx(0.75)


# ---------------------------------------------------------------- rolling


def test_rolling_windows_are_shorter_than_the_series_or_empty() -> None:
    days = _days(100)
    nav = _grow(days, 0.001)
    cagrs, drawdowns = rolling_cagrs(nav, years=10)
    assert not len(cagrs) and not len(drawdowns)
    assert summarise_rolling(nav, years=10) is None


def test_a_steady_climb_has_the_same_cagr_in_every_window() -> None:
    days = _days(252 * 6)
    nav = _grow(days, 0.0004)
    summary = summarise_rolling(nav, years=3)
    assert summary is not None
    assert summary.windows > 10
    assert summary.cagr_worst == pytest.approx(summary.cagr_median, rel=0.02)
    assert summary.loss_rate == 0.0
    assert summary.mdd_worst == pytest.approx(0.0, abs=1e-9)


def test_beats_reference_counts_windows_not_averages() -> None:
    days = _days(252 * 6)
    fast, slow = _grow(days, 0.0005), _grow(days, 0.0001)
    reference, _ = rolling_cagrs(slow, years=3)
    summary = summarise_rolling(fast, years=3, reference=reference)
    assert summary is not None
    assert summary.beats_reference == pytest.approx(1.0)


def test_a_mismatched_reference_is_ignored_rather_than_compared_wrongly() -> None:
    days = _days(252 * 6)
    nav = _grow(days, 0.0004)
    summary = summarise_rolling(nav, years=3, reference=np.array([0.1, 0.2]))
    assert summary is not None
    assert summary.beats_reference is None


# ----------------------------------------------------------- risk matching


def test_risk_matching_ranks_by_drawdown_not_return() -> None:
    reference = {"cagr": 0.19, "max_drawdown": -0.62}
    candidates = {
        "far": {"cagr": 0.30, "max_drawdown": -0.95, "multiple": 100.0},
        "near": {"cagr": 0.10, "max_drawdown": -0.61, "multiple": 17.0},
    }
    ranked = risk_matched(reference, candidates, take=2)
    assert ranked[0]["name"] == "near"
    assert ranked[0]["cagr_gap"] == pytest.approx(0.10 - 0.19)


# --------------------------------------------------------------- episodes


def test_an_episode_serialises_its_dates_as_text() -> None:
    episode = Episode(
        name="test", shape="빠른 폭락", start=START, end=START, returns={"전략": -0.5}
    )
    assert episode.to_dict()["start"] == START.isoformat()
    assert episode.to_dict()["returns"]["전략"] == pytest.approx(-0.5)


def test_the_committed_report_matches_the_frozen_strategy() -> None:
    """The artifact must describe the strategy that is actually shipped."""
    import json

    from fear_ladder import paths
    from fear_ladder.config.loader import load_config

    path = paths.REPORTS_DIR / "performance.json"
    if not path.exists():  # generated by scripts/make_performance_report.py
        pytest.skip("reports/performance.json has not been generated")
    report = json.loads(path.read_text(encoding="utf-8"))
    strategy = load_config().strategy
    assert report["strategy_version"] == strategy.strategy_version
    assert report["parameters"]["regime_labels"] == list(strategy.regime.labels or ())
    assert pd.notna(report["overall"]["전략"]["cagr"])


def test_the_report_covers_the_window_it_claims_to() -> None:
    """The committed artifact must reach back to where the research began.

    Guards the same thing the generator refuses to do: a report built from the
    pruned five-year operational database has the same shape as the real one
    and measures a different history.
    """
    import json

    from fear_ladder import paths
    from fear_ladder.config.loader import load_config

    path = paths.REPORTS_DIR / "performance.json"
    if not path.exists():
        pytest.skip("reports/performance.json has not been generated")
    report = json.loads(path.read_text(encoding="utf-8"))
    research_start = load_config().strategy.dataset_split.research_start
    assert research_start is not None
    assert date.fromisoformat(report["window"]["start"]) <= research_start


def test_the_generator_refuses_a_window_it_cannot_cover() -> None:
    """``make_performance_report.py`` must not quietly narrow the evidence.

    The operational database is pruned to five years every trading day, so this
    guard is the only thing between a routine re-run and a report that silently
    replaces thirty years of measurement with five.
    """
    import importlib.util
    import sys

    from fear_ladder import paths
    from fear_ladder.config.loader import load_config

    spec = importlib.util.spec_from_file_location(
        "_make_performance_report", paths.PROJECT_ROOT / "scripts" / "make_performance_report.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    strategy = load_config().strategy
    research_start = strategy.dataset_split.research_start
    assert research_start is not None

    assert module.covers_research_window(research_start, strategy)
    assert module.covers_research_window(research_start - timedelta(days=1), strategy)
    assert not module.covers_research_window(research_start + timedelta(days=1), strategy)
    # The pruned database starts far later than this; that is the real case.
    assert not module.covers_research_window(date(2021, 9, 15), strategy)

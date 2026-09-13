"""TASK-070 .. TASK-074 — NAV simulation, execution timing, costs, benchmarks."""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame, Series

from fear_ladder.backtest.benchmarks import (
    BenchmarkSpec,
    build_targets,
    rebalance_dates,
    standard_benchmarks,
)
from fear_ladder.backtest.costs import CostModel, turnover_between
from fear_ladder.backtest.metrics import (
    compute_metrics,
    drawdown_series,
    metrics_from_result,
    recovery_days,
)
from fear_ladder.backtest.simulator import (
    PortfolioSimulator,
    SimulationError,
)
from fear_ladder.constants import Asset, ExecutionTiming

START = date(2024, 1, 1)


def _days(count: int) -> list[date]:
    return [START + timedelta(days=offset) for offset in range(count)]


def _flat_prices(count: int, level: float = 100.0) -> tuple[DataFrame, DataFrame]:
    index = pd.Index(_days(count), name="observation_date")
    sleeves = (Asset.QQQ, Asset.QLD)
    frame = DataFrame({asset.value: [level] * count for asset in sleeves}, index=index)
    return frame, frame.copy()


def _prices(closes: dict[str, list[float]], opens: dict[str, list[float]] | None = None):
    count = len(next(iter(closes.values())))
    index = pd.Index(_days(count), name="observation_date")
    close_frame = DataFrame(closes, index=index)
    open_frame = DataFrame(opens or closes, index=index)
    return close_frame, open_frame


# ------------------------------------------------------------------ TASK-072


def test_turnover_ignores_the_cash_leg() -> None:
    # Moving 30% of the book from cash into QLD trades 30%, not 60%.
    turnover = turnover_between(
        {Asset.QLD: 0.7, Asset.CASH: 0.3}, {Asset.QLD: 1.0, Asset.CASH: 0.0}
    )
    assert turnover == pytest.approx(0.3)


def test_turnover_counts_both_legs_of_a_switch() -> None:
    turnover = turnover_between({Asset.QQQ: 1.0}, {Asset.QLD: 1.0})
    assert turnover == pytest.approx(2.0)


def test_cost_is_linear_in_traded_notional() -> None:
    model = CostModel(commission_bps=1.0, spread_bps=2.0, slippage_bps=2.0)
    assert model.total_bps == pytest.approx(5.0)
    assert model.cost_of(1.0) == pytest.approx(0.0005)
    assert model.cost_of(0.5) == pytest.approx(0.00025)


def test_the_zero_cost_model_exists_for_the_required_comparison() -> None:
    # BACKTEST_SPEC.md 17 wants both the with-cost and cost-free result.
    assert CostModel.zero().is_free
    assert CostModel.zero().cost_of(2.0) == 0.0


def test_cost_model_refuses_unresolved_research_parameters() -> None:
    from fear_ladder.config.schema import CostModelSpec

    with pytest.raises(ValueError, match="unresolved research parameter"):
        CostModel.from_spec(CostModelSpec())


def test_cost_model_builds_from_the_placeholder_profile(placeholder_config) -> None:
    model = CostModel.from_spec(placeholder_config.strategy.cost_model)
    assert model.total_bps == pytest.approx(5.0)


# ------------------------------------------------------------------ TASK-071


def test_a_target_is_never_executed_on_the_day_it_was_decided() -> None:
    """The central no-look-ahead guarantee (``BACKTEST_SPEC.md`` 5).

    QQQ doubles on day 1. A target decided on day 1 must not capture that move.
    """
    closes, opens = _prices({"QQQ": [100.0, 200.0, 200.0]})
    simulator = PortfolioSimulator(closes=closes, opens=opens)

    result = simulator.run({_days(3)[1]: {Asset.QQQ: 1.0}})
    assert result.nav.iloc[1] == pytest.approx(1.0), "day 1's jump must not be captured"
    assert result.weights.loc[_days(3)[1], Asset.QQQ] == 0.0


def test_next_open_earns_the_overnight_move_at_the_old_weights() -> None:
    closes, opens = _prices(
        {"QQQ": [100.0, 110.0, 120.0], "QLD": [100.0, 100.0, 100.0]},
        {"QQQ": [100.0, 105.0, 115.0], "QLD": [100.0, 100.0, 100.0]},
    )
    simulator = PortfolioSimulator(
        closes=closes, opens=opens, execution_timing=ExecutionTiming.NEXT_OPEN
    )
    days = _days(3)
    # Fully in QQQ from day 0's decision, switching to QLD on day 1's decision.
    result = simulator.run({days[0]: {Asset.QQQ: 1.0}, days[1]: {Asset.QLD: 1.0}})

    # Day 1: bought QQQ at the open (100 -> 105 overnight not earned, we were flat),
    # then 105 -> 110 intraday = +4.76%.
    assert result.nav.iloc[1] == pytest.approx(110.0 / 105.0)
    # Day 2: QQQ overnight 110 -> 115 at the old weights, then switch to flat QLD.
    assert result.nav.iloc[2] == pytest.approx(110.0 / 105.0 * 115.0 / 110.0)


def test_next_close_earns_the_whole_day_at_the_old_weights() -> None:
    closes, _ = _prices({"QQQ": [100.0, 110.0, 120.0], "QLD": [100.0, 100.0, 100.0]})
    simulator = PortfolioSimulator(
        closes=closes, execution_timing=ExecutionTiming.NEXT_CLOSE
    )
    days = _days(3)
    result = simulator.run({days[0]: {Asset.QQQ: 1.0}})

    # Day 1 buys at the close, so nothing is earned that day.
    assert result.nav.iloc[1] == pytest.approx(1.0)
    # Day 2 earns the full 110 -> 120 close-to-close move.
    assert result.nav.iloc[2] == pytest.approx(120.0 / 110.0)


def test_next_open_requires_open_prices() -> None:
    closes, _ = _prices({"QQQ": [100.0, 101.0]})
    with pytest.raises(SimulationError, match="needs open prices"):
        PortfolioSimulator(closes=closes)


# ------------------------------------------------------------------ TASK-070


def test_nav_compounds_the_held_returns() -> None:
    closes, opens = _prices({"QQQ": [100.0, 100.0, 110.0, 121.0]})
    simulator = PortfolioSimulator(closes=closes, opens=opens)
    result = simulator.run({_days(4)[0]: {Asset.QQQ: 1.0}})
    assert result.nav.iloc[-1] == pytest.approx(1.21)


def test_cash_earns_nothing() -> None:
    closes, opens = _prices({"QQQ": [100.0, 100.0, 200.0]})
    simulator = PortfolioSimulator(closes=closes, opens=opens)
    result = simulator.run({_days(3)[0]: {Asset.CASH: 1.0}})
    assert result.nav.iloc[-1] == pytest.approx(1.0)


def test_a_half_cash_book_earns_half_the_move() -> None:
    closes, opens = _prices({"QQQ": [100.0, 100.0, 120.0]})
    simulator = PortfolioSimulator(closes=closes, opens=opens)
    result = simulator.run({_days(3)[0]: {Asset.QQQ: 0.5, Asset.CASH: 0.5}})
    assert result.nav.iloc[-1] == pytest.approx(1.10)


def test_weights_drift_between_rebalances() -> None:
    """An untraded book is not a constant-weight book.

    ``BACKTEST_SPEC.md`` 16 only rebalances on a regime change, so drift is the
    normal state of the portfolio and has to be modelled.
    """
    closes, opens = _prices(
        {"QQQ": [100.0, 100.0, 200.0], "QLD": [100.0, 100.0, 100.0]}
    )
    simulator = PortfolioSimulator(closes=closes, opens=opens)
    result = simulator.run({_days(3)[0]: {Asset.QQQ: 0.5, Asset.QLD: 0.5}})

    final = result.weights.iloc[-1]
    assert final[Asset.QQQ] == pytest.approx(2 / 3)
    assert final[Asset.QLD] == pytest.approx(1 / 3)


def test_target_leverage_is_tracked_daily() -> None:
    closes, opens = _flat_prices(3)
    simulator = PortfolioSimulator(closes=closes, opens=opens)
    result = simulator.run({_days(3)[0]: {Asset.QLD: 0.7, Asset.CASH: 0.3}})
    assert result.target_leverage.iloc[-1] == pytest.approx(1.4)


def test_targets_must_be_portfolios_of_priced_sleeves() -> None:
    closes, opens = _flat_prices(3)
    simulator = PortfolioSimulator(closes=closes, opens=opens)
    with pytest.raises(SimulationError, match="unpriced sleeves"):
        simulator.run({_days(3)[0]: {Asset.TQQQ: 1.0}})
    with pytest.raises(SimulationError, match=r"not 1\.0"):
        simulator.run({_days(3)[0]: {Asset.QQQ: 0.5}})


# ------------------------------------------------------------------ TASK-073


def test_an_unchanged_target_does_not_trade() -> None:
    closes, opens = _flat_prices(5)
    simulator = PortfolioSimulator(closes=closes, opens=opens)
    days = _days(5)
    targets = {day: {Asset.QQQ: 1.0} for day in days}

    result = simulator.run(targets)
    assert result.trade_count == 1, "only the initial entry"
    assert result.total_turnover == pytest.approx(1.0)


def test_a_changed_target_trades_once() -> None:
    closes, opens = _flat_prices(5)
    simulator = PortfolioSimulator(closes=closes, opens=opens)
    days = _days(5)
    targets = {days[0]: {Asset.QQQ: 1.0}, days[2]: {Asset.QLD: 1.0}}

    result = simulator.run(targets)
    assert result.trade_count == 2
    assert [trade.trade_date for trade in result.trades] == [days[1], days[3]]


def test_costs_reduce_nav_at_the_rebalance() -> None:
    closes, opens = _flat_prices(4)
    model = CostModel(commission_bps=10.0)
    simulator = PortfolioSimulator(closes=closes, opens=opens, cost_model=model)
    days = _days(4)

    result = simulator.run({days[0]: {Asset.QQQ: 1.0}, days[1]: {Asset.QLD: 1.0}})
    # Entry trades 1.0 notional, the switch trades 2.0. Prices never move.
    expected = (1 - model.cost_of(1.0)) * (1 - model.cost_of(2.0))
    assert result.nav.iloc[-1] == pytest.approx(expected)


def test_the_cost_free_run_is_strictly_better() -> None:
    closes, opens = _flat_prices(4)
    days = _days(4)
    targets = {days[0]: {Asset.QQQ: 1.0}, days[1]: {Asset.QLD: 1.0}}

    free = PortfolioSimulator(closes=closes, opens=opens).run(targets)
    charged = PortfolioSimulator(
        closes=closes, opens=opens, cost_model=CostModel(spread_bps=5.0)
    ).run(targets)
    assert charged.nav.iloc[-1] < free.nav.iloc[-1]


def test_a_forced_rebalance_restores_drifted_weights() -> None:
    closes, opens = _prices(
        {"QQQ": [100.0, 100.0, 200.0, 200.0], "QLD": [100.0, 100.0, 100.0, 100.0]}
    )
    simulator = PortfolioSimulator(closes=closes, opens=opens)
    days = _days(4)
    targets = {day: {Asset.QQQ: 0.5, Asset.QLD: 0.5} for day in days}

    result = simulator.run(targets, force_rebalance_dates=[days[3]])
    assert result.weights.loc[days[3], Asset.QQQ] == pytest.approx(0.5)
    assert result.trade_count == 2


# ------------------------------------------------------------------ TASK-074


def test_the_three_required_benchmarks_are_defined() -> None:
    names = [spec.name for spec in standard_benchmarks()]
    assert names == ["QQQ 100%", "QLD 100%", "QLD 70% / Cash 30%"]


def test_benchmarks_run_through_the_same_simulator_and_cost_model() -> None:
    # BACKTEST_SPEC.md 17 — identical treatment is checked, not assumed.
    closes, opens = _prices(
        {"QQQ": [100.0, 100.0, 110.0], "QLD": [100.0, 100.0, 120.0]}
    )
    model = CostModel(commission_bps=10.0)
    simulator = PortfolioSimulator(closes=closes, opens=opens, cost_model=model)

    for spec in standard_benchmarks():
        targets, forced = build_targets(spec, closes.index)
        result = simulator.run(targets, name=spec.name, force_rebalance_dates=forced)
        assert result.cost_model is model
        assert result.nav.iloc[0] == pytest.approx(1.0)


def test_a_static_benchmark_enters_on_the_next_day_like_the_strategy() -> None:
    closes, opens = _prices({"QQQ": [100.0, 200.0, 200.0], "QLD": [100.0, 100.0, 100.0]})
    simulator = PortfolioSimulator(closes=closes, opens=opens)
    spec = BenchmarkSpec(name="QQQ", weights={Asset.QQQ: 1.0})
    targets, forced = build_targets(spec, closes.index)

    result = simulator.run(targets, name=spec.name, force_rebalance_dates=forced)
    assert result.nav.iloc[1] == pytest.approx(1.0), "no same-day execution for benchmarks either"


def test_monthly_rebalance_dates_are_period_ends() -> None:
    index = pd.Index([date(2024, 1, day) for day in range(1, 32)])
    dates = rebalance_dates(index, "monthly")
    assert dates == [date(2024, 1, 31)]
    assert rebalance_dates(index, "none") == []


def test_a_drifting_mix_stops_describing_itself() -> None:
    """Why the 70/30 benchmark defaults to rebalancing.

    A 2x sleeve left to drift takes over the book, so an unrebalanced
    "QLD 70 / Cash 30" is not a 70/30 portfolio for most of the sample.
    """
    count = 120
    index = pd.Index(_days(count), name="observation_date")
    growth = [100.0 * (1.02**i) for i in range(count)]
    closes = DataFrame({"QLD": growth}, index=index)
    simulator = PortfolioSimulator(closes=closes, opens=closes)

    drifting = BenchmarkSpec("drift", {Asset.QLD: 0.7, Asset.CASH: 0.3}, rebalance="none")
    targets, forced = build_targets(drifting, index)
    result = simulator.run(targets, force_rebalance_dates=forced)
    assert result.weights.iloc[-1][Asset.QLD] > 0.9

    monthly = BenchmarkSpec("monthly", {Asset.QLD: 0.7, Asset.CASH: 0.3}, rebalance="monthly")
    targets, forced = build_targets(monthly, index)
    rebalanced = simulator.run(targets, force_rebalance_dates=forced)
    assert rebalanced.trade_count > result.trade_count


def test_benchmark_weights_must_be_a_portfolio() -> None:
    with pytest.raises(ValueError, match=r"not 1\.0"):
        BenchmarkSpec(name="bad", weights={Asset.QLD: 0.7})


# ------------------------------------------------------------------- metrics


def test_drawdown_and_recovery() -> None:
    nav = pd.Series(
        [1.0, 1.2, 0.6, 0.9, 1.3],
        index=pd.Index(_days(5), name="observation_date"),
    )
    depths = drawdown_series(nav)
    assert depths.max() == pytest.approx(0.5)
    assert recovery_days(nav) == 2


def test_a_series_that_never_recovers_reports_none() -> None:
    nav = pd.Series([1.0, 2.0, 0.5, 0.6], index=pd.Index(_days(4)))
    assert recovery_days(nav) is None


def test_metrics_cover_the_required_set() -> None:
    rng = np.random.default_rng(19)
    count = 800
    index = pd.Index(_days(count), name="observation_date")
    nav = pd.Series(np.cumprod(1 + rng.normal(0.0005, 0.012, count)), index=index)

    metrics = compute_metrics(nav, name="strategy")
    for field_name in (
        "cagr",
        "total_return",
        "volatility",
        "max_drawdown",
        "sharpe",
        "sortino",
        "calmar",
        "worst_year",
        "recovery_days",
        "time_under_water",
        "turnover",
    ):
        assert hasattr(metrics, field_name), field_name
    assert metrics.summary()


def test_cagr_matches_a_known_path() -> None:
    index = pd.Index([date(2020, 1, 1), date(2022, 1, 1)])
    nav = pd.Series([1.0, 1.21], index=index)
    metrics = compute_metrics(nav, name="x")
    assert metrics.total_return == pytest.approx(0.21)
    assert metrics.cagr == pytest.approx(0.10, abs=1e-3)


def test_metrics_read_straight_off_a_result() -> None:
    closes, opens = _flat_prices(10)
    simulator = PortfolioSimulator(
        closes=closes, opens=opens, cost_model=CostModel(commission_bps=5.0)
    )
    result = simulator.run({_days(10)[0]: {Asset.QQQ: 1.0}}, name="test")
    metrics = metrics_from_result(result, regime_change_count=3)

    assert metrics.name == "test"
    assert metrics.trade_count == 1
    assert metrics.turnover == pytest.approx(1.0)
    assert metrics.regime_change_count == 3


# ------------------------------------------------------------- cash accrual
#
# The strategy sits in cash roughly a fifth of the time, so paying it nothing
# is not neutral - it systematically understates every defensive rule.
#
# Note the T+1 offset throughout: a target decided on day 0 is executed on
# day 1, so accrual only starts from day 1.


def _cash_only_prices(days: list[date]) -> DataFrame:
    return DataFrame({Asset.QQQ.value: [100.0] * len(days)}, index=days)


def _run_cash(days: list[date], rates: Series | None, weights=None):
    sim = PortfolioSimulator(
        closes=_cash_only_prices(days),
        execution_timing=ExecutionTiming.NEXT_CLOSE,
        cash_rates=rates,
    )
    return sim.run({days[0]: weights or {Asset.CASH: 1.0}})


def _accrued(days: list[date], annual_pct: float, cash_weight: float = 1.0) -> float:
    """ACT/365 compounding, reimplemented independently of the simulator."""
    held, value = days[1:], 1.0
    for start, finish in pairwise(held):
        value *= 1 + cash_weight * annual_pct / 100 * (finish - start).days / 365
    return value


def test_cash_earns_nothing_without_a_rate() -> None:
    days = [date(2024, 1, 1) + timedelta(n) for n in range(11)]
    assert _run_cash(days, None).nav.iloc[-1] == pytest.approx(1.0)


def test_cash_accrues_the_quoted_rate() -> None:
    days = [date(2023, 1, 1) + timedelta(n) for n in range(400)]
    result = _run_cash(days, Series(5.0, index=days))
    assert result.nav.iloc[-1] == pytest.approx(_accrued(days, 5.0), rel=1e-12)


def test_a_full_year_of_cash_returns_about_the_quoted_rate() -> None:
    """The economic claim, not just the arithmetic: 5% cash pays ~5% a year."""
    days = [date(2023, 1, 1) + timedelta(n) for n in range(367)]
    result = _run_cash(days, Series(5.0, index=days))
    assert result.nav.iloc[-1] == pytest.approx(1.05, abs=0.002)


def test_a_weekend_is_paid() -> None:
    """Bills accrue every calendar day, not only the ones the market opens."""
    thursday, friday, monday = date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8)
    days = [thursday, friday, monday]
    result = _run_cash(days, Series(5.0, index=days))
    assert result.nav.iloc[-1] == pytest.approx(1 + 0.05 * 3 / 365, rel=1e-12)


def test_the_rate_used_is_the_one_known_when_the_period_began() -> None:
    """No return may be earned at a rate published after the fact."""
    days = [date(2024, 1, 1) + timedelta(n) for n in range(3)]
    # The spike lands on the final day; it must not pay the period before it.
    result = _run_cash(days, Series([2.0, 2.0, 99.0], index=days))
    assert result.nav.iloc[-1] == pytest.approx(1 + 0.02 / 365, rel=1e-12)


def test_a_missing_rate_earns_nothing_rather_than_carrying_a_stale_one() -> None:
    days = [date(2024, 1, 1) + timedelta(n) for n in range(4)]
    #                     held from here ^^^ , and day 1's rate is unknown
    result = _run_cash(days, Series([5.0, float("nan"), 5.0, 5.0], index=days))
    assert result.nav.iloc[-1] == pytest.approx(1 + 0.05 / 365, rel=1e-12)


def test_cash_rates_must_be_sorted() -> None:
    days = [date(2024, 1, 1) + timedelta(n) for n in range(3)]
    with pytest.raises(SimulationError, match="sorted"):
        PortfolioSimulator(
            closes=_cash_only_prices(days),
            execution_timing=ExecutionTiming.NEXT_CLOSE,
            cash_rates=Series([5.0] * 3, index=list(reversed(days))),
        )


def test_only_the_cash_weight_earns_the_rate() -> None:
    days = [date(2024, 1, 1) + timedelta(n) for n in range(3)]
    result = _run_cash(
        days, Series(10.0, index=days), weights={Asset.QQQ: 0.5, Asset.CASH: 0.5}
    )
    assert result.nav.iloc[-1] == pytest.approx(_accrued(days, 10.0, 0.5), rel=1e-12)

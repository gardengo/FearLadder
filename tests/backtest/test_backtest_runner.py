"""End-to-end backtest: prices in, NAV and benchmarks out."""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame

from fear_ladder.backtest.costs import CostModel
from fear_ladder.constants import UNKNOWN_REGIME, Asset, ExecutionTiming
from fear_ladder.research.backtest_runner import MarketData, StrategyBacktest

START = date(2015, 1, 1)
LENGTH = 1400


def _index(length: int = LENGTH) -> pd.Index:
    return pd.Index(
        [START + timedelta(days=offset) for offset in range(length)],
        name="observation_date",
    )


@lru_cache(maxsize=4)
def _market(seed: int = 42, length: int = LENGTH) -> MarketData:
    """Synthetic but structurally realistic inputs.

    QLD/TQQQ are generated as 2x/3x daily moves of QQQ, so the leverage
    relationship the strategy reasons about actually holds in the fixture.
    """
    rng = np.random.default_rng(seed)
    index = _index(length)
    daily = rng.normal(0.0004, 0.012, length)

    qqq = 100.0 * np.cumprod(1 + daily)
    qld = 100.0 * np.cumprod(1 + 2 * daily)
    tqqq = 100.0 * np.cumprod(1 + 3 * daily)

    closes = DataFrame({"QQQ": qqq, "QLD": qld, "TQQQ": tqqq}, index=index)
    # Opens sit between the previous close and today's close.
    opens = closes.shift(1).fillna(closes.iloc[0]) * 0.5 + closes * 0.5

    vix = np.abs(rng.normal(18, 5, length)) + np.abs(daily) * 300
    fng = np.clip(50 + daily.cumsum() * 200 + rng.normal(0, 10, length), 0, 100)
    aaii = np.clip(rng.normal(0.05, 0.12, length), -1, 1)

    series = closes.copy()
    series["VIX"] = vix
    series["CNN_FEAR_GREED"] = fng
    series["AAII_SENTIMENT"] = aaii
    return MarketData(series=series, closes=closes, opens=opens)


@pytest.fixture(scope="module")
def run(placeholder_config):
    """One backtest, shared by every test that only inspects it."""
    return StrategyBacktest(placeholder_config).run(_market())


def test_the_whole_chain_runs(run) -> None:
    assert run.strategy_version == "v0.0-placeholder"
    assert run.parameter_status == "RESEARCH_PLACEHOLDER"
    assert not run.result.nav.empty
    assert run.result.nav.iloc[0] == pytest.approx(1.0)
    assert run.metrics.summary()


def test_the_backtest_produces_regimes_and_allocations(placeholder_config, run) -> None:
    labels = set(placeholder_config.strategy.regime.labels)
    observed = set(run.regimes.unique()) - {UNKNOWN_REGIME}
    assert observed <= labels
    assert observed, "the synthetic market should visit at least one regime"
    assert run.allocations


def test_allocation_weights_are_always_portfolios(run) -> None:
    for decision in run.allocations.values():
        assert decision.allocation is not None
        assert sum(decision.allocation.weights.values()) == pytest.approx(1.0)
        assert all(weight >= 0 for weight in decision.allocation.weights.values())


def test_no_allocation_is_produced_for_unknown_days(run) -> None:
    unknown_days = set(run.regimes[run.regimes == UNKNOWN_REGIME].index)
    assert not (unknown_days & set(run.allocations))


def test_the_three_benchmarks_are_run_on_the_same_prices(run) -> None:
    assert set(run.benchmarks) == {"QQQ 100%", "QLD 100%", "QLD 70% / Cash 30%"}
    for result in run.benchmarks.values():
        assert result.nav.index.equals(run.result.nav.index)
        assert result.cost_model is run.result.cost_model


def test_the_comparison_table_lines_everything_up(run) -> None:
    table = run.comparison()
    assert "strategy" in table.index
    assert {"cagr", "max_drawdown", "sharpe", "calmar", "turnover"} <= set(table.columns)


def test_a_backtest_is_reproducible(placeholder_config) -> None:
    # BACKTEST_SPEC.md 25 — the same inputs must give the same output.
    data = _market()
    first = StrategyBacktest(placeholder_config).run(data)
    second = StrategyBacktest(placeholder_config).run(data)
    pd.testing.assert_series_equal(first.result.nav, second.result.nav)
    pd.testing.assert_series_equal(first.regimes, second.regimes)


def test_costs_reduce_the_strategy_but_not_the_ranking_machinery(
    placeholder_config,
) -> None:
    data = _market()
    free = StrategyBacktest(placeholder_config, cost_model=CostModel.zero()).run(data)
    charged = StrategyBacktest(
        placeholder_config, cost_model=CostModel(commission_bps=20.0)
    ).run(data)

    assert charged.result.nav.iloc[-1] <= free.result.nav.iloc[-1]
    assert charged.regime_change_count == free.regime_change_count, (
        "costs must not change the signal, only the realised NAV"
    )


def test_a_window_restricts_the_sample(placeholder_config) -> None:
    data = _market()
    cutoff = START + timedelta(days=900)
    run = StrategyBacktest(placeholder_config).run(data, end=cutoff)
    assert run.result.nav.index.max() <= cutoff


def test_the_execution_rule_is_taken_from_configuration(run) -> None:
    assert run.result.execution_timing is ExecutionTiming.NEXT_OPEN


def test_market_data_rejects_untradable_sleeves() -> None:
    index = _index(5)
    frame = DataFrame({"SPY": [1.0] * 5}, index=index)
    with pytest.raises(ValueError, match="untradable columns"):
        MarketData(series=frame, closes=frame)


def test_the_daily_regime_frame_is_dashboard_ready(run) -> None:
    frame = run.daily_regime_frame()
    assert {"composite_score", "regime", "target_leverage", "nav"} <= set(frame.columns)


def test_tqqq_is_only_ever_held_when_the_gate_opened(run) -> None:
    for day, decision in run.allocations.items():
        assert decision.allocation is not None
        if decision.allocation.weight(Asset.TQQQ) > 0:
            assert decision.gate is not None and decision.gate.allowed, day

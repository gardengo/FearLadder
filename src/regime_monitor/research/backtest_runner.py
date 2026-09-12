"""End-to-end strategy backtest (research path).

``BACKTEST_SPEC.md`` 28 requires the research and production paths to be
separated *in code*, not merely by convention::

    Live daily runner가 연구용 최적화 코드를 호출해서 parameter를
    변경해서는 안 된다.

Everything under :mod:`regime_monitor.research` is the research path. It reads
historical data and candidate parameters and produces a backtest. Nothing in
:mod:`regime_monitor.pipeline` imports from here, and a test enforces that.

The chain assembled here is the same one the daily worker runs, which is the
point: a regime the backtest produced for 2020-03-16 and a regime the live
worker would have produced that day come from identical code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from pandas import DataFrame, Series

from regime_monitor.allocation.engine import AllocationDecision, AllocationEngine
from regime_monitor.allocation.tqqq_gate import GateContext
from regime_monitor.backtest.benchmarks import BenchmarkSpec, build_targets, standard_benchmarks
from regime_monitor.backtest.costs import CostModel
from regime_monitor.backtest.metrics import (
    PerformanceMetrics,
    comparison_frame,
    metrics_from_result,
)
from regime_monitor.backtest.simulator import BacktestResult, PortfolioSimulator
from regime_monitor.config.schema import AppConfig
from regime_monitor.constants import UNKNOWN_REGIME, Asset
from regime_monitor.indicators.engine import IndicatorEngine, IndicatorResult
from regime_monitor.regime.classifier import RegimeScale
from regime_monitor.regime.transition import TransitionDecision, TransitionEngine
from regime_monitor.scoring.composite import ScoreEngine

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarketData:
    """Historical inputs for a backtest.

    ``series`` holds every input the indicators read (prices and scalars).
    ``closes``/``opens`` hold only the tradable sleeves, because only those can
    be held in the portfolio.
    """

    series: DataFrame
    closes: DataFrame
    opens: DataFrame | None = None

    def __post_init__(self) -> None:
        if self.series.empty:
            raise ValueError("no input series")
        if self.closes.empty:
            raise ValueError("no tradable price history")
        unknown = {str(column) for column in self.closes.columns} - {
            asset.value for asset in Asset
        }
        if unknown:
            raise ValueError(f"closes contains untradable columns: {sorted(unknown)}")

    def window(self, start: date | None, end: date | None) -> MarketData:
        def cut(frame: DataFrame | None) -> DataFrame | None:
            if frame is None:
                return None
            mask = Series(True, index=frame.index)
            if start is not None:
                mask &= Series(frame.index >= start, index=frame.index)
            if end is not None:
                mask &= Series(frame.index <= end, index=frame.index)
            return frame.loc[mask]

        return MarketData(
            series=cut(self.series),  # type: ignore[arg-type]
            closes=cut(self.closes),  # type: ignore[arg-type]
            opens=cut(self.opens),
        )


@dataclass(frozen=True, slots=True)
class BacktestRun:
    """Everything one strategy backtest produced."""

    strategy_version: str
    parameter_status: str
    result: BacktestResult
    metrics: PerformanceMetrics
    benchmarks: dict[str, BacktestResult] = field(default_factory=dict)
    benchmark_metrics: dict[str, PerformanceMetrics] = field(default_factory=dict)
    composite_score: Series = field(default_factory=Series)
    indicator_scores: DataFrame = field(default_factory=DataFrame)
    regimes: Series = field(default_factory=Series)
    decisions: tuple[TransitionDecision, ...] = ()
    allocations: dict[date, AllocationDecision] = field(default_factory=dict)

    @property
    def regime_change_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.changed)

    def comparison(self) -> DataFrame:
        return comparison_frame([self.metrics, *self.benchmark_metrics.values()])

    def daily_regime_frame(self) -> DataFrame:
        return DataFrame(
            {
                "composite_score": self.composite_score,
                "regime": self.regimes,
                "target_leverage": self.result.target_leverage,
                "nav": self.result.nav,
            }
        )


@dataclass(frozen=True, slots=True)
class StrategyBacktest:
    """Runs the full chain for one candidate parameter set."""

    config: AppConfig
    cost_model: CostModel | None = None

    def run(
        self,
        data: MarketData,
        *,
        start: date | None = None,
        end: date | None = None,
        include_benchmarks: bool = True,
        name: str = "strategy",
    ) -> BacktestRun:
        strategy = self.config.strategy
        windowed = data.window(start, end)

        scores, indicator_results = self._score(windowed)
        composite = ScoreEngine.from_config(self.config).composite_series(scores)

        scale = RegimeScale.from_spec(strategy.regime)
        transitions = TransitionEngine.from_spec(scale, strategy.transition)
        decisions = transitions.run(composite)

        allocations = self._allocate(decisions, indicator_results)
        targets = {
            day: decision.allocation.weights
            for day, decision in allocations.items()
            if decision.allocation is not None
        }

        simulator = PortfolioSimulator(
            closes=windowed.closes,
            opens=windowed.opens,
            cost_model=self.cost_model or CostModel.from_spec(strategy.cost_model),
            execution_timing=strategy.execution.timing,
        )
        result = simulator.run(targets, name=name)
        regime_changes = sum(1 for decision in decisions if decision.changed)
        metrics = metrics_from_result(result, regime_change_count=regime_changes)

        benchmarks: dict[str, BacktestResult] = {}
        benchmark_metrics: dict[str, PerformanceMetrics] = {}
        if include_benchmarks:
            benchmarks, benchmark_metrics = self._run_benchmarks(simulator, windowed)

        return BacktestRun(
            strategy_version=strategy.strategy_version,
            parameter_status=strategy.parameter_status.value,
            result=result,
            metrics=metrics,
            benchmarks=benchmarks,
            benchmark_metrics=benchmark_metrics,
            composite_score=composite,
            indicator_scores=scores,
            regimes=Series(
                [decision.regime for decision in decisions],
                index=pd.Index(
                    [decision.observation_date for decision in decisions],
                    name="observation_date",
                ),
                name="regime",
            ),
            decisions=tuple(decisions),
            allocations=allocations,
        )

    # -- steps -------------------------------------------------------------
    def _score(self, data: MarketData) -> tuple[DataFrame, dict[str, IndicatorResult]]:
        sources = {
            str(column): data.series[column].dropna() for column in data.series.columns
        }
        indicator_results = IndicatorEngine(self.config.indicators).compute_all(sources)
        scores = ScoreEngine.from_config(self.config).normalize(indicator_results)
        return scores, indicator_results

    def _allocate(
        self,
        decisions: list[TransitionDecision],
        indicator_results: dict[str, IndicatorResult],
    ) -> dict[date, AllocationDecision]:
        engine = AllocationEngine.from_config(self.config)

        # One wide frame, converted once. Looking each indicator up per day with
        # .loc would be ~20 lookups x every trading day, which dominates the run
        # and would make the Phase 8 parameter search unusable.
        values = DataFrame(
            {name: result.values for name, result in indicator_results.items()}
        )
        by_day: dict[date, dict[str, float | None]] = {
            day: {
                name: (None if pd.isna(value) else float(value))
                for name, value in row.items()
            }
            for day, row in values.to_dict(orient="index").items()
        }

        allocations: dict[date, AllocationDecision] = {}
        for decision in decisions:
            if decision.regime == UNKNOWN_REGIME:
                continue
            context = GateContext(
                observation_date=decision.observation_date,
                indicator_values=by_day.get(decision.observation_date, {}),
                composite_score=decision.score,
                regime=decision.regime,
            )
            allocations[decision.observation_date] = engine.allocate(
                decision.regime, decision.observation_date, context=context
            )
        return allocations

    def _run_benchmarks(
        self, simulator: PortfolioSimulator, data: MarketData
    ) -> tuple[dict[str, BacktestResult], dict[str, PerformanceMetrics]]:
        available = {Asset(str(column)) for column in data.closes.columns} | {Asset.CASH}
        results: dict[str, BacktestResult] = {}
        metrics: dict[str, PerformanceMetrics] = {}
        for spec in standard_benchmarks():
            if not set(spec.weights) <= available:
                logger.info("skipping benchmark %s: sleeves not priced", spec.name)
                continue
            results[spec.name], metrics[spec.name] = self._run_benchmark(
                simulator, data, spec
            )
        return results, metrics

    @staticmethod
    def _run_benchmark(
        simulator: PortfolioSimulator, data: MarketData, spec: BenchmarkSpec
    ) -> tuple[BacktestResult, PerformanceMetrics]:
        targets, forced = build_targets(spec, data.closes.index)
        result = simulator.run(targets, name=spec.name, force_rebalance_dates=forced)
        return result, metrics_from_result(result)

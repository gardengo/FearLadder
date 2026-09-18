"""Backtest artifacts (``BACKTEST_SPEC.md`` 26) and run provenance (§25).

The artifact layout is prescribed::

    reports/backtest/
    ├── summary.json
    ├── metrics.json
    ├── daily_portfolio.csv
    ├── daily_regime.csv
    ├── daily_indicators.csv
    ├── trades.csv
    ├── parameters.json
    └── charts/

``parameters.json`` and ``summary.json`` together answer "what exactly produced
this?", which is what makes a result reproducible rather than merely repeatable:
strategy version, parameter status, data window, execution rule, cost model and
the code commit.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pandas import DataFrame

from fear_ladder import paths
from fear_ladder.config.schema import AppConfig
from fear_ladder.research.backtest_runner import BacktestRun

logger = logging.getLogger(__name__)


def write_json_report(path: Path, payload: Any) -> Path:
    """Write one research report, in the one format they all share.

    Every script under ``scripts/`` that produces a file in ``reports/`` wrote
    this same four-argument ``json.dumps`` call; they are read side by side and
    diffed against each other, so the formatting has to match exactly.

    ``sort_keys`` makes a re-run's diff show what changed rather than how the
    dict happened to be built, ``ensure_ascii=False`` keeps the Korean notes
    readable in the file itself, and the trailing newline keeps git from
    reporting every report as having no final line.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    return path


def code_commit() -> str | None:
    """Current git commit, so a report can be traced back to its code."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=paths.PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


@dataclass(frozen=True, slots=True)
class ReportWriter:
    """Writes one backtest's artifacts into a directory."""

    output_dir: Path = paths.BACKTEST_REPORT_DIR

    def write(self, run: BacktestRun, config: AppConfig) -> dict[str, Path]:
        directory = self.output_dir
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "charts").mkdir(exist_ok=True)

        written: dict[str, Path] = {}
        written["summary.json"] = self._json(
            directory / "summary.json", self._summary(run, config)
        )
        written["metrics.json"] = self._json(
            directory / "metrics.json",
            {
                "strategy": run.metrics.to_dict(),
                "benchmarks": {
                    name: metrics.to_dict()
                    for name, metrics in run.benchmark_metrics.items()
                },
            },
        )
        written["parameters.json"] = self._json(
            directory / "parameters.json", _parameters(config)
        )
        written["daily_portfolio.csv"] = self._csv(
            directory / "daily_portfolio.csv", _portfolio_frame(run)
        )
        written["daily_regime.csv"] = self._csv(
            directory / "daily_regime.csv", run.daily_regime_frame()
        )
        written["daily_indicators.csv"] = self._csv(
            directory / "daily_indicators.csv", run.indicator_scores
        )
        written["trades.csv"] = self._csv(
            directory / "trades.csv", run.result.trades_frame()
        )
        logger.info("wrote %d backtest artifacts to %s", len(written), directory)
        return written

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _json(path: Path, payload: dict[str, Any]) -> Path:
        return write_json_report(path, payload)

    @staticmethod
    def _csv(path: Path, frame: DataFrame) -> Path:
        frame.to_csv(path, encoding="utf-8")
        return path

    @staticmethod
    def _summary(run: BacktestRun, config: AppConfig) -> dict[str, Any]:
        """The reproducibility record ``BACKTEST_SPEC.md`` 25 asks for."""
        nav = run.result.nav
        return {
            "strategy_version": run.strategy_version,
            "parameter_status": run.parameter_status,
            "data_version": config.strategy.data_version,
            "parameter_version": config.strategy.parameter_version,
            "code_commit": code_commit(),
            "start_date": str(nav.index.min()),
            "end_date": str(nav.index.max()),
            "execution_rule": config.strategy.execution.timing.value,
            "same_day_execution_allowed": False,
            "cost_model": run.result.cost_model.describe(),
            "run_timestamp": datetime.now(tz=UTC).isoformat(),
            "regime_change_count": run.regime_change_count,
            "trade_count": run.result.trade_count,
            "headline": run.metrics.summary(),
            "benchmarks": {
                name: metrics.summary() for name, metrics in run.benchmark_metrics.items()
            },
            "warning": (
                "RESEARCH_PLACEHOLDER parameters: this result describes the "
                "machinery, not a strategy."
                if run.parameter_status == "RESEARCH_PLACEHOLDER"
                else None
            ),
        }


def _parameters(config: AppConfig) -> dict[str, Any]:
    strategy = config.strategy
    return {
        "strategy_version": strategy.strategy_version,
        "parameter_status": strategy.parameter_status.value,
        "frozen_at": strategy.frozen_at,
        "score": strategy.score.model_dump(),
        "regime": strategy.regime.model_dump(),
        "transition": strategy.transition.model_dump(),
        "allocation": strategy.allocation.model_dump(),
        "tqqq_gate": strategy.tqqq_gate.model_dump(),
        "execution": strategy.execution.model_dump(),
        "cost_model": strategy.cost_model.model_dump(),
        "dataset_split": strategy.dataset_split.model_dump(),
        "indicators": {
            name: {
                "compute": spec.compute,
                "params": spec.params,
                "source": spec.source,
                "direction": spec.direction.value,
                "normalization": spec.normalization.model_dump(),
            }
            for name, spec in config.indicators.enabled_indicators.items()
        },
        "unresolved_parameters": list(config.unresolved_parameters()),
    }


def _portfolio_frame(run: BacktestRun) -> DataFrame:
    frame = run.result.weights.copy()
    frame.columns = [f"w_{column.value}" for column in frame.columns]
    frame["nav"] = run.result.nav
    frame["target_leverage"] = run.result.target_leverage
    frame["turnover"] = run.result.turnover
    frame["cost"] = run.result.costs
    for name, benchmark in run.benchmarks.items():
        frame[f"nav_{name}"] = benchmark.nav
    return frame

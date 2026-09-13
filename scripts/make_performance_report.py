"""Regenerate ``reports/performance.json`` — the dashboard's evidence file.

    python scripts/make_performance_report.py

Runs the frozen strategy over every day of stored history, builds the static
equity/cash benchmark grid beside it, and writes the comparison out as JSON so
``app/streamlit_app.py`` can render it without a research database.

This is a *reporting* step, not a research step. It reads the frozen profile and
changes nothing: if the numbers here look wrong, the fix is a new freeze, not an
edit to this file or its output.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.constants import Asset
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.research.backtest_runner import StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.performance import (
    cash_curve,
    episode_returns,
    risk_matched,
    static_mix,
    summarise_rolling,
    window_stats,
)

logger = logging.getLogger("performance")

OUTPUT = paths.REPORTS_DIR / "performance.json"
STRATEGY = "전략"
#: Equity weights for the static grid. Ten-point steps are fine enough to find
#: the mix that matches the strategy's drawdown and coarse enough to read.
WEIGHTS = (100, 90, 80, 70, 60, 50, 40, 30, 20, 10)
ETFS = ("QQQ", "QLD", "TQQQ")
ROLLING_YEARS = (3, 5, 10, 20)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    return parser


def _label(symbol: str, percent: int) -> str:
    return symbol if percent == 100 else f"{symbol} {percent}%/현금 {100 - percent}%"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    strategy = config.strategy
    logger.info("%s (%s)", strategy.strategy_version, strategy.parameter_status.value)

    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)

    closes = data.closes
    first = max(closes[symbol].dropna().index[0] for symbol in ETFS)
    run = StrategyBacktest(config).run(data, start=first, include_benchmarks=False)
    nav = run.result.nav
    days = list(nav.index)
    logger.info("%d trading days, %s .. %s", len(days), days[0], days[-1])

    cash = cash_curve(data.cash_rates, days)
    navs = {STRATEGY: nav / nav.iloc[0], "현금": cash}
    for symbol in ETFS:
        series = closes[symbol][days]
        navs[symbol] = series / series.iloc[0]
        for percent in WEIGHTS:
            if percent == 100:
                continue
            navs[_label(symbol, percent)] = static_mix(
                navs[symbol], cash, percent / 100
            )

    overall = {name: window_stats(series, cash) for name, series in navs.items()}
    reference, _ = _rolling_reference(navs[STRATEGY])
    rolling = {
        name: [
            summary.to_dict()
            for years in ROLLING_YEARS
            if (
                summary := summarise_rolling(
                    series, years, reference=reference.get(years)
                )
            )
            is not None
        ]
        for name, series in navs.items()
    }

    weights = strategy.score.weights or {}
    ladder = {
        label: sum(
            weight * _leverage(asset)
            for asset, weight in (strategy.allocation.mappings or {}).get(label, {}).items()
        )
        for label in (strategy.regime.labels or ())
    }

    report = {
        "strategy_version": strategy.strategy_version,
        "parameter_status": strategy.parameter_status.value,
        "frozen_at": strategy.frozen_at.isoformat() if strategy.frozen_at else None,
        "window": {"start": days[0].isoformat(), "end": days[-1].isoformat()},
        "overall": overall,
        "rolling": rolling,
        "episodes": [episode.to_dict() for episode in episode_returns(navs, days)],
        "risk_matched": risk_matched(
            overall[STRATEGY],
            {name: stats for name, stats in overall.items() if name != STRATEGY},
        ),
        "parameters": {
            "indicator_weights": dict(sorted(weights.items(), key=lambda kv: -kv[1])),
            "regime_labels": list(strategy.regime.labels or ()),
            "regime_boundaries": list(strategy.regime.boundaries or ()),
            "ladder": ladder,
            "trend_filter": {
                "indicator": strategy.trend_filter.indicator,
                "threshold": strategy.trend_filter.threshold,
                "reentry_threshold": strategy.trend_filter.reentry_threshold,
                "max_leverage_below": strategy.trend_filter.max_leverage_below,
                "depth_indicator": strategy.trend_filter.depth_indicator,
                "min_depth_to_engage": strategy.trend_filter.min_depth_to_engage,
            },
            "transition": {
                "confirmation_days": strategy.transition.confirmation_days,
                "hysteresis": strategy.transition.hysteresis,
                "minimum_duration_days": strategy.transition.minimum_duration_days,
            },
            "execution": {
                "timing": strategy.execution.timing.value,
                "rebalance_on": strategy.execution.rebalance_on,
                "commission_bps": strategy.cost_model.commission_bps,
            },
            "splits": {
                "research": [
                    strategy.dataset_split.research_start.isoformat(),
                    strategy.dataset_split.research_end.isoformat(),
                ],
                "validation": [
                    strategy.dataset_split.validation_start.isoformat(),
                    strategy.dataset_split.validation_end.isoformat(),
                ],
                "oos": [
                    strategy.dataset_split.oos_start.isoformat(),
                    strategy.dataset_split.oos_end.isoformat(),
                ],
            },
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.info("wrote %s", args.out)
    stats = overall[STRATEGY]
    logger.info(
        "strategy over %.1f years: CAGR %.2f%%  MDD %.1f%%  Sharpe %.2f",
        stats["years"],
        stats["cagr"] * 100,
        stats["max_drawdown"] * 100,
        stats["sharpe"],
    )
    return 0


def _rolling_reference(nav):
    """The strategy's own rolling CAGRs, so every benchmark is scored against it."""
    from fear_ladder.research.performance import rolling_cagrs

    reference = {}
    for years in ROLLING_YEARS:
        cagrs, _ = rolling_cagrs(nav, years)
        if len(cagrs):
            reference[years] = cagrs
    return reference, None


def _leverage(asset: object) -> float:
    from fear_ladder.constants import ASSET_LEVERAGE

    key = asset if isinstance(asset, Asset) else Asset(str(asset))
    return ASSET_LEVERAGE[key]


if __name__ == "__main__":
    raise SystemExit(main())

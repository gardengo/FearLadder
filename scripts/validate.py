"""Validate a searched candidate before anyone considers freezing it.

    python scripts/validate.py --candidate config/research/candidate.strategy.yaml

Three checks, in the order that makes them meaningful:

1. **Sensitivity** (TASK-093). Score the neighbourhood of each chosen parameter.
   A peak that collapses one step to either side is a fitting artefact, and
   ``BACKTEST_SPEC.md`` §21 prefers a stable region to a taller spike.
2. **Validation window** (TASK-090). The first look at data the search never
   read. A large drop from research to validation is the honest measure of how
   much the search overfitted.
3. **Walk-forward** (TASK-091). Train → freeze → test, rolled forward, so the
   selection is re-made repeatedly on data that precedes each test.

The out-of-sample window is deliberately *not* touched here. It is spent once,
after the strategy is otherwise settled (``BACKTEST_SPEC.md`` §20), and
``--unseal-oos`` is the explicit, logged act that spends it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.config.schema import AppConfig
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.research.backtest_runner import MarketData, StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.search import (
    Candidate,
    GridSearch,
    Objective,
    transition_candidates,
)
from fear_ladder.research.sensitivity import analyse
from fear_ladder.research.splits import DatasetSplit, Split, SplitGuard

logger = logging.getLogger("validate")

CANDIDATE = paths.CONFIG_DIR / "research" / "candidate.strategy.yaml"
INDICATORS = paths.CONFIG_DIR / "research" / "placeholder.indicators.yaml"

METRIC_COLUMNS = [
    "cagr",
    "max_drawdown",
    "sharpe",
    "calmar",
    "worst_year",
    "turnover_per_year",
    "regime_change_count",
]


def _print(frame) -> None:
    print(frame.to_string())


# ------------------------------------------------------------------ TASK-093


def check_sensitivity(
    config: AppConfig, data: MarketData, guard: SplitGuard, objective: Objective
) -> None:
    search = GridSearch(config=config, data=data, guard=guard, objective=objective)
    print("\n=== sensitivity (TASK-093) ===")

    trend = config.strategy.trend_filter
    if trend.enabled and trend.threshold is not None:
        offsets = [-0.04, -0.02, 0.0, 0.02, 0.04]
        values = [round(trend.threshold + offset, 4) for offset in offsets]
        spec = trend.model_dump()
        candidates = [
            Candidate(
                label=f"trend.threshold={value:+.3f}",
                overrides={"trend_filter": {**spec, "threshold": value}},
            )
            for value in values
        ]
        report = analyse(search, candidates, values, parameter="trend.threshold")
        print(report.summary())
        _print(report.to_frame())

    transition = config.strategy.transition
    if transition.minimum_duration_days is not None:
        base = transition.minimum_duration_days
        values = [max(1, base + offset) for offset in (-10, -5, 0, 5, 10)]
        candidates = [
            Candidate(
                label=f"transition(c={transition.confirmation_days},"
                f"h={transition.hysteresis:g},d={value})",
                overrides={
                    "transition": {
                        "confirmation_days": transition.confirmation_days,
                        "hysteresis": transition.hysteresis,
                        "minimum_duration_days": value,
                        "research_candidates": {
                            key: list(items)
                            for key, items in transition.research_candidates.items()
                        },
                    }
                },
            )
            for value in values
        ]
        report = analyse(
            search, candidates, [float(value) for value in values],
            parameter="transition.minimum_duration_days",
        )
        print(report.summary())
        _print(report.to_frame())


# ------------------------------------------------------------------ TASK-090


def check_validation_window(
    config: AppConfig, data: MarketData, guard: SplitGuard
) -> None:
    print("\n=== research vs validation (TASK-090) ===")
    rows = {}
    for split in (Split.RESEARCH, Split.VALIDATION):
        window = guard.split.window(split)
        run = StrategyBacktest(config).run(
            data, start=window.start, end=window.end, name=f"strategy@{split.value}"
        )
        rows[split.value] = run.metrics.to_dict()
        for name, metrics in run.benchmark_metrics.items():
            rows[f"{name}@{split.value}"] = metrics.to_dict()

    import pandas as pd

    frame = pd.DataFrame(rows).T
    _print(frame[[column for column in METRIC_COLUMNS if column in frame.columns]])

    research_cagr = rows[Split.RESEARCH.value]["cagr"]
    validation_cagr = rows[Split.VALIDATION.value]["cagr"]
    gap = research_cagr - validation_cagr
    print(
        f"\noverfit gap (research CAGR - validation CAGR): {gap:+.2%}"
    )
    if gap > 0.05:
        logger.warning(
            "the search gained %.1f percentage points that did not survive "
            "the validation window",
            gap * 100,
        )


# ------------------------------------------------------------------ TASK-091


def check_walk_forward(
    config: AppConfig, data: MarketData, guard: SplitGuard, objective: Objective
) -> None:
    from fear_ladder.research.walk_forward import WalkForward

    print("\n=== walk-forward (TASK-091) ===")
    candidates = tuple(transition_candidates(config))
    research = guard.split.research
    report = WalkForward(config, data, candidates, objective=objective).run(
        start=research.start,
        end=research.end,
        train_days=365 * 6,
        test_days=365 * 2,
        step_days=365 * 2,
    )
    _print(report.to_frame())
    print(f"\n{report.summary()}")


# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=CANDIDATE)
    parser.add_argument("--indicators", type=Path, default=INDICATORS)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--checks",
        default="sensitivity,validation,walkforward",
        help="comma-separated subset to run",
    )
    parser.add_argument(
        "--unseal-oos",
        metavar="REASON",
        default=None,
        help=(
            "spend the out-of-sample window. Only once, only when the strategy "
            "is otherwise settled: after this it is no longer a clean test."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config(strategy_path=args.candidate, indicators_path=args.indicators)
    guard = SplitGuard(DatasetSplit.from_spec(config.strategy.dataset_split))
    objective = Objective()

    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)
    logger.info("%d trading days loaded", len(data.closes))

    checks = {check.strip() for check in args.checks.split(",") if check.strip()}
    if "sensitivity" in checks:
        check_sensitivity(config, data, guard, objective)
    if "validation" in checks:
        guard.unseal(Split.VALIDATION, reason="candidate validation (TASK-090)")
        check_validation_window(config, data, guard)
    if "walkforward" in checks:
        check_walk_forward(config, data, guard, objective)

    if args.unseal_oos:
        guard.unseal(Split.OOS, reason=args.unseal_oos)
        window = guard.split.oos
        run = StrategyBacktest(config).run(
            data, start=window.start, end=window.end, name="strategy@OOS"
        )
        print("\n=== OUT OF SAMPLE (spent) ===")
        _print(run.comparison()[[c for c in METRIC_COLUMNS if c in run.comparison().columns]])
        logger.warning(
            "the OOS window is now spent. Changing parameters after seeing this "
            "makes it no longer out-of-sample (BACKTEST_SPEC.md 20)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

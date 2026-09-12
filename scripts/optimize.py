"""Run the parameter search, one dimension at a time (TASK-080 .. TASK-085).

    python scripts/optimize.py --stages trend,transition,regime,weights --write

Coordinate descent: each stage searches one group of parameters, the winner is
carried into the next stage, and every stage reads the **research window only**
(``SplitGuard`` raises otherwise).

Why sequential rather than a joint grid: the full product of the four stages is
tens of thousands of backtests. Sequential is tractable and, more usefully,
*legible* — each stage's table shows what that parameter actually did. The cost
is that it can miss interactions between stages, which is why the result is not
a decision: it is a candidate that then has to survive sensitivity analysis, the
validation window and walk-forward before anything is frozen.

Nothing here writes to ``config/strategy.yaml``. ``--write`` updates the
*candidate* file; freezing stays a separate, deliberate act (TASK-101).
"""

from __future__ import annotations

import argparse
import itertools
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml
from pandas import DataFrame

from regime_monitor import paths
from regime_monitor.config.loader import load_config
from regime_monitor.config.schema import AppConfig
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork
from regime_monitor.monitoring.logging import configure_logging
from regime_monitor.research.backtest_runner import MarketData
from regime_monitor.research.data_loader import load_market_data
from regime_monitor.research.ladder import LadderSpec, build_ladder, default_labels
from regime_monitor.research.search import (
    Candidate,
    GridSearch,
    Objective,
    SearchReport,
)
from regime_monitor.research.splits import DatasetSplit, Split, SplitGuard

logger = logging.getLogger("optimize")

CANDIDATE = paths.CONFIG_DIR / "research" / "candidate.strategy.yaml"
INDICATORS = paths.CONFIG_DIR / "research" / "placeholder.indicators.yaml"

REPORT_COLUMNS = [
    "objective",
    "cagr",
    "max_drawdown",
    "sharpe",
    "calmar",
    "worst_year",
    "turnover_per_year",
    "regime_change_count",
]


@dataclass(frozen=True, slots=True)
class StageResult:
    name: str
    report: SearchReport
    overrides: dict[str, Any]

    def table(self, limit: int = 8) -> DataFrame:
        frame = self.report.to_frame()
        columns = [column for column in REPORT_COLUMNS if column in frame.columns]
        return frame[columns].head(limit)


# ------------------------------------------------------------------ stages


def trend_candidates(config: AppConfig) -> list[Candidate]:
    """TASK-083 — where the trend line sits and how hard it caps."""
    spec = config.strategy.trend_filter.model_dump()
    grid = config.strategy.trend_filter.research_candidates
    thresholds = grid.get("threshold", (0.0,))
    caps = grid.get("max_leverage_below", (1.0,))
    # The band is swept as a width above the exit, not as an absolute level, so
    # every combination stays valid however the threshold moves.
    bands = grid.get("reentry_band", (0.0,))

    candidates = []
    for threshold, cap, band in itertools.product(thresholds, caps, bands):
        variant = dict(spec)
        variant.update(
            threshold=float(threshold),
            max_leverage_below=float(cap),
            reentry_threshold=float(threshold) + float(band),
        )
        candidates.append(
            Candidate(
                label=f"trend(t={threshold:+.2f},cap={cap:.1f},band={band:.2f})",
                overrides={"trend_filter": variant},
                dimension="trend_filter",
            )
        )
    # Worth seeing what the filter is actually buying.
    candidates.append(
        Candidate(
            label="trend(off)",
            overrides={"trend_filter": {**spec, "enabled": False}},
            dimension="trend_filter",
        )
    )
    return candidates


def transition_candidates_for(config: AppConfig) -> list[Candidate]:
    """TASK-085 — confirmation, hysteresis, minimum duration."""
    from regime_monitor.research.search import transition_candidates

    return list(transition_candidates(config))


def regime_candidates(config: AppConfig) -> list[Candidate]:
    """TASK-082 + TASK-083 — stage count and where the cuts fall.

    Allocations are regenerated from the operator's ladder endpoints for each
    stage count, so changing the count never silently invents a portfolio.
    """
    ladder_spec = LadderSpec(fear_leverage=3.0, greed_leverage=0.5)
    constraints = config.strategy.allocation.constraints.model_dump()
    base_regime = config.strategy.regime.model_dump()

    candidates = []
    for count in (3, 5, 7, 9):
        labels = default_labels(count)
        mappings = {
            label: {asset.value: weight for asset, weight in weights.items()}
            for label, weights in build_ladder(labels, ladder_spec).items()
        }
        for shape, boundaries in _boundary_shapes(count).items():
            regime = dict(base_regime)
            regime.update(count=count, labels=list(labels), boundaries=list(boundaries))
            candidates.append(
                Candidate(
                    label=f"regimes={count}/{shape}",
                    overrides={
                        "regime": regime,
                        "allocation": {"mappings": mappings, "constraints": constraints},
                    },
                    dimension="regime",
                )
            )
    return candidates


def _boundary_shapes(count: int) -> dict[str, tuple[float, ...]]:
    """Three ways to cut the 0-100 score axis into ``count`` bands.

    ``even`` spaces them uniformly. ``wide-middle`` widens the neutral band so
    the strategy sits still more often. ``wide-tails`` widens the extremes so
    the aggressive and defensive ends are harder to reach.
    """
    step = 100.0 / count
    even = tuple(round(step * (index + 1), 4) for index in range(count - 1))

    def stretch(factor: float) -> tuple[float, ...]:
        middle = 50.0
        return tuple(
            round(middle + (cut - middle) * factor, 4) for cut in even
        )

    return {
        "even": even,
        "wide-middle": stretch(1.30),
        "wide-tails": stretch(0.70),
    }


def weight_candidates_for(config: AppConfig) -> list[Candidate]:
    """TASK-080 + TASK-081 — which indicator families carry the score.

    Whole families are weighted rather than individual indicators: 20 free
    weights over one window is an invitation to fit noise, and the families are
    what the documents actually reason about (``PRD.md`` §6).
    """
    families: dict[str, list[str]] = {}
    for name, spec in config.indicators.enabled_indicators.items():
        families.setdefault(spec.family, []).append(name)

    profiles: dict[str, dict[str, float]] = {
        "equal": dict.fromkeys(families, 1.0),
        "trend-heavy": {"trend": 3.0, "momentum": 2.0, "drawdown": 1.0,
                        "volatility": 1.0, "rsi": 1.0, "sentiment": 1.0},
        "fear-heavy": {"volatility": 3.0, "drawdown": 3.0, "sentiment": 2.0,
                       "rsi": 1.0, "trend": 1.0, "momentum": 1.0},
        "drawdown-led": {"drawdown": 4.0, "volatility": 2.0, "trend": 1.0,
                         "momentum": 1.0, "rsi": 1.0, "sentiment": 1.0},
        "price-only": {"trend": 2.0, "momentum": 2.0, "drawdown": 2.0,
                       "rsi": 1.0, "volatility": 0.0, "sentiment": 0.0},
        "no-sentiment": {"trend": 2.0, "momentum": 1.0, "drawdown": 2.0,
                         "rsi": 1.0, "volatility": 2.0, "sentiment": 0.0},
    }

    candidates = []
    for label, family_weights in profiles.items():
        weights: dict[str, float] = {}
        for family, names in families.items():
            share = family_weights.get(family, 0.0)
            if share <= 0:
                continue
            for name in names:
                weights[name] = share / len(names)
        total = sum(weights.values())
        if total <= 0:
            continue
        candidates.append(
            Candidate(
                label=f"weights:{label}",
                overrides={"score": {"weights": {k: v / total for k, v in weights.items()}}},
                dimension="weights",
            )
        )
    return candidates


STAGES = {
    "trend": ("trend filter (TASK-083)", trend_candidates),
    "transition": ("transition (TASK-085)", transition_candidates_for),
    "regime": ("regime count & boundaries (TASK-082/083)", regime_candidates),
    "weights": ("indicator weights (TASK-080/081)", weight_candidates_for),
}


# --------------------------------------------------------------------- run


def run_stage(
    name: str,
    config: AppConfig,
    data: MarketData,
    guard: SplitGuard,
    objective: Objective,
) -> StageResult:
    title, builder = STAGES[name]
    candidates = builder(config)
    logger.info("stage %s: %d candidates", title, len(candidates))

    search = GridSearch(config=config, data=data, guard=guard, objective=objective)
    report = search.run(candidates, split=Split.RESEARCH, dimension=name)
    best = report.best
    if best is None:
        raise SystemExit(f"stage {name} produced no usable candidate")
    return StageResult(name=name, report=report, overrides=best.candidate.overrides)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=CANDIDATE)
    parser.add_argument("--indicators", type=Path, default=INDICATORS)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--stages",
        default="trend,transition,regime,weights",
        help="comma-separated, run in the order given",
    )
    parser.add_argument("--top", type=int, default=8, help="rows to print per stage")
    parser.add_argument(
        "--write", action="store_true", help="update the candidate yaml with the winners"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config(strategy_path=args.candidate, indicators_path=args.indicators)
    guard = SplitGuard(DatasetSplit.from_spec(config.strategy.dataset_split))
    objective = Objective()
    logger.info("objective: %s", objective.describe())
    logger.info("research window: %s", guard.split.research)

    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)
    logger.info("%d trading days loaded", len(data.closes))

    applied: dict[str, Any] = {}
    for name in [stage.strip() for stage in args.stages.split(",") if stage.strip()]:
        if name not in STAGES:
            raise SystemExit(f"unknown stage {name!r}; known: {sorted(STAGES)}")
        result = run_stage(name, config, data, guard, objective)

        print(f"\n=== {STAGES[name][0]} ===")
        print(result.table(args.top).to_string())
        print(f"--> {result.report.best.candidate.label}")

        applied.update(result.overrides)
        config = Candidate(label=f"after-{name}", overrides=result.overrides).apply(config)
        # Keep the version stable across stages; Candidate.apply appends to it.
        config = _rename(config, args.candidate)

    if args.write:
        _write_candidate(args.candidate, applied)
        logger.info("updated %s", args.candidate)
    else:
        logger.warning("dry run — re-run with --write to update the candidate file")
    return 0


def _rename(config: AppConfig, candidate_path: Path) -> AppConfig:
    from regime_monitor.config.schema import StrategyConfig

    original = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))["strategy_version"]
    payload = config.strategy.model_dump()
    payload["strategy_version"] = original
    return AppConfig(
        indicators=config.indicators,
        strategy=StrategyConfig(**payload),
        alerts=config.alerts,
        data_sources=config.data_sources,
    )


def _write_candidate(path: Path, overrides: dict[str, Any]) -> None:
    """Merge the winners into the candidate file, keeping its comments' intent.

    The file is rewritten from parsed YAML, so the prose comments are lost; the
    header is re-added with a pointer to this script.
    """
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload.update(overrides)
    header = (
        "# candidate.strategy.yaml — regenerated by scripts/optimize.py.\n"
        "#\n"
        "# Searched on the research window only. NOT a decision: this still has\n"
        "# to survive sensitivity analysis, the validation window and\n"
        "# walk-forward before scripts/freeze.py will accept it.\n\n"
    )
    path.write_text(
        header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())

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
from dataclasses import dataclass, field
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
CANDIDATE_INDICATORS = paths.CONFIG_DIR / "research" / "candidate.indicators.yaml"

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
    indicator_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    selected: str = ""
    objective_winner: str = ""

    @property
    def overridden(self) -> bool:
        return bool(self.selected) and self.selected != self.objective_winner

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


ROLLING = ("rolling_percentile", "rolling_zscore")


def _rolling_families(config: AppConfig) -> dict[str, list[str]]:
    """Indicator names grouped by family, rolling-normalised ones only."""
    families: dict[str, list[str]] = {}
    for name, spec in config.indicators.enabled_indicators.items():
        if spec.normalization.method in ROLLING:
            families.setdefault(spec.family, []).append(name)
    return families


def _window_override(window: int) -> dict[str, Any]:
    """A normalization payload with the window moved and min_periods kept sane.

    ``min_periods`` cannot exceed the window, and a warm-up that is most of the
    window leaves almost no history to rank against. Half the window is the
    same ratio the placeholder profile used at 504/252.
    """
    return {"normalization": {"window": window, "min_periods": window // 2}}


def indicator_candidates(config: AppConfig) -> list[Candidate]:
    """How much history each indicator is judged against.

    Never searched before this: 17 of 21 indicators sat at a 504-day window
    inherited from the placeholder profile. It is not a cosmetic setting. A
    rolling percentile asks "extreme compared to the recent past", so a bubble
    lasting longer than the window normalises itself away - which is what
    happened at the dot-com top, where the strategy read Neutral and carried
    1.78x into a 63% decline.

    Swept by family, not per indicator: 4 windows across 17 indicators is 17
    billion combinations, and the family grouping already organises the weights.

    Each family keeps its own candidate grid rather than sharing one. AAII is a
    weekly series whose candidates are in weeks; forcing 504 on it would mean a
    ten-year lookback. The across-the-board sweep therefore moves every family
    to the same *position* in its own grid - all shortest, all longest - rather
    than to the same number.
    """
    families = _rolling_families(config)
    if not families:
        return []

    grids: dict[str, list[int]] = {}
    for family, names in families.items():
        options: set[int] = set()
        for name in names:
            options.update(config.indicators.indicators[name].normalization.research_candidates)
        if options:
            grids[family] = sorted(options)
    if not grids:
        return []

    def candidate(label: str, moves: dict[str, int]) -> Candidate:
        return Candidate(
            label=label,
            overrides={},
            dimension="indicators",
            indicator_overrides={
                name: _window_override(window)
                for family, window in moves.items()
                for name in families[family]
            },
        )

    candidates: list[Candidate] = []
    depth = min(len(grid) for grid in grids.values())
    for rank in range(depth):
        moves = {family: grid[rank] for family, grid in grids.items()}
        shown = "/".join(str(moves[f]) for f in sorted(moves))
        candidates.append(candidate(f"norm(all:rank{rank}={shown})", moves))

    for family, grid in sorted(grids.items()):
        for window in grid:
            candidates.append(candidate(f"norm({family}={window})", {family: window}))

    candidates.extend(_absolute_candidates(config, families))
    return candidates


def _absolute_candidates(
    config: AppConfig, families: dict[str, list[str]]
) -> list[Candidate]:
    """Put a family on its definitional scale instead of a rolling rank.

    A rolling percentile asks "extreme compared to the recent past", which is
    why no single window works: a bubble that outlasts the window normalises
    itself away, and so does a bear market. Measured, a 252-day window catches
    the dot-com top and loses the 2008 bottom; 504 does the reverse.

    An absolute scale has no such memory. It is only offered where the config
    declares a ``definitional_range`` - RSI is 0-100 because RSI is 0-100, not
    because a sample said so. Without that declaration this would be inventing
    a threshold, which ``CLAUDE_CODE_INITIAL_PROMPT.md`` 10 forbids.
    """
    candidates: list[Candidate] = []
    for family, names in sorted(families.items()):
        ranges = [config.indicators.indicators[name].definitional_range for name in names]
        if any(bounds is None for bounds in ranges):
            continue
        candidates.append(
            Candidate(
                label=f"norm({family}=absolute)",
                overrides={},
                dimension="indicators",
                indicator_overrides={
                    name: {
                        "normalization": {
                            "method": "bounded",
                            "window": None,
                            "min_periods": None,
                            "raw_at_score_min": bounds[0],
                            "raw_at_score_max": bounds[1],
                        }
                    }
                    for name, bounds in zip(names, ranges, strict=True)
                    if bounds is not None
                },
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
    "indicators": ("indicator normalization windows", indicator_candidates),
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
    select: str | None = None,
) -> StageResult:
    title, builder = STAGES[name]
    candidates = builder(config)
    logger.info("stage %s: %d candidates", title, len(candidates))

    search = GridSearch(config=config, data=data, guard=guard, objective=objective)
    report = search.run(candidates, split=Split.RESEARCH, dimension=name)
    best = report.best
    if best is None:
        raise SystemExit(f"stage {name} produced no usable candidate")

    chosen = best
    if select is not None:
        matches = [o for o in report.outcomes if o.candidate.label == select]
        if not matches:
            raise SystemExit(
                f"stage {name}: no candidate labelled {select!r}. "
                f"Available: {sorted(o.candidate.label for o in report.outcomes)}"
            )
        chosen = matches[0]
        # Loud on purpose. The objective cannot see everything that matters -
        # it scores aggregates, not "did this recognise the 2008 bottom" - so
        # overriding it is legitimate, but it must never happen quietly.
        logger.warning(
            "stage %s: taking %s (objective %.4f) over the objective winner %s (%.4f) "
            "— a deliberate override, record the reason",
            name, chosen.candidate.label, chosen.objective,
            best.candidate.label, best.objective,
        )
    return StageResult(
        name=name,
        report=report,
        overrides=chosen.candidate.overrides,
        indicator_overrides=chosen.candidate.indicator_overrides,
        selected=chosen.candidate.label,
        objective_winner=best.candidate.label,
    )


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
        "--select",
        action="append",
        default=[],
        metavar="STAGE=LABEL",
        help=(
            "take a named candidate instead of the objective winner, e.g. "
            "indicators=norm(rsi=absolute). The objective scores aggregates and "
            "cannot see everything that matters, so an override is legitimate — "
            "but it is logged loudly and the reason belongs in docs/strategy.md."
        ),
    )
    parser.add_argument(
        "--write", action="store_true", help="update the candidate yaml with the winners"
    )
    parser.add_argument(
        "--write-indicators",
        type=Path,
        default=CANDIDATE_INDICATORS,
        help=(
            "where searched indicator parameters are written. Never the "
            "placeholder profile: that one is a fixed reference the tests read."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    # Argument errors fail before anything opens a database or reads a window.
    selections: dict[str, str] = {}
    for item in args.select:
        stage, _, label = item.partition("=")
        if not label:
            raise SystemExit(f"--select expects STAGE=LABEL, got {item!r}")
        selections[stage.strip()] = label.strip()

    config = load_config(strategy_path=args.candidate, indicators_path=args.indicators)
    guard = SplitGuard(DatasetSplit.from_spec(config.strategy.dataset_split))
    objective = Objective()
    logger.info("objective: %s", objective.describe())
    logger.info("research window: %s", guard.split.research)

    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)
    logger.info("%d trading days loaded", len(data.closes))

    applied: dict[str, Any] = {}
    applied_indicators: dict[str, dict[str, Any]] = {}
    for name in [stage.strip() for stage in args.stages.split(",") if stage.strip()]:
        if name not in STAGES:
            raise SystemExit(f"unknown stage {name!r}; known: {sorted(STAGES)}")
        result = run_stage(name, config, data, guard, objective, select=selections.get(name))

        print(f"\n=== {STAGES[name][0]} ===")
        print(result.table(args.top).to_string())
        if result.overridden:
            print(f"--> {result.selected}  (objective winner was {result.objective_winner})")
        else:
            print(f"--> {result.selected}")

        applied.update(result.overrides)
        for indicator, override in result.indicator_overrides.items():
            applied_indicators.setdefault(indicator, {}).update(override)
        config = Candidate(
            label=f"after-{name}",
            overrides=result.overrides,
            indicator_overrides=result.indicator_overrides,
        ).apply(config)
        # Keep the version stable across stages; Candidate.apply appends to it.
        config = _rename(config, args.candidate)

    if args.write:
        _write_candidate(args.candidate, applied)
        logger.info("updated %s", args.candidate)
        if applied_indicators:
            _write_indicators(args.indicators, args.write_indicators, config)
            logger.info("wrote %s", args.write_indicators)
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


def _write_indicators(source: Path, destination: Path, config: AppConfig) -> None:
    """Write the searched indicator set out, never over the placeholder profile.

    ``placeholder.indicators.yaml`` is a fixed reference the tests read; a
    search must not move it. The searched set lands beside it under its own
    name and the candidate strategy is run against that.
    """
    if destination.resolve() == source.resolve():
        raise SystemExit(
            f"refusing to overwrite {source}: the placeholder profile is a fixed "
            "reference. Pass --write-indicators with a different path."
        )
    payload = config.indicators.model_dump(mode="json", exclude_none=True)
    header = """\
# candidate.indicators.yaml — regenerated by scripts/optimize.py.
#
# Searched on the research window only. The normalization windows here were
# placeholder values until this stage existed; see docs/strategy.md §2.6.
#
# Pair it with candidate.strategy.yaml — both, or neither:
#   python scripts/backtest.py
#     --profile    config/research/candidate.strategy.yaml
#     --indicators config/research/candidate.indicators.yaml

"""
    destination.write_text(
        header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _write_candidate(path: Path, overrides: dict[str, Any]) -> None:
    """Merge the winners into the candidate file.

    The file is rewritten from parsed YAML, so **every prose comment in it is
    lost** and only a generic header is re-added. In a project whose whole
    discipline is recording *why* a number was chosen, that is expensive: the
    per-parameter rationale has to be written back by hand afterwards.

    So a stage that changed nothing here does not rewrite the file at all.
    Running the indicator stage used to blow away the strategy file's reasoning
    for no reason whatsoever.
    """
    if not overrides:
        logger.info("no strategy overrides to write; leaving %s untouched", path)
        return
    logger.warning(
        "rewriting %s from parsed YAML — its prose comments will be lost, "
        "re-record the rationale for anything you changed",
        path,
    )
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

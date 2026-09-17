"""Which of the twenty indicators is actually carrying the score? (TASK-183)

    python scripts/indicator_ablation.py --db path/to/full.db

``score.weights`` is the largest untested surface in the frozen strategy by
parameter count — twenty numbers, support graded '보통' in ``docs/strategy.md``
§2.5, and never once shaken. This removes indicators and re-measures.

Two passes, and the second is the one that matters.

**Leave one out.** Drop a single indicator, renormalise the remaining weights,
re-run. Answers "is this indicator load-bearing on its own".

**Leave a family out.** Drop all of ``rsi``, or all of ``momentum``, and so on.
Leave-one-out *systematically understates* importance whenever indicators are
correlated, which these are by construction: removing ``rsi_14`` while
``rsi_30`` remains changes almost nothing, and reading that as "RSI does not
matter" would be exactly backwards. The family pass is what can actually answer
"would a much smaller set do the same job".

Renormalisation is not optional. ``BACKTEST_SPEC.md`` §10 requires the weights
to be a convex combination, and leaving a hole at zero would shrink every
composite score toward the neutral midpoint — which silently moves every band
edge in ``regime.boundaries`` without changing a single configured number.

Performance is only half the reading, so each variant also reports how far the
*score* moved: an indicator can barely touch CAGR while rewriting the signal,
and one that moves neither is genuinely inert.

The OOS column is a measurement, not a selection (``docs/strategy.md`` §2.8 —
that window is spent). Nothing here writes to ``config/``; acting on any of it
means a new freeze.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pandas import Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.config.schema import AppConfig
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.research.backtest_runner import BacktestRun, MarketData, StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.performance import cash_curve, window_stats

logger = logging.getLogger("indicator_ablation")

OUTPUT = paths.REPORTS_DIR / "indicator_ablation.json"

#: Weights must still sum to one afterwards (``BACKTEST_SPEC.md`` §10).
WEIGHT_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class Variant:
    """One ablation: what was removed, and under which heading to report it."""

    name: str
    kind: str
    dropped: tuple[str, ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=None,
        help="full-history database (scripts/collect_full_history.py)",
    )
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--families-only", action="store_true",
        help="skip the leave-one-out pass and only drop whole families",
    )
    return parser


def windows(config: AppConfig) -> tuple[tuple[str, date | None, date | None], ...]:
    """The strategy's own splits, plus the whole record.

    Reusing ``dataset_split`` rather than inventing eras keeps this table
    readable next to §2.5 and §2.8, which are stated over exactly these.
    """
    split = config.strategy.dataset_split
    return (
        ("full 1996-2026", None, None),
        ("research 1999-2015", split.research_start, split.research_end),
        ("validation 2015-2021", split.validation_start, split.validation_end),
        ("oos 2021-2026", split.oos_start, split.oos_end),
    )


def plan(config: AppConfig, *, families_only: bool) -> list[Variant]:
    weights = config.strategy.score.weights or {}
    specs = config.indicators.enabled_indicators

    families: dict[str, list[str]] = {}
    for name in weights:
        families.setdefault(specs[name].family, []).append(name)

    variants = [
        Variant(f"family:{family}", "family", tuple(sorted(members)))
        for family, members in sorted(families.items())
    ]
    if not families_only:
        variants = [
            *(Variant(f"drop:{name}", "single", (name,)) for name in sorted(weights)),
            *variants,
        ]
    return variants


def renormalised(weights: dict[str, float], dropped: tuple[str, ...]) -> dict[str, float]:
    """The remaining weights, rescaled to sum to one again."""
    kept = {name: weight for name, weight in weights.items() if name not in dropped}
    if not kept:
        raise ValueError(f"dropping {dropped} leaves no indicator to score with")
    total = sum(kept.values())
    if total <= 0:
        raise ValueError(f"dropping {dropped} leaves zero total weight")
    rescaled = {name: weight / total for name, weight in kept.items()}
    assert abs(sum(rescaled.values()) - 1.0) < WEIGHT_TOLERANCE
    return rescaled


def variant_config(config: AppConfig, dropped: tuple[str, ...]) -> AppConfig:
    """The frozen config minus some indicators. Copied, never mutated."""
    weights = renormalised(dict(config.strategy.score.weights or {}), dropped)
    score = config.strategy.score.model_copy(update={"weights": weights})
    return config.model_copy(
        update={"strategy": config.strategy.model_copy(update={"score": score})}
    )


def measure(run: BacktestRun, data: MarketData) -> dict[str, float]:
    nav = run.result.nav
    stats = window_stats(nav / nav.iloc[0], cash_curve(data.cash_rates, list(nav.index)))
    return {
        "cagr": stats["cagr"],
        "max_drawdown": float((nav / nav.cummax() - 1.0).min()),
        "sharpe": stats["sharpe"],
        "regime_changes": float(run.regime_change_count),
    }


def signal_shift(baseline: BacktestRun, run: BacktestRun) -> dict[str, float]:
    """How far the *signal* moved, independently of what it earned.

    Performance alone cannot separate "this indicator is inert" from "this
    indicator matters and the strategy happened to be insensitive to it over
    this particular path". The second is a much weaker reason to drop something.
    """
    scores = _aligned(baseline.composite_score, run.composite_score)
    regimes = _aligned(baseline.regimes, run.regimes)
    return {
        "score_correlation": float(scores[0].corr(scores[1])),
        "score_mean_abs_shift": float((scores[1] - scores[0]).abs().mean()),
        "regime_disagreement": float((regimes[0] != regimes[1]).mean()),
    }


def _aligned(left: Series, right: Series) -> tuple[Series, Series]:
    shared = left.dropna().index.intersection(right.dropna().index)
    return left.loc[shared], right.loc[shared]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)
    logger.info(
        "%s: %d indicators, %d trading days",
        config.strategy.strategy_version,
        len(config.strategy.score.weights or {}),
        len(data.closes),
    )

    spans = windows(config)
    baseline_run = StrategyBacktest(config).run(data, include_benchmarks=False)
    baseline = {
        label: measure(
            StrategyBacktest(config).run(data, start=start, end=end, include_benchmarks=False)
            if label != spans[0][0]
            else baseline_run,
            data,
        )
        for label, start, end in spans
    }
    for label, stats in baseline.items():
        logger.info(
            "%-22s %-22s CAGR %6.2f%%  MDD %6.1f%%  Sharpe %5.2f  changes %3d",
            "baseline", label, stats["cagr"] * 100, stats["max_drawdown"] * 100,
            stats["sharpe"], int(stats["regime_changes"]),
        )

    rows: list[dict[str, object]] = []
    for item in plan(config, families_only=args.families_only):
        variant = variant_config(config, item.dropped)
        full_run = StrategyBacktest(variant).run(data, include_benchmarks=False)
        measured = {spans[0][0]: measure(full_run, data)}
        for label, start, end in spans[1:]:
            run = StrategyBacktest(variant).run(
                data, start=start, end=end, include_benchmarks=False
            )
            measured[label] = measure(run, data)

        shift = signal_shift(baseline_run, full_run)
        rows.append(
            {
                "variant": item.name,
                "kind": item.kind,
                "dropped": list(item.dropped),
                "remaining": len(config.strategy.score.weights or {}) - len(item.dropped),
                "windows": {
                    label: {
                        **stats,
                        "cagr_change_pp": (stats["cagr"] - baseline[label]["cagr"]) * 100,
                        "drawdown_change_pp": (
                            stats["max_drawdown"] - baseline[label]["max_drawdown"]
                        ) * 100,
                    }
                    for label, stats in measured.items()
                },
                **shift,
            }
        )
        full = rows[-1]["windows"][spans[0][0]]  # type: ignore[index]
        logger.info(
            "%-22s %-22s CAGR %6.2f%% (%+5.2f)  MDD %6.1f%% (%+5.1f)  "
            "score r %.4f  regimes differ %5.1f%%",
            item.name, spans[0][0], full["cagr"] * 100, full["cagr_change_pp"],
            full["max_drawdown"] * 100, full["drawdown_change_pp"],
            shift["score_correlation"], shift["regime_disagreement"] * 100,
        )

    report = {
        "task": "TASK-183",
        "strategy_version": config.strategy.strategy_version,
        "weights": dict(config.strategy.score.weights or {}),
        "windows": [
            {"window": label, "start": str(start or ""), "end": str(end or "")}
            for label, start, end in spans
        ],
        "baseline": baseline,
        "ablations": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

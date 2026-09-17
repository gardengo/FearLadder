"""Ninety-three observations instead of one. (TASK-184)

    python scripts/event_analysis.py --db path/to/full.db

Every comparison in ``docs/strategy.md`` so far compresses thirty years into a
single CAGR. Splitting by window gives three numbers. That is the whole sample,
and it is why §2.10 could read a coincidence as a reproduction: with n=1 there is
nothing to be surprised by.

The frozen strategy changes rung 93 times. Each change opens a span that ends at
the next one, and each span has a return. That turns one path into a
**distribution**, which is the first thing here capable of carrying a variance.

Two parts.

**The ledger.** Every transition, what it earned, and against what. ``edge`` is
the strategy's span return minus the index's over the same days — what the rung
choice bought relative to simply holding QQQ.

**The paired comparison.** Two parameter values produce two different sets of
transitions, so their events cannot be paired directly. What can be paired is
*calendar*: take the union of both variants' change dates as breakpoints, and
both variants then have a return over each identical span. The per-span
differences sum exactly to the total log-return gap, so the headline difference
gets decomposed into contributions that can be counted, signed and dispersed.

Attribution rule, stated because it is the whole design (and because the
alternative choices are defensible too):

* A span runs **from one change's signal date to the next change's signal
  date**. Execution is NEXT_OPEN, so the trade the event causes lands inside its
  own span, which is where its cost belongs.
* **The trend filter is left in.** It is part of the strategy, not an
  interference to be removed, and taking it out would price a portfolio the
  strategy never held. But it *does* confound: on a span the filter has capped,
  the return says more about the 200-day moving average than about the rung. So
  every event records ``trend_capped_share``, and the summary splits on it.

Read the t-statistics as descriptive. Spans are consecutive slices of one
market, not independent draws, so the true uncertainty is wider than the
arithmetic says. The sign test is reported alongside for the same reason.

Nothing here writes to ``config/``. It measures the frozen strategy; acting on
it would need a new freeze (§4).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from math import comb, sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
from pandas import Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.config.schema import AppConfig
from fear_ladder.constants import ASSET_LEVERAGE, Asset
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.research.backtest_runner import BacktestRun, MarketData, StrategyBacktest
from fear_ladder.research.data_loader import load_market_data

logger = logging.getLogger("event_analysis")

OUTPUT = paths.REPORTS_DIR / "event_analysis.json"
UNDERLYING = "QQQ"

#: Above this share of capped days, a span is reported as the trend filter's
#: span rather than the rung's.
FILTER_DOMINATED = 0.5


@dataclass(frozen=True, slots=True)
class Family:
    """A parameter and the grid §2.10/§2.11 argued over."""

    name: str
    values: tuple[float, ...]


FAMILIES: tuple[Family, ...] = (
    Family("minimum_duration_days", (20.0, 30.0, 45.0, 60.0, 75.0, 90.0, 105.0, 120.0, 150.0)),
    Family("hysteresis", (0.0, 2.0, 5.0, 8.0, 12.0)),
    Family("max_leverage_below", (0.0, 0.25, 0.5, 1.0, 1.5)),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=None,
        help="full-history database (scripts/collect_full_history.py)",
    )
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--ledger-only", action="store_true",
        help="write the event ledger and skip the paired parameter comparison",
    )
    return parser


def variant(config: AppConfig, family: str, value: float) -> AppConfig:
    """The frozen config with one parameter replaced. Copied, never mutated."""
    strategy = config.strategy
    if family == "max_leverage_below":
        block = strategy.trend_filter.model_copy(update={family: value})
        strategy = strategy.model_copy(update={"trend_filter": block})
    elif family in {"minimum_duration_days", "hysteresis"}:
        cast = int(value) if family == "minimum_duration_days" else value
        block = strategy.transition.model_copy(update={family: cast})
        strategy = strategy.model_copy(update={"transition": block})
    else:
        raise ValueError(f"unknown parameter family {family!r}")
    return config.model_copy(update={"strategy": strategy})


def frozen_value(config: AppConfig, family: str) -> float:
    block = (
        config.strategy.trend_filter
        if family == "max_leverage_below"
        else config.strategy.transition
    )
    return float(getattr(block, family))


def change_dates(run: BacktestRun) -> list[date]:
    return [decision.observation_date for decision in run.decisions if decision.changed]


def nominal_leverage(config: AppConfig, regime: str) -> float | None:
    """The rung's own target leverage, before the trend filter touches it."""
    mappings = config.strategy.allocation.mappings or {}
    weights = mappings.get(regime)
    if weights is None:
        return None
    return sum(
        ASSET_LEVERAGE[Asset(asset)] * weight for asset, weight in weights.items()
    )


def ledger(config: AppConfig, run: BacktestRun, data: MarketData) -> list[dict[str, object]]:
    """One row per rung change: what it earned, against what, and under what cap."""
    nav = run.result.nav
    index = data.closes[UNDERLYING].reindex(nav.index).ffill()
    leverage = run.result.target_leverage

    changes = [decision for decision in run.decisions if decision.changed]
    rows: list[dict[str, object]] = []
    for position, decision in enumerate(changes):
        start = decision.observation_date
        end = (
            changes[position + 1].observation_date
            if position + 1 < len(changes)
            else nav.index[-1]
        )
        span = nav.loc[start:end]
        if len(span) < 2:
            continue

        # The span's *days* are (start, end]: the return is nav[end]/nav[start],
        # so the days that earned it start the day after. Including ``start``
        # would count the boundary day in this span and in the previous one.
        capped = [
            bool(item.trend.capped)
            for day, item in run.allocations.items()
            if start < day <= end and item.trend is not None
        ]
        strategy_return = float(span.iloc[-1] / span.iloc[0] - 1.0)
        index_return = float(index.loc[end] / index.loc[start] - 1.0)
        rows.append(
            {
                "event": position + 1,
                "from_regime": decision.previous_regime,
                "to_regime": decision.regime,
                "direction": _direction(config, decision.previous_regime, decision.regime),
                "start": str(start),
                "end": str(end),
                "days": len(span) - 1,
                "strategy_return": strategy_return,
                "index_return": index_return,
                "edge": strategy_return - index_return,
                "nominal_leverage": nominal_leverage(config, decision.regime),
                "mean_target_leverage": float(leverage.loc[start:end].iloc[1:].mean()),
                "trend_capped_share": float(np.mean(capped)) if capped else 0.0,
            }
        )
    return rows


def _direction(config: AppConfig, previous: str | None, current: str) -> str:
    """Up or down the ladder — fear is *down* in score and *up* in leverage."""
    labels = config.strategy.regime.labels or []
    if previous not in labels or current not in labels:
        return "unknown"
    return "into_fear" if labels.index(current) < labels.index(previous) else "into_greed"


def describe(values: list[float]) -> dict[str, float]:
    """Mean, dispersion and a descriptive t. Not an inference — see the docstring."""
    array = np.asarray(values, dtype="float64")
    count = len(array)
    if count == 0:
        return {"n": 0.0}
    deviation = float(array.std(ddof=1)) if count > 1 else 0.0
    # Identical values do not give exactly zero under floating point, and the
    # residue is enough to produce a t of 1e16. Well below any real dispersion
    # in log returns, well above the noise.
    if deviation < 1e-12:
        deviation = 0.0
    mean = float(array.mean())
    return {
        "n": float(count),
        "mean": mean,
        "median": float(np.median(array)),
        "sd": deviation,
        "t": mean / (deviation / sqrt(count)) if deviation > 0 else 0.0,
        "share_positive": float((array > 0).mean()),
        "worst": float(array.min()),
        "best": float(array.max()),
    }


def sign_test(wins: int, total: int) -> float:
    """Two-sided binomial p under a 50/50 null. Exact, no dependency needed."""
    if total == 0:
        return 1.0
    smaller = min(wins, total - wins)
    tail = sum(comb(total, k) for k in range(smaller + 1)) / 2**total
    return min(1.0, 2 * tail)


def paired(
    frozen: tuple[Series, list[date]], candidate: tuple[Series, list[date]]
) -> dict[str, float]:
    """Both variants' returns over the same calendar spans.

    The breakpoints are the union of the two change sets, so neither variant's
    transitions get to define the windows on their own. Log returns make the
    per-span differences add up to the total gap exactly.
    """
    frozen_nav, frozen_changes = frozen
    candidate_nav, candidate_changes = candidate
    shared = frozen_nav.index.intersection(candidate_nav.index)
    breaks = sorted(
        {shared[0], shared[-1], *frozen_changes, *candidate_changes} & set(shared)
    )
    if len(breaks) < 2:
        return {"n": 0.0}

    differences: list[float] = []
    for start, end in pairwise(breaks):
        left = float(np.log(frozen_nav.loc[end] / frozen_nav.loc[start]))
        right = float(np.log(candidate_nav.loc[end] / candidate_nav.loc[start]))
        differences.append(right - left)

    stats = describe(differences)
    # Ties carry no sign. Counting them as losses would report a confident
    # verdict for two paths that are in fact identical.
    wins = int(sum(1 for value in differences if value > 0))
    losses = int(sum(1 for value in differences if value < 0))
    return {
        **stats,
        "total_log_gap": float(sum(differences)),
        "wins": float(wins),
        "losses": float(losses),
        "ties": float(len(differences) - wins - losses),
        "sign_test_p": sign_test(wins, wins + losses),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)

    frozen_run = StrategyBacktest(config).run(data, include_benchmarks=False)
    events = ledger(config, frozen_run, data)
    logger.info("%d rung changes over %d trading days", len(events), len(data.closes))

    edges = [float(row["edge"]) for row in events]  # type: ignore[arg-type]
    quiet = [row for row in events if float(row["trend_capped_share"]) <= FILTER_DOMINATED]  # type: ignore[arg-type]
    summary = {
        "all": describe(edges),
        "into_fear": describe(
            [float(row["edge"]) for row in events if row["direction"] == "into_fear"]  # type: ignore[arg-type]
        ),
        "into_greed": describe(
            [float(row["edge"]) for row in events if row["direction"] == "into_greed"]  # type: ignore[arg-type]
        ),
        "trend_filter_quiet": describe([float(row["edge"]) for row in quiet]),  # type: ignore[arg-type]
        "trend_filter_dominated": describe(
            [
                float(row["edge"])  # type: ignore[arg-type]
                for row in events
                if float(row["trend_capped_share"]) > FILTER_DOMINATED  # type: ignore[arg-type]
            ]
        ),
    }
    for name, stats in summary.items():
        if not stats.get("n"):
            continue
        logger.info(
            "%-24s n=%3d  edge mean %+7.2f%%  median %+7.2f%%  sd %6.2f%%  "
            "t %+5.2f  positive %4.1f%%",
            name, int(stats["n"]), stats["mean"] * 100, stats["median"] * 100,
            stats["sd"] * 100, stats["t"], stats["share_positive"] * 100,
        )

    comparisons: list[dict[str, object]] = []
    if not args.ledger_only:
        baseline = (frozen_run.result.nav, change_dates(frozen_run))
        for family in FAMILIES:
            held = frozen_value(config, family.name)
            for value in family.values:
                if value == held:
                    continue
                run = StrategyBacktest(variant(config, family.name, value)).run(
                    data, include_benchmarks=False
                )
                stats = paired(baseline, (run.result.nav, change_dates(run)))
                comparisons.append(
                    {"family": family.name, "frozen": held, "candidate": value, **stats}
                )
                logger.info(
                    "%-22s %-6g vs %-6g  spans %3d  mean %+8.5f  t %+5.2f  "
                    "wins %3d/%-3d  sign p %.3f  total %+7.4f",
                    family.name, value, held, int(stats["n"]), stats["mean"],
                    stats["t"], int(stats["wins"]), int(stats["n"]),
                    stats["sign_test_p"], stats["total_log_gap"],
                )

    report = {
        "task": "TASK-184",
        "strategy_version": config.strategy.strategy_version,
        "attribution": {
            "span": "signal date of one change to the signal date of the next",
            "execution": config.strategy.execution.timing.value,
            "trend_filter": "left in; every event records the share of days it capped",
            "filter_dominated_threshold": FILTER_DOMINATED,
        },
        "events": events,
        "summary": summary,
        "paired_comparisons": comparisons,
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

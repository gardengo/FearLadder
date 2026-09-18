"""How long would ``max_leverage_below = 0.0`` have kept you out? (TASK-186)

    python scripts/cash_exposure.py --db path/to/full.db

§2.5 settled ``max_leverage_below`` by ruling out 1.0x on structure — it prices
the same as simply holding QQQ — and then choosing 0.5x over 0.0x on principle.
The principle was recorded as one number, "현금 100% 인 날 29.9%", measured on
the research window only, and the argument attached to it was that a human has
to actually obey the re-entry rule.

That is the one live parameter whose stated justification was never measured on
the full history, and never measured as the thing it actually claims to be. "A
third of days in cash" is not what a person experiences. **Consecutive months
out of the market** is. A rule that puts you in cash on alternating Tuesdays and
a rule that puts you in cash for fifteen straight months produce the same share
and are not the same rule.

So this turns the share into a distribution of stretch lengths, and then asks
where the performance difference between the two caps actually comes from.

Three parts.

**Binding.** How often the trend filter caps at all. The two variants are
identical on every other day, so this bounds everything below.

**Stretches.** At 0.0, the consecutive runs fully in cash: how many, how long,
and when. At 0.5 the same days are 50% QQQ, so the count there should be zero —
it is measured rather than assumed, because it is the whole of §2.5's argument.

**Decomposition.** The cumulative log gap between the two, split by year, by
crisis vs. calm, and by era. A total is not a verdict if it is one crash wearing
a thirty-year coat.

Read the era split as descriptive, not predictive. Picking 2010 as a boundary is
itself a choice made after seeing the data; it is reported because the real-ETF
window is already a named window in §2.11, not because the number is a forecast.

Nothing here writes to ``config/``. It measures the frozen strategy against one
alternative; acting on it would need a new freeze (§4).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
from pandas import Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.constants import Asset
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.research.backtest_runner import BacktestRun, StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.measurement import with_parameter
from fear_ladder.research.reports import write_json_report

logger = logging.getLogger("cash_exposure")

OUTPUT = paths.REPORTS_DIR / "cash_exposure.json"

#: The two caps §2.5 said were the only real choices. 1.0/1.5 are not here:
#: §2.13 and §2.15 already showed them monotonically and largely worse.
CAP = "max_leverage_below"
CAPS: tuple[float, ...] = (0.0, 0.5)

#: Trading days, used only to label stretch lengths in human units.
MONTH = 21
QUARTER = 63
HALF_YEAR = 126
YEAR = 252

#: Years holding a peak-to-trough decline big enough to be named in §2.11/§2.13.
#: Used to ask whether the gap is a handful of crises rather than a tendency.
CRISIS_YEARS: frozenset[int] = frozenset({2000, 2001, 2002, 2008, 2022})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="full-history database (scripts/collect_full_history.py)",
    )
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--top", type=int, default=10, help="how many longest stretches to record"
    )
    return parser


def equity_share(item: object) -> float:
    """Share of the portfolio not in cash. ``nan`` when there is no allocation.

    Cash is the only sleeve that is not exposure, so one minus its weight is the
    whole answer regardless of which rung produced it.
    """
    allocation = getattr(item, "allocation", None)
    if allocation is None:
        return float("nan")
    weights = allocation.weights
    return float(
        sum(value for asset, value in weights.items() if asset is not Asset.CASH)
    )


def stretches(days: list[date], flagged: set[date]) -> list[tuple[date, date, int]]:
    """Consecutive runs of flagged days, measured in *trading* days.

    Length is counted along ``days``, not the calendar, so a weekend or a market
    holiday does not break a run and does not lengthen one either.
    """
    position = {day: index for index, day in enumerate(days)}
    runs: list[tuple[date, date, int]] = []
    start: date | None = None
    previous: date | None = None
    for day in days:
        if day not in flagged:
            continue
        if start is None or previous is None:
            start = previous = day
            continue
        if position[day] == position[previous] + 1:
            previous = day
            continue
        runs.append((start, previous, position[previous] - position[start] + 1))
        start = previous = day
    if start is not None and previous is not None:
        runs.append((start, previous, position[previous] - position[start] + 1))
    return runs


def describe_stretches(
    runs: list[tuple[date, date, int]], top: int
) -> dict[str, object]:
    if not runs:
        return {"count": 0, "longest_days": 0, "total_days": 0, "longest": []}
    lengths = [length for _, _, length in runs]
    ordered = sorted(runs, key=lambda run: (-run[2], run[0]))
    return {
        "count": len(runs),
        "total_days": int(sum(lengths)),
        "longest_days": int(max(lengths)),
        "median_days": float(np.median(lengths)),
        "mean_days": float(np.mean(lengths)),
        "at_least_1_month": int(sum(1 for n in lengths if n >= MONTH)),
        "at_least_3_months": int(sum(1 for n in lengths if n >= QUARTER)),
        "at_least_6_months": int(sum(1 for n in lengths if n >= HALF_YEAR)),
        "at_least_1_year": int(sum(1 for n in lengths if n >= YEAR)),
        "longest": [
            {
                "start": str(start),
                "end": str(end),
                "days": int(length),
                "years": round(length / YEAR, 2),
            }
            for start, end, length in ordered[:top]
        ],
    }


def performance(nav: Series) -> dict[str, float]:
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    multiple = float(nav.iloc[-1] / nav.iloc[0])
    return {
        "cagr": multiple ** (1.0 / years) - 1.0,
        "max_drawdown": float((nav / nav.cummax() - 1.0).min()),
        "final_multiple": multiple,
        "years": years,
    }


def log_gap(better: Series, worse: Series) -> Series:
    """Cumulative log-return gap, both rebased to their common first day.

    Rebasing is what makes the series a *gap* rather than two different starting
    capitals; the last value is then exactly the total log difference.
    """
    common = better.index.intersection(worse.index)
    left, right = better.loc[common], worse.loc[common]
    return np.log(left / left.iloc[0]) - np.log(right / right.iloc[0])


def decompose(gap: Series, *, crisis: frozenset[int] = CRISIS_YEARS) -> dict[str, object]:
    """Split the gap into the pieces that decide whether a total means anything.

    Daily differences are used throughout, so every split is a partition of the
    same total and no day is counted twice or dropped.

    A move is attributed to the year its interval *ends* in, which is the usual
    calendar-return convention: a year's figure is its last level against the
    previous year's last level. Unlike :func:`era` that costs nothing here,
    because every year boundary is charged the same way on both sides.
    """
    daily = gap.diff().dropna()
    years = [day.year for day in daily.index]
    total = float(daily.sum())
    in_crisis = float(daily[[year in crisis for year in years]].sum())
    by_year = daily.groupby(years).sum()
    return {
        "total": total,
        "crisis_years": sorted(crisis),
        "in_crisis_years": in_crisis,
        "in_other_years": total - in_crisis,
        "years_ahead": int((by_year > 0).sum()),
        "years_total": len(by_year),
        "by_year": {int(year): float(value) for year, value in by_year.items()},
    }


def era(gap: Series, start: date) -> dict[str, float]:
    """The gap accumulated from ``start`` onward, annualised.

    The series is restricted *before* differencing, never after. A daily
    difference stamped on the boundary day was earned over the interval ending
    there, and that interval begins before the era — the same ``(start, end]``
    rule the event ledger uses. Differencing first would hand the era a return
    it did not earn, which on a 2008 boundary is most of a crash.
    """
    window = gap[[day >= start for day in gap.index]]
    if len(window) < 2:
        return {"total": 0.0, "annualised": 0.0, "years": 0.0}
    years = (window.index[-1] - window.index[0]).days / 365.25
    total = float(window.iloc[-1] - window.iloc[0])
    return {
        "total": total,
        "annualised": float(np.exp(total / years) - 1.0) if years > 0 else 0.0,
        "years": years,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    frozen_cap = float(config.strategy.trend_filter.max_leverage_below)
    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)

    runs: dict[float, BacktestRun] = {}
    for cap in CAPS:
        variant_config = (
            config if cap == frozen_cap else with_parameter(config, CAP, cap)
        )
        runs[cap] = StrategyBacktest(variant_config).run(data, include_benchmarks=False)

    days = sorted(set.intersection(*(set(run.allocations) for run in runs.values())))
    capped = {
        day
        for day in days
        if runs[0.0].allocations[day].trend is not None
        and runs[0.0].allocations[day].trend.capped
    }
    logger.info(
        "trend filter binds on %d of %d days (%.1f%%)",
        len(capped),
        len(days),
        100.0 * len(capped) / len(days),
    )

    exposure: dict[str, object] = {}
    for cap, run in runs.items():
        fully_out = {day for day in days if equity_share(run.allocations[day]) <= 1e-9}
        shares = [equity_share(run.allocations[day]) for day in sorted(capped)]
        summary = {
            "days_fully_in_cash": len(fully_out),
            "share_fully_in_cash": len(fully_out) / len(days),
            "equity_share_while_capped": {
                "mean": float(np.mean(shares)) if shares else 0.0,
                "min": float(np.min(shares)) if shares else 0.0,
                "max": float(np.max(shares)) if shares else 0.0,
            },
            "stretches": describe_stretches(stretches(days, fully_out), args.top),
            "performance": performance(run.result.nav),
        }
        exposure[str(cap)] = summary
        logger.info(
            "cap=%-4g  cash %4d days (%4.1f%%)  longest stretch %3d days  "
            "CAGR %6.2f%%  MDD %6.1f%%",
            cap,
            len(fully_out),
            100.0 * len(fully_out) / len(days),
            summary["stretches"]["longest_days"],
            summary["performance"]["cagr"] * 100,
            summary["performance"]["max_drawdown"] * 100,
        )

    gap = log_gap(runs[0.0].result.nav, runs[0.5].result.nav)
    breakdown = decompose(gap)
    logger.info(
        "log gap (0.0 - 0.5) %+0.4f  =  crisis %+0.4f  +  rest %+0.4f  "
        "(0.0 ahead in %d/%d years)",
        breakdown["total"],
        breakdown["in_crisis_years"],
        breakdown["in_other_years"],
        breakdown["years_ahead"],
        breakdown["years_total"],
    )
    eras = {
        label: era(gap, start)
        for label, start in (("2010-", date(2010, 1, 1)), ("2015-", date(2015, 1, 1)))
    }
    for label, stats in eras.items():
        logger.info(
            "%s gap %+0.4f log (%+0.2f%%/yr for cap=0.0)",
            label,
            stats["total"],
            stats["annualised"] * 100,
        )

    report = {
        "task": "TASK-186",
        "strategy_version": config.strategy.strategy_version,
        "frozen_max_leverage_below": frozen_cap,
        "trading_days": len(days),
        "trend_filter_binding_days": len(capped),
        "trend_filter_binding_share": len(capped) / len(days),
        "exposure": exposure,
        "gap": {**breakdown, "eras": eras},
    }
    write_json_report(args.out, report)
    logger.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

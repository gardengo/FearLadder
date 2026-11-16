"""Write the frozen backtest's equity and drawdown curves for the dashboard.

    python scripts/make_equity_curve.py --db path/to/full.db

``reports/performance.json`` holds the frozen strategy's totals — CAGR, worst
drawdown, final multiple — but not the path that produced them, so the backtest
tab could state "202x" and "-62.4%" without being able to show when either
happened. This writes that path to ``reports/equity_curve.json``.

It does not touch ``performance.json`` and cannot disagree with it:

* The curves stop on the report's own last day. The database handed in goes on
  past the freeze; the days after it are the live record, which the history tab
  owns, and running them into this file would move a frozen number.
* Before anything is written, the final multiple of the strategy and of QQQ are
  compared with the ones in the report. If either differs — a provider has
  restated history, or the configuration is not the frozen one — the script
  refuses rather than publish a picture of a different backtest beside the
  numbers of this one.

Like every research script it needs the full history, which the operational
database does not have: build one with ``scripts/collect_full_history.py``.

The curves are stored weekly. Thirty years of daily points is 7,700 rows the
page would have to ship to draw a line a screen wide. Equity takes the last
value of each week; drawdown takes the *worst* value of each week, computed
from the daily curve, so the troughs are the true ones and not whatever the
week happened to close at.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.constants import TRADABLE_SYMBOLS
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.research.backtest_runner import StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.reports import write_json_report

logger = logging.getLogger("equity_curve")

REPORT = paths.REPORTS_DIR / "performance.json"
OUTPUT = paths.REPORTS_DIR / "equity_curve.json"
STRATEGY = "전략"
BENCHMARK = "QQQ"

#: How closely the recomputed final multiples must match the report's. Loose
#: enough for float noise between runs, far tighter than any restated price.
TOLERANCE = 1e-6


class CurveMismatchError(RuntimeError):
    """The recomputed backtest is not the one the report describes."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="a full-history database")
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    return parser


def weekly(series: pd.Series, *, worst: bool = False) -> pd.Series:
    """One point per week, dated the last day that week actually had.

    ``worst`` keeps the lowest value of the week instead of the last — the
    right summary for a drawdown, whose reader is looking for the bottom.
    """
    indexed = series.copy()
    indexed.index = pd.to_datetime(indexed.index)
    weeks = indexed.index.to_period("W-FRI")
    grouped = indexed.groupby(weeks)
    values = grouped.min() if worst else grouped.last()
    last_days = indexed.index.to_series().groupby(weeks).max()
    values.index = pd.DatetimeIndex(last_days.to_numpy())
    return values


def drawdown(nav: pd.Series) -> pd.Series:
    return nav / nav.cummax() - 1.0


def build_curves(
    strategy_nav: pd.Series, benchmark_close: pd.Series, *, end: date, report: dict
) -> dict[str, object]:
    """The payload, or :class:`CurveMismatchError` if it is not this report's backtest."""
    days = [day for day in strategy_nav.index if _as_date(day) <= end]
    if not days:
        raise CurveMismatchError(f"the backtest has no day on or before {end}")
    navs = {
        STRATEGY: strategy_nav[days] / strategy_nav[days].iloc[0],
        BENCHMARK: benchmark_close[days] / benchmark_close[days].iloc[0],
    }

    window = report["window"]
    first, last = _as_date(days[0]), _as_date(days[-1])
    if first.isoformat() != window["start"] or last.isoformat() != window["end"]:
        raise CurveMismatchError(
            f"the recomputed window is {first} .. {last}; the report covers "
            f"{window['start']} .. {window['end']}"
        )
    for name, nav in navs.items():
        expected = float(report["overall"][name]["multiple"])
        actual = float(nav.iloc[-1])
        if abs(actual / expected - 1.0) > TOLERANCE:
            raise CurveMismatchError(
                f"{name} ends at {actual:.6f}x here and {expected:.6f}x in the report"
            )

    equity = {name: weekly(nav) for name, nav in navs.items()}
    falls = {name: weekly(drawdown(nav), worst=True) for name, nav in navs.items()}
    dates = [stamp.date().isoformat() for stamp in equity[STRATEGY].index]
    return {
        "strategy_version": report["strategy_version"],
        "window": dict(window),
        "sampling": "weekly: equity is the week's last value, drawdown its worst",
        "dates": dates,
        "equity": {name: [round(float(v), 5) for v in curve] for name, curve in equity.items()},
        "drawdown": {name: [round(float(v), 5) for v in curve] for name, curve in falls.items()},
    }


def _as_date(day: object) -> date:
    return day.date() if hasattr(day, "date") else day  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    if not args.report.is_file():
        logger.error("no report at %s; run scripts/make_performance_report.py first", args.report)
        return 2
    report = json.loads(args.report.read_text(encoding="utf-8"))

    if args.db.resolve() == paths.default_db_path().resolve():
        logger.error(
            "refusing to read the operational database: it is pruned to five "
            "years and cannot reproduce a thirty-year curve. Build a full "
            "history with scripts/collect_full_history.py and pass that."
        )
        return 2

    config = load_config()
    if config.strategy.strategy_version != report["strategy_version"]:
        logger.error(
            "the configuration is %s but the report is for %s",
            config.strategy.strategy_version,
            report["strategy_version"],
        )
        return 2

    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)
    closes = data.closes
    first = max(closes[symbol].dropna().index[0] for symbol in TRADABLE_SYMBOLS)
    run = StrategyBacktest(config).run(data, start=first, include_benchmarks=False)

    try:
        payload = build_curves(
            run.result.nav,
            closes[BENCHMARK],
            end=date.fromisoformat(report["window"]["end"]),
            report=report,
        )
    except CurveMismatchError as exc:
        logger.error("refusing to write: %s", exc)
        return 2

    write_json_report(args.out, payload)
    logger.info(
        "wrote %s: %d weekly points, %s .. %s, strategy %.1fx, worst drawdown %.1f%%",
        args.out,
        len(payload["dates"]),  # type: ignore[arg-type]
        payload["dates"][0],  # type: ignore[index]
        payload["dates"][-1],  # type: ignore[index]
        payload["equity"][STRATEGY][-1],  # type: ignore[index]
        min(payload["drawdown"][STRATEGY]) * 100,  # type: ignore[index]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""How much does the frozen strategy depend on *which day* a regime change lands on?

    python scripts/transition_sensitivity.py --db path/to/full.db

``transition.minimum_duration_days`` holds a rung for 75 trading days. After
that the rung changes on the first day the score sits clear of the current band
by ``hysteresis`` — and it moves to *that one day's* band. With
``confirmation_days = 1`` there is no persistence test at all: hysteresis is a
level filter, not a time filter, and the composite score moves a median 3.5
points a day against bands 15 points wide.

So one day's reading picks the leverage for the next three and a half months.
This measures what that costs, two ways, without changing the frozen strategy:

``lag``
    The transition engine sees the score as it stood *k* trading days ago.
    Strictly causal, and it changes which day lands on day 76 — so it prices
    trigger-day luck directly. If the strategy is robust, a one-day delay
    should barely register.

``smooth``
    The transition engine sees a *k*-day trailing mean instead of one close.
    This is the fix the mechanism suggests, measured before anyone adopts it:
    does requiring the score to *stay* somewhere help, or is the single-day
    reading fine?

Both intervene only where the rung is chosen. Prices, indicators, the score
itself, the trend filter and the TQQQ gate are untouched, so any difference is
attributable to the trigger and nothing else.

**This measures the frozen strategy; it does not propose a new one.** The OOS
split is already consumed (``docs/strategy.md`` §2.8), so acting on anything
here means a new freeze, labelled as such.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pandas import Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.regime.transition import TransitionEngine
from fear_ladder.research.backtest_runner import StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.performance import cash_curve, window_stats

logger = logging.getLogger("sensitivity")

OUTPUT = paths.REPORTS_DIR / "transition_sensitivity.json"
ETFS = ("QQQ", "QLD", "TQQQ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--lags", type=int, nargs="*", default=[1, 2, 3, 5],
        help="trading days to delay the score the rung is chosen from",
    )
    parser.add_argument(
        "--windows", type=int, nargs="*", default=[3, 5, 10, 20],
        help="trailing-mean lengths to choose the rung from",
    )
    return parser


@contextmanager
def _trigger(transform: Callable[[Series], Series] | None) -> Iterator[None]:
    """Run the regime replay on a transformed score, and only the replay.

    Patched rather than parameterised because the production engine has no seam
    for this and must not grow one for a research question
    (``BACKTEST_SPEC.md`` §28). The patch is undone on the way out, so a variant
    cannot leak into the next one.
    """
    if transform is None:
        yield
        return
    original = TransitionEngine.run

    def patched(self: TransitionEngine, scores: Series) -> object:
        return original(self, transform(scores))

    TransitionEngine.run = patched  # type: ignore[method-assign]
    try:
        yield
    finally:
        TransitionEngine.run = original  # type: ignore[method-assign]


def _lagged(days: int) -> Callable[[Series], Series]:
    """Yesterday's score, or the one from ``days`` trading days ago."""
    return lambda scores: scores.shift(days)


def _smoothed(window: int) -> Callable[[Series], Series]:
    """A trailing mean. ``min_periods=1`` so the warm-up is not blanked out,
    which would silently move the strategy's start date."""
    return lambda scores: scores.rolling(window, min_periods=1).mean()


def _variants(lags: list[int], windows: list[int]) -> list[tuple[str, object]]:
    rows: list[tuple[str, object]] = [("baseline", None)]
    rows += [(f"lag {days}d", _lagged(days)) for days in lags]
    rows += [(f"mean {window}d", _smoothed(window)) for window in windows]
    return rows


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    logger.info(
        "%s (%s) — minimum_duration=%s, confirmation=%s, hysteresis=%s",
        config.strategy.strategy_version,
        config.strategy.parameter_status.value,
        config.strategy.transition.minimum_duration_days,
        config.strategy.transition.confirmation_days,
        config.strategy.transition.hysteresis,
    )

    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)

    closes = data.closes
    first = max(closes[symbol].dropna().index[0] for symbol in ETFS)

    results = []
    for label, transform in _variants(args.lags, args.windows):
        with _trigger(transform):
            run = StrategyBacktest(config).run(data, start=first, include_benchmarks=False)
        nav = run.result.nav
        days = list(nav.index)
        stats = window_stats(nav / nav.iloc[0], cash_curve(data.cash_rates, days))
        results.append(
            {
                "variant": label,
                "cagr": stats["cagr"],
                "max_drawdown": stats["max_drawdown"],
                "sharpe": stats["sharpe"],
                "multiple": stats["multiple"],
                "regime_changes": run.regime_change_count,
                "years": stats["years"],
            }
        )
        logger.info(
            "%-12s CAGR %6.2f%%  MDD %6.1f%%  Sharpe %.2f  changes %3d",
            label,
            stats["cagr"] * 100,
            stats["max_drawdown"] * 100,
            stats["sharpe"],
            run.regime_change_count,
        )

    baseline = results[0]
    for row in results:
        row["cagr_vs_baseline"] = row["cagr"] - baseline["cagr"]

    report = {
        "strategy_version": config.strategy.strategy_version,
        "window": {"start": str(first), "end": str(closes.index.max())},
        "transition": {
            "confirmation_days": config.strategy.transition.confirmation_days,
            "hysteresis": config.strategy.transition.hysteresis,
            "minimum_duration_days": config.strategy.transition.minimum_duration_days,
        },
        "variants": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.info("wrote %s", args.out)

    spread = max(row["cagr"] for row in results) - min(row["cagr"] for row in results)
    logger.info("CAGR spread across variants: %.2f%%p", spread * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

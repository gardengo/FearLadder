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

``--sweep`` asks the neighbouring question: not *which day* the rung changes on,
but whether the three transition constants sit anywhere defensible. The
walk-forward folds picked d = 30/60/45/45/60 and never the frozen 75
(``docs/strategy.md`` §2.6), so the shape of that curve over the full history is
worth seeing rather than inferring. The frozen configuration is copied, never
written: ``config/`` is untouched.

**This measures the frozen strategy; it does not propose a new one.** The OOS
split is already consumed (``docs/strategy.md`` §2.8), so acting on anything
here means a new freeze, labelled as such.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pandas import Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.config.schema import AppConfig
from fear_ladder.constants import TRADABLE_SYMBOLS
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.regime.transition import TransitionEngine
from fear_ladder.research.backtest_runner import StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.measurement import with_block
from fear_ladder.research.performance import cash_curve, window_stats
from fear_ladder.research.reports import write_json_report

logger = logging.getLogger("sensitivity")

OUTPUT = paths.REPORTS_DIR / "transition_sensitivity.json"
SWEEP_OUTPUT = paths.REPORTS_DIR / "transition_parameter_sweep.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--lags", type=int, nargs="*", default=[1, 2, 3, 5],
        help="trading days to delay the score the rung is chosen from",
    )
    parser.add_argument(
        "--windows", type=int, nargs="*", default=[3, 5, 10, 20],
        help="trailing-mean lengths to choose the rung from",
    )
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--sweep", action="store_true",
        help="sweep the transition constants instead of the trigger",
    )
    parser.add_argument(
        "--durations", type=int, nargs="*",
        default=[20, 30, 45, 60, 75, 90, 105, 120, 150],
        help="minimum_duration_days values to try under --sweep",
    )
    parser.add_argument(
        "--confirmations", type=int, nargs="*", default=[1, 2, 3, 5],
        help="confirmation_days values to try under --sweep",
    )
    parser.add_argument(
        "--hystereses", type=float, nargs="*", default=[0.0, 2.0, 5.0, 8.0, 12.0],
        help="hysteresis values to try under --sweep",
    )
    return parser


def _sweeps(
    config: AppConfig, durations: list[int], confirmations: list[int], hystereses: list[float]
) -> list[tuple[str, None, AppConfig]]:
    """One variant per constant value, the frozen value included in each family."""
    spec = config.strategy.transition
    #: prefix -> the constant it sweeps, and the values to try.
    families: tuple[tuple[str, str, list[float]], ...] = (
        ("d", "minimum_duration_days", list(durations)),
        ("c", "confirmation_days", list(confirmations)),
        ("h", "hysteresis", list(hystereses)),
    )
    rows: list[tuple[str, None, AppConfig]] = []
    for prefix, name, values in families:
        held = getattr(spec, name)
        for value in values:
            mark = " *" if value == held else ""
            variant = with_block(config, "transition", **{name: value})
            rows.append((f"{prefix}={value:g}{mark}", None, variant))
    return rows


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
    if args.out is None:
        args.out = SWEEP_OUTPUT if args.sweep else OUTPUT
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
    first = max(closes[symbol].dropna().index[0] for symbol in TRADABLE_SYMBOLS)
    if args.start:
        first = max(first, args.start)
    last = args.end

    if args.sweep:
        plan = _sweeps(config, args.durations, args.confirmations, args.hystereses)
    else:
        plan = [
            (label, transform, config) for label, transform in _variants(args.lags, args.windows)
        ]

    results = []
    for label, transform, variant_config in plan:
        with _trigger(transform):
            run = StrategyBacktest(variant_config).run(
                data, start=first, end=last, include_benchmarks=False
            )
        nav = run.result.nav
        days = list(nav.index)
        stats = window_stats(nav, cash_curve(data.cash_rates, days))
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
        "window": {"start": str(first), "end": str(last or closes.index.max())},
        "transition": {
            "confirmation_days": config.strategy.transition.confirmation_days,
            "hysteresis": config.strategy.transition.hysteresis,
            "minimum_duration_days": config.strategy.transition.minimum_duration_days,
        },
        "variants": results,
    }
    write_json_report(args.out, report)
    logger.info("wrote %s", args.out)

    spread = max(row["cagr"] for row in results) - min(row["cagr"] for row in results)
    logger.info("CAGR spread across variants: %.2f%%p", spread * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""What decides how much this strategy loses in a *large* crash?

    python scripts/tail_risk.py --db path/to/full.db

``docs/strategy.md`` §2.11 asks a question §2.10 could not: the leveraged
sleeves are reconstructed before 2010-02-11, and the only index decline deeper
than 40% in the dataset (QQQ −83.0%, 2000–2002) sits entirely inside that
reconstructed era. So every drawdown-based conclusion drawn from the research
window is priced by a *model*, and the real-priced era contains no crash to
check it against.

That cuts both ways, and this script measures both sides:

``--windows``
    The same sweep run over crash eras and calm eras separately. A parameter
    whose ranking flips between them is not measuring what it appears to.

``max_leverage_below`` / ``min_depth_to_engage``
    The two trend-filter constants that decide what the strategy holds once the
    trend has broken — the only place a deep decline is actually resisted.
    Unlike ``transition.minimum_duration_days`` and ``transition.hysteresis``,
    which came out of §2.10/§2.11 as sawtooth noise, these two are monotone in
    a crash, so the ordering survives the model's pricing error even where the
    absolute level does not.

Every variant is a *copy* of the frozen config (the models are frozen and
``config/`` is the freeze's evidence). Nothing here writes to ``config/``.

**This measures the frozen strategy; it does not propose a new one.** The OOS
split is already consumed (``docs/strategy.md`` §2.8), so acting on anything
here means a new freeze, labelled as such.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pandas import Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.research.backtest_runner import StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.performance import cash_curve, window_stats

logger = logging.getLogger("tail_risk")

OUTPUT = paths.REPORTS_DIR / "tail_risk.json"
SLEEVES = ("QLD", "TQQQ")

#: Named eras. The split that matters is not research/validation/OOS but
#: *crash versus calm*, and — separately — *modelled versus real* sleeve prices.
WINDOWS: tuple[tuple[str, date | None, date | None], ...] = (
    ("dotcom 1999-2003", date(1999, 3, 10), date(2003, 12, 31)),
    ("gfc 2007-2009", date(2007, 1, 1), date(2009, 12, 31)),
    ("real 2010-2026", date(2010, 2, 11), None),
    ("oos 2021-2026", date(2021, 2, 27), None),
    ("full 1996-2026", None, None),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--caps", type=float, nargs="*", default=[0.0, 0.25, 0.5, 1.0],
        help="trend_filter.max_leverage_below values to try",
    )
    parser.add_argument(
        "--depths", type=float, nargs="*", default=[0.0, 0.1, 0.2, 0.3],
        help="trend_filter.min_depth_to_engage values to try",
    )
    return parser


def _with_trend_filter(config: object, **changes: object) -> object:
    """The frozen config with some trend-filter constants replaced.

    Copied, never mutated: the models are frozen and ``config/`` is the
    freeze's evidence.
    """
    strategy = config.strategy  # type: ignore[attr-defined]
    trend_filter = strategy.trend_filter.model_copy(update=changes)
    return config.model_copy(  # type: ignore[attr-defined]
        update={"strategy": strategy.model_copy(update={"trend_filter": trend_filter})}
    )


def _variants(config: object, caps: list[float], depths: list[float]) -> list[tuple[str, object]]:
    spec = config.strategy.trend_filter  # type: ignore[attr-defined]
    rows: list[tuple[str, object]] = []
    for cap in caps:
        mark = " *" if cap == spec.max_leverage_below else ""
        rows.append((f"cap={cap:g}{mark}", _with_trend_filter(config, max_leverage_below=cap)))
    for depth in depths:
        mark = " *" if depth == spec.min_depth_to_engage else ""
        rows.append(
            (f"depth={depth:g}{mark}", _with_trend_filter(config, min_depth_to_engage=depth))
        )
    return rows


def _worst_drawdown(nav: Series) -> tuple[date, date, float]:
    """Peak, trough and depth of the single deepest decline."""
    drawdown = nav / nav.cummax() - 1.0
    trough = drawdown.idxmin()
    peak = nav.loc[:trough].idxmax()
    return peak, trough, float(drawdown.min())


def _real_sleeve_start(config: object) -> date:
    """The first day on which no held sleeve is a reconstructed price."""
    symbols = config.data_sources.price.symbols  # type: ignore[attr-defined]
    return max(symbols[symbol].inception for symbol in SLEEVES)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    boundary = _real_sleeve_start(config)
    logger.info(
        "%s (%s) — max_leverage_below=%s, min_depth_to_engage=%s; sleeves real from %s",
        config.strategy.strategy_version,
        config.strategy.parameter_status.value,
        config.strategy.trend_filter.max_leverage_below,
        config.strategy.trend_filter.min_depth_to_engage,
        boundary,
    )

    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)

    plan = _variants(config, args.caps, args.depths)
    windows = []
    for label, start, end in WINDOWS:
        rows = []
        for name, variant in plan:
            run = StrategyBacktest(variant).run(
                data, start=start, end=end, include_benchmarks=False
            )
            nav = run.result.nav
            stats = window_stats(nav / nav.iloc[0], cash_curve(data.cash_rates, list(nav.index)))
            peak, trough, depth = _worst_drawdown(nav)
            rows.append(
                {
                    "variant": name,
                    "cagr": stats["cagr"],
                    "max_drawdown": depth,
                    "sharpe": stats["sharpe"],
                    # The finding this script exists for: a drawdown that
                    # happened before the boundary is priced by the
                    # reconstruction, not observed.
                    "drawdown_peak": str(peak),
                    "drawdown_trough": str(trough),
                    "sleeves_reconstructed": trough < boundary,
                }
            )
            logger.info(
                "%-18s %-10s CAGR %6.2f%%  MDD %6.1f%%  Sharpe %5.2f  %s..%s%s",
                label, name, stats["cagr"] * 100, depth * 100, stats["sharpe"],
                peak, trough, "  (modelled sleeves)" if trough < boundary else "",
            )
        windows.append({"window": label, "start": str(start or ""), "end": str(end or ""),
                        "variants": rows})

    report = {
        "strategy_version": config.strategy.strategy_version,
        "real_sleeve_start": str(boundary),
        "trend_filter": {
            "max_leverage_below": config.strategy.trend_filter.max_leverage_below,
            "min_depth_to_engage": config.strategy.trend_filter.min_depth_to_engage,
        },
        "windows": windows,
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

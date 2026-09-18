"""How accurate is the reconstructed leveraged sleeve *inside a crash*? (TASK-181)

    python scripts/reconstruction_accuracy.py --db path/to/full.db

``docs/strategy.md`` §2.11 withdrew the drawdown argument because the only index
decline deeper than 40% in the dataset (QQQ −83.0%, 2000–2002) sits entirely
inside the era where both leveraged sleeves are *modelled*. The strategy's
headline −62.4% is therefore priced by
:mod:`fear_ladder.data.collectors.synthetic`, not observed.

So: how good is that model when the market is falling fast? The docstring of
``synthetic.py`` already reports whole-life tracking (QLD R² 0.990, cumulative
−1.5%), but a fund's whole life is overwhelmingly calm days, and the error that
matters accumulates in the weeks nobody was calm.

QLD makes the question answerable. It listed 2006-06-21 and lived through the
financial crisis with **real** prices: QQQ −53.6%, QLD −84.0%. Running the model
over that same window and comparing is the closest thing to a controlled test of
the 2000–2002 pricing that this dataset allows.

Two columns here that a whole-life figure cannot give:

``annualised_drift``
    A −1.5% cumulative error over 20 years is −0.08%/yr. The same −1.5% over a
    16-month crash is −1.1%/yr. Cumulative errors from windows of different
    length are not comparable, and this column is what makes them so.

``drawdown_error_pp``
    The number §2.11 actually turns on. Positive means the model's decline is
    *shallower* than the real fund's, and a model that understates the loss is
    the direction that would make the strategy's −62.4% optimistic.

**Read any 3x row as an upper bound, never as a measurement.** TQQQ only has
real prices from 2010-02-11, so no 3x sleeve has ever been observed through a
>50% index decline. The leverage-scaling section measures how the error grows
from 2x to 3x over the windows where both are real, which is the most this
dataset can say about carrying a QLD result onto TQQQ.

Nothing here writes to ``config/``. This is a post-hoc measurement of the frozen
strategy's *inputs*; it proposes no parameter change.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
from pandas import DataFrame, Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.config.schema import AppConfig
from fear_ladder.data.collectors.registry import build_registry
from fear_ladder.data.collectors.synthetic import (
    INCEPTION,
    TRADING_DAYS_PER_YEAR,
    SyntheticLeveragedProvider,
    daily_financing,
)
from fear_ladder.data.interfaces import MarketObservationRepository
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.research.backtest_runner import MarketData, StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.measurement import with_parameter
from fear_ladder.research.performance import cash_curve, window_stats
from fear_ladder.research.reconstruction import implied_drag, model_path
from fear_ladder.research.reports import write_json_report

logger = logging.getLogger("reconstruction_accuracy")

OUTPUT = paths.REPORTS_DIR / "reconstruction_accuracy.json"
UNDERLYING = "QQQ"

#: Enough calendar days before a window to define its first daily return.
WARMUP = timedelta(days=15)

#: The one observation the stress test is built on: a 2x sleeve priced through
#: a >50% index decline at real prices. There is no second one in this dataset.
STRESS_SOURCE_WINDOW = "gfc 2007-2009"
STRESS_SOURCE_SYMBOL = "QLD"

#: Charge crisis carry only while the index is this far below its own high.
DEFAULT_STRESS_THRESHOLD = 0.20

#: ``real 2010-2026`` is the control, not a result: the stress only touches
#: prices before a sleeve listed, so that row has to come back unchanged. If it
#: ever moves, the correction has leaked into observed prices.
STRESS_WINDOWS: tuple[tuple[str, date | None, date | None], ...] = (
    ("full 1996-2026", None, None),
    ("dotcom 1999-2003", date(1999, 3, 10), date(2003, 12, 31)),
    ("real 2010-2026", date(2010, 2, 11), None),
)


@dataclass(frozen=True, slots=True)
class Window:
    """A named era, and why it is in the list."""

    label: str
    kind: str
    start: date | None
    end: date | None
    note: str


#: Crash windows run from the index peak to the *leveraged* trough, which comes
#: later than the index's own — volatility decay keeps taking from a 2x fund
#: while the index has already turned. Calm windows are whole calendar years
#: containing no bear market, so they isolate the drift that the whole-life
#: figure is mostly made of.
WINDOWS: tuple[Window, ...] = (
    Window(
        "gfc 2007-2009", "crash", date(2007, 10, 31), date(2009, 3, 9),
        "the only >50% index decline a leveraged sleeve has lived through at real prices",
    ),
    Window(
        "gfc rebound 2009-2010", "rebound", date(2009, 3, 9), date(2010, 12, 31),
        "the other half of a crash: a violent recovery, where a 2x fund also compounds oddly",
    ),
    Window(
        "covid 2020", "crash", date(2020, 2, 19), date(2020, 3, 23),
        "fastest decline in the record, and short enough that drift cannot explain the error",
    ),
    Window(
        "bear 2022", "crash", date(2021, 11, 19), date(2022, 12, 28),
        "the strategy's own worst real-priced drawdown (§2.11)",
    ),
    Window(
        "calm 2013-2015", "calm", date(2013, 1, 1), date(2015, 12, 31),
        "reference: what the error looks like when nothing happens",
    ),
    Window(
        "calm 2016-2019", "calm", date(2016, 1, 1), date(2019, 12, 31),
        "reference: a second calm stretch, so the calm number is not one fluke",
    ),
    Window(
        "whole life", "all", None, None,
        "each fund from its own inception — the figure synthetic.py's docstring quotes",
    ),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=None,
        help="full-history database (scripts/collect_full_history.py). The operational "
             "one holds five years and every window here reaches past that.",
    )
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--stress-threshold", type=float, default=DEFAULT_STRESS_THRESHOLD,
        help="index drawdown past which crisis carry is charged (default 0.20)",
    )
    parser.add_argument(
        "--caps", type=float, nargs="*", default=[0.0, 0.25, 0.5, 1.0, 1.5],
        help="trend_filter.max_leverage_below values to re-run the stress test at "
             "(config/strategy.yaml's own research_candidates)",
    )
    parser.add_argument(
        "--no-stress", action="store_true",
        help="measure the model only; skip re-running the strategy on stressed prices",
    )
    return parser


def closes(
    repository: MarketObservationRepository,
    symbol: str,
    start: date | None,
    end: date | None,
) -> Series:
    """Stored closing prices — the same field the backtest prices sleeves with."""
    observations = repository.get_observations(symbol, start=start, end=end)
    series = Series(
        {row.observation_date: float(row.close) for row in observations if row.close is not None},
        dtype="float64",
    ).sort_index()
    series.index.name = "observation_date"
    return series


def worst_drawdown(prices: Series) -> tuple[date, float]:
    drawdown = prices / prices.cummax() - 1.0
    return drawdown.idxmin(), float(drawdown.min())


def compare(real: Series, model: Series) -> dict[str, object]:
    """Model against observation, on the days both exist."""
    shared = real.dropna().index.intersection(model.dropna().index)
    real, model = real.loc[shared], model.loc[shared]

    real_returns = real.pct_change().dropna()
    model_returns = model.pct_change().dropna()
    both = real_returns.index.intersection(model_returns.index)
    difference = model_returns.loc[both] - real_returns.loc[both]

    real_growth = float(real.iloc[-1] / real.iloc[0])
    model_growth = float(model.iloc[-1] / model.iloc[0])
    cumulative = model_growth / real_growth - 1.0
    years = len(shared) / TRADING_DAYS_PER_YEAR

    real_trough, real_drawdown = worst_drawdown(real)
    model_trough, model_drawdown = worst_drawdown(model)
    return {
        "days": len(shared),
        "years": years,
        "r_squared": float(real_returns.loc[both].corr(model_returns.loc[both])) ** 2,
        "cumulative_error": cumulative,
        "annualised_drift": (1.0 + cumulative) ** (1.0 / years) - 1.0 if years > 0 else 0.0,
        "tracking_error_annual": float(difference.std()) * TRADING_DAYS_PER_YEAR**0.5,
        "max_path_error": float(
            ((model / model.iloc[0]) / (real / real.iloc[0]) - 1.0).abs().max()
        ),
        "real_max_drawdown": real_drawdown,
        "model_max_drawdown": model_drawdown,
        "real_drawdown_trough": str(real_trough),
        "model_drawdown_trough": str(model_trough),
        # Positive: the model's decline is shallower than the fund's, so a
        # backtest priced by it would understate the loss.
        "drawdown_error_pp": (model_drawdown - real_drawdown) * 100.0,
        "optimistic": model_drawdown > real_drawdown,
    }


def measure_window(
    repository: MarketObservationRepository,
    window: Window,
    symbol: str,
    provider: SyntheticLeveragedProvider,
) -> dict[str, object] | None:
    """One fund over one window, or ``None`` when its real prices do not cover it."""
    inception = provider.inceptions[symbol]
    start = window.start or inception
    if start < inception:
        logger.info(
            "%-22s %-5s skipped: real prices only start %s", window.label, symbol, inception
        )
        return None

    real = closes(repository, symbol, start, window.end)
    underlying = closes(repository, UNDERLYING, start - WARMUP, window.end)
    if len(real) < 2 or underlying.empty:
        logger.warning("%-22s %-5s skipped: no stored prices", window.label, symbol)
        return None

    financing = (
        daily_financing(provider.financing_rates, underlying.index)
        if provider.financing_rates is not None
        else Series(0.0, index=underlying.index)
    )
    leverage = provider.leverages[symbol]
    drag = provider.drags.get(symbol, 0.0)
    model = model_path(underlying, real, leverage=leverage, financing=financing, drag=drag)
    solved = implied_drag(underlying, real, leverage=leverage, financing=financing)
    row: dict[str, object] = {
        "symbol": symbol,
        "leverage": leverage,
        "start": str(real.index[0]),
        "end": str(real.index[-1]),
        "configured_drag": drag,
        "implied_drag": solved,
        "drag_shortfall": None if solved is None else solved - drag,
        **compare(real, model),
    }
    logger.info(
        "%-22s %-5s %4dd  R2 %.4f  cum %+6.2f%%  drift %+6.2f%%/yr  "
        "MDD real %6.1f%% model %6.1f%% (%+5.1f%%p%s)  drag %.2f%% vs implied %s",
        window.label, symbol, row["days"], row["r_squared"],
        row["cumulative_error"] * 100, row["annualised_drift"] * 100,
        row["real_max_drawdown"] * 100, row["model_max_drawdown"] * 100,
        row["drawdown_error_pp"], ", optimistic" if row["optimistic"] else "",
        drag * 100, "n/a" if solved is None else f"{solved * 100:.2f}%",
    )
    return row


def find_measurement(
    windows: list[dict[str, object]], label: str, symbol: str
) -> dict[str, object] | None:
    for window in windows:
        if window["window"] != label:
            continue
        funds: list[dict[str, object]] = window["funds"]  # type: ignore[assignment]
        for row in funds:
            if row["symbol"] == symbol:
                return row
    return None


def stress_drags(shortfall: float, provider: SyntheticLeveragedProvider) -> dict[str, float]:
    """Spread the measured crisis shortfall across the sleeves by borrowed exposure.

    Stated because it cannot be tested: the excess cost is treated as a
    financing spread charged on borrowed notional, so it scales with ``L−1``.
    That is the standard structure of the swap a leveraged fund holds, but no
    3x fund existed in 2008 to confirm it — which is why the 3x number is a
    bound, not a measurement. The observed corroboration is weaker and one-sided:
    in every crash window where both sleeves are real, the 3x shortfall is the
    larger of the two.
    """
    reference = provider.leverages[STRESS_SOURCE_SYMBOL] - 1.0
    return {
        symbol: shortfall * (leverage - 1.0) / reference
        for symbol, leverage in provider.leverages.items()
    }


def stressed_prices(
    prices: DataFrame, *, extra: dict[str, float], threshold: float
) -> DataFrame:
    """Reconstructed sleeve prices, re-priced as if crisis carry had been charged.

    Anchored at each sleeve's inception exactly as the reconstruction is: a fund
    that bled more carry on its way to a known price must have *started* higher.
    So the corrected history starts higher and falls further, which is the
    direction that matters — it deepens the modelled drawdown rather than
    flattering it — and the seam itself does not move.

    Charged only while the index is more than ``threshold`` below its own high.
    The shortfall is a funding-stress effect: the calm windows imply a *negative*
    drag, so charging it every day would be the wrong correction, not a
    conservative one.
    """
    drawdown = prices[UNDERLYING] / prices[UNDERLYING].cummax() - 1.0
    under_stress = (drawdown <= -threshold).astype("float64")

    stressed = prices.copy()
    for symbol, drag in extra.items():
        if symbol not in stressed.columns or drag <= 0.0:
            continue
        carried = (under_stress * (drag / TRADING_DAYS_PER_YEAR)).cumsum()
        seam = carried[carried.index <= INCEPTION[symbol]]
        if seam.empty:  # the frame starts after this sleeve listed: nothing modelled
            continue
        # Clipping at zero leaves every post-seam day at a factor of exactly 1.
        stressed[symbol] = prices[symbol] * np.exp((seam.iloc[-1] - carried).clip(lower=0.0))
    return stressed


def stress_test(
    config: AppConfig, data: MarketData, *, extra: dict[str, float], threshold: float
) -> dict[str, object]:
    """Re-run the frozen strategy with the crash-era sleeves priced at crisis carry.

    Only the sleeve *prices* move. Every indicator in this system reads QQQ, so
    the regime path is identical by construction — the comparison is purely
    about what the portfolio was worth, which is the claim §2.11 rests on.
    """
    stressed = MarketData(
        series=data.series,
        closes=stressed_prices(data.closes, extra=extra, threshold=threshold),
        opens=(
            None
            if data.opens is None
            else stressed_prices(data.opens, extra=extra, threshold=threshold)
        ),
        cash_rates=data.cash_rates,
    )

    rows = []
    for label, start, end in STRESS_WINDOWS:
        measured = {}
        for name, inputs in (("baseline", data), ("stressed", stressed)):
            run = StrategyBacktest(config).run(
                inputs, start=start, end=end, include_benchmarks=False
            )
            nav = run.result.nav
            stats = window_stats(
                nav / nav.iloc[0], cash_curve(inputs.cash_rates, list(nav.index))
            )
            trough, depth = worst_drawdown(nav)
            measured[name] = {
                "cagr": stats["cagr"],
                "max_drawdown": depth,
                "sharpe": stats["sharpe"],
                "drawdown_peak": str(nav.loc[:trough].idxmax()),
                "drawdown_trough": str(trough),
            }
        rows.append(
            {
                "window": label,
                "start": str(start or ""),
                "end": str(end or ""),
                **measured,
                "drawdown_change_pp": (
                    measured["stressed"]["max_drawdown"] - measured["baseline"]["max_drawdown"]
                )
                * 100.0,
                "cagr_change_pp": (
                    measured["stressed"]["cagr"] - measured["baseline"]["cagr"]
                ) * 100.0,
            }
        )
        logger.info(
            "stress %-18s MDD %6.1f%% -> %6.1f%% (%+5.1f%%p)   CAGR %6.2f%% -> %6.2f%%",
            label,
            measured["baseline"]["max_drawdown"] * 100,
            measured["stressed"]["max_drawdown"] * 100,
            rows[-1]["drawdown_change_pp"],
            measured["baseline"]["cagr"] * 100,
            measured["stressed"]["cagr"] * 100,
        )
    return {
        "source": f"{STRESS_SOURCE_WINDOW} / {STRESS_SOURCE_SYMBOL} drag_shortfall",
        "index_drawdown_threshold": threshold,
        "extra_drag": extra,
        "windows": rows,
    }


def stress_by_cap(
    config: AppConfig,
    data: MarketData,
    *,
    extra: dict[str, float],
    threshold: float,
    caps: list[float],
) -> list[dict[str, object]]:
    """How much the answer above depends on the one parameter §2.11 could not settle.

    The frozen strategy barely notices the model's crash error because the trend
    filter has already cut leverage by the time the error accumulates. That is a
    property of ``max_leverage_below = 0.5``, not of the reconstruction.

    Note what the cap caps: *leverage*, not allocation. At any value up to 1.0
    the broken-trend portfolio is QQQ and cash, both priced from real quotes
    (QQQ back to 1999-03-10, spliced only before that), so the model has almost
    nothing to be wrong about. Only the 1.5 candidate keeps a leveraged sleeve
    on through the decline, and only there does the reconstruction get to set
    the depth of the drawdown. That is the row to read.
    """
    stressed = MarketData(
        series=data.series,
        closes=stressed_prices(data.closes, extra=extra, threshold=threshold),
        opens=(
            None
            if data.opens is None
            else stressed_prices(data.opens, extra=extra, threshold=threshold)
        ),
        cash_rates=data.cash_rates,
    )
    frozen = config.strategy.trend_filter.max_leverage_below
    rows: list[dict[str, object]] = []
    for cap in caps:
        variant = with_parameter(config, "max_leverage_below", cap)
        depths = {}
        for name, inputs in (("baseline", data), ("stressed", stressed)):
            nav = StrategyBacktest(variant).run(inputs, include_benchmarks=False).result.nav
            depths[name] = worst_drawdown(nav)[1]
        rows.append(
            {
                "max_leverage_below": cap,
                "frozen": cap == frozen,
                "baseline_max_drawdown": depths["baseline"],
                "stressed_max_drawdown": depths["stressed"],
                "drawdown_change_pp": (depths["stressed"] - depths["baseline"]) * 100.0,
            }
        )
        logger.info(
            "stress cap=%-5g          MDD %6.1f%% -> %6.1f%% (%+5.1f%%p)%s",
            cap, depths["baseline"] * 100, depths["stressed"] * 100,
            rows[-1]["drawdown_change_pp"], "  *frozen" if cap == frozen else "",
        )
    return rows


def leverage_scaling(windows: list[dict[str, object]]) -> list[dict[str, object]]:
    """How much worse the error gets from 2x to 3x, where both are observed.

    The 2008 answer is a QLD answer, and the strategy holds TQQQ in exactly the
    regimes a crash produces. Whether a 2x result may be quoted about a 3x
    sleeve at all depends on this ratio.
    """
    rows: list[dict[str, object]] = []
    for window in windows:
        funds: list[dict[str, object]] = window["funds"]  # type: ignore[assignment]
        by_symbol = {row["symbol"]: row for row in funds}
        if not {"QLD", "TQQQ"} <= by_symbol.keys():
            continue
        two, three = by_symbol["QLD"], by_symbol["TQQQ"]
        tracking_2x: float = two["tracking_error_annual"]  # type: ignore[assignment]
        tracking_3x: float = three["tracking_error_annual"]  # type: ignore[assignment]
        rows.append(
            {
                "window": window["window"],
                "kind": window["kind"],
                "cumulative_error_2x": two["cumulative_error"],
                "cumulative_error_3x": three["cumulative_error"],
                "drawdown_error_pp_2x": two["drawdown_error_pp"],
                "drawdown_error_pp_3x": three["drawdown_error_pp"],
                "tracking_error_ratio": tracking_3x / tracking_2x if tracking_2x else None,
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    provider = build_registry(config.data_sources).price
    if not isinstance(provider, SyntheticLeveragedProvider):
        raise SystemExit(
            "price reconstruction is disabled in config/data_sources.yaml; "
            "there is no model to measure"
        )
    if provider.financing_rates is None:
        logger.warning(
            "no financing series: what gets measured here is the optimistic model, "
            "not the one the backtest is priced with"
        )

    windows: list[dict[str, object]] = []
    with SQLiteUnitOfWork(args.db) as uow:
        for window in WINDOWS:
            funds = [
                row
                for symbol in sorted(provider.leverages)
                if (row := measure_window(uow.observations, window, symbol, provider))
            ]
            if not funds:
                continue
            windows.append(
                {
                    "window": window.label,
                    "kind": window.kind,
                    "note": window.note,
                    "start": str(window.start or ""),
                    "end": str(window.end or ""),
                    "funds": funds,
                }
            )
        data = None if args.no_stress else load_market_data(uow.observations, config)

    stress: dict[str, object] | None = None
    source = find_measurement(windows, STRESS_SOURCE_WINDOW, STRESS_SOURCE_SYMBOL)
    if data is None:
        pass
    elif source is None or source["drag_shortfall"] is None:
        logger.warning(
            "no %s / %s measurement to build a stress test from",
            STRESS_SOURCE_WINDOW, STRESS_SOURCE_SYMBOL,
        )
    elif (shortfall := float(source["drag_shortfall"])) <= 0:  # type: ignore[arg-type]
        logger.info(
            "the model was not optimistic in %s (shortfall %.2f%%/yr); nothing to stress",
            STRESS_SOURCE_WINDOW, shortfall * 100,
        )
    else:
        extra = stress_drags(shortfall, provider)
        stress = stress_test(config, data, extra=extra, threshold=args.stress_threshold)
        stress["by_cap"] = stress_by_cap(
            config, data, extra=extra, threshold=args.stress_threshold, caps=args.caps
        )

    report = {
        "task": "TASK-181",
        "strategy_version": config.strategy.strategy_version,
        "underlying": UNDERLYING,
        "financing_symbol": config.data_sources.price.reconstruction.financing_symbol,
        "financing_available": provider.financing_rates is not None,
        "drags": {symbol: provider.drags.get(symbol) for symbol in sorted(provider.leverages)},
        "windows": windows,
        "leverage_scaling": leverage_scaling(windows),
        "stress": stress,
    }
    write_json_report(args.out, report)
    logger.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

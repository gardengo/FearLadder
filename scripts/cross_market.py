"""Does the same strategy respond the same way on the S&P 500? (TASK-182)

    python scripts/cross_market.py --db path/to/full.db

Every time window is spent. Research ended 2015-08, validation 2021-02, and the
OOS split was consumed in ``docs/strategy.md`` §2.8. The only axis with any
independence left is **a different market**, so this runs the frozen strategy
on SPY / SSO / UPRO and asks whether the *shape* of its parameter response
survives the move.

What is being compared is not the score. Indicator thresholds were searched on
the Nasdaq-100, so a worse CAGR on the S&P would mean very little. What means
something is whether a parameter that looked like structure still looks like
structure, and whether one that looked like noise still looks like noise:

``trend_filter.max_leverage_below``
    §2.11 called this monotone and therefore real. If it is monotone here too,
    on a different index, that is the closest thing to an out-of-sample
    confirmation this project can still buy.

``transition.minimum_duration_days`` / ``transition.hysteresis``
    §2.10/§2.11 called both sawtooth — an effect the size of noise whose
    direction flips between windows. A second market that also refuses to rank
    them consistently settles it.

Method. The engine's sleeve names are definitional (``constants.Asset``) and the
indicator set reads ``QQQ`` by configuration, so the S&P prices are relabelled
at the boundary: SPY becomes ``QQQ``, SSO becomes ``QLD``, UPRO becomes
``TQQQ``. Nothing downstream is copied or modified — the frozen config and the
same chain the daily worker runs are used verbatim. The relabelling happens in
memory only; no row anywhere claims SPY is QQQ.

The sentiment and rate inputs (VIX, CNN, AAII, the bill rate) are read from the
same database as the Nasdaq run. VIX is S&P implied volatility to begin with, so
if anything it fits this market better than the one it was searched on.

SSO and UPRO are reconstructed before their inceptions exactly as QLD/TQQQ are,
with a drag *fitted over each fund's own real life* rather than borrowed from
the Nasdaq funds — see :func:`fit_drag`. UPRO listed 2009-06-25, so no 3x sleeve
is real in 2008 here either; the gap is the same shape as the Nasdaq one.

**This measures the frozen strategy; it does not propose a new one.** Nothing
here writes to ``config/``, and a result that favoured some other parameter
value would need a new freeze (§4) rather than an edit.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd
from pandas import DataFrame, Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.config.schema import AppConfig
from fear_ladder.constants import Asset
from fear_ladder.data.collectors.fdr import FinanceDataReaderPriceProvider
from fear_ladder.data.collectors.registry import build_registry
from fear_ladder.data.collectors.synthetic import (
    MEASURED_DRAG,
    SyntheticLeveragedProvider,
    daily_financing,
)
from fear_ladder.data.interfaces import MarketObservationRepository
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.research.backtest_runner import MarketData, StrategyBacktest
from fear_ladder.research.data_loader import load_market_data, required_symbols
from fear_ladder.research.performance import cash_curve, window_stats
from fear_ladder.research.reconstruction import implied_drag

logger = logging.getLogger("cross_market")

OUTPUT = paths.REPORTS_DIR / "cross_market.json"

#: Enough calendar days before a window to define its first daily return.
WARMUP = timedelta(days=15)

#: Both markets are compared from here. SPY has traded since 1993-01-29, so the
#: S&P side needs no index splice at all — three years of warm-up are already
#: real quotes, where the Nasdaq side has to reach back through NDX.
START = date(1996, 1, 2)


@dataclass(frozen=True, slots=True)
class Prices:
    """One market's price matrix, keyed by the engine sleeve each column stands in for.

    The fitted drags travel with the prices because they *are* part of them —
    every pre-inception row was priced with them, and a cached matrix whose
    drags were forgotten could not say how it was built.
    """

    closes: DataFrame
    opens: DataFrame
    #: Real symbol -> fitted annual carry. Empty for a market with nothing modelled.
    drags: dict[str, float]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "drags": self.drags,
                    "closes": _records(self.closes),
                    "opens": _records(self.opens),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def read(cls, path: Path) -> Prices:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            closes=_frame(payload["closes"]),
            opens=_frame(payload["opens"]),
            drags=payload["drags"],
        )


def _records(frame: DataFrame) -> dict[str, dict[str, float]]:
    return {str(day): row.dropna().to_dict() for day, row in frame.iterrows()}


def _frame(records: dict[str, dict[str, float]]) -> DataFrame:
    frame = DataFrame.from_dict(records, orient="index")
    frame.index = pd.Index(
        [date.fromisoformat(day) for day in frame.index], name="observation_date"
    )
    # Sleeve order, not alphabetical: a cached matrix has to come back shaped
    # exactly like a freshly fetched one or the two runs are not the same run.
    order = [asset.value for asset in Asset if asset.value in frame.columns]
    return frame.reindex(columns=order).sort_index()


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """One fund family, named by the engine sleeve each of its funds stands in for."""

    name: str
    #: engine sleeve -> the symbol actually traded
    symbols: dict[str, str]
    inceptions: dict[str, date]

    @property
    def underlying(self) -> str:
        return self.symbols[Asset.QQQ.value]

    @property
    def sleeves(self) -> dict[str, str]:
        """The leveraged sleeves only — the ones that need reconstructing."""
        return {
            engine: symbol
            for engine, symbol in self.symbols.items()
            if engine != Asset.QQQ.value
        }


SP500 = MarketSpec(
    name="sp500",
    symbols={"QQQ": "SPY", "QLD": "SSO", "TQQQ": "UPRO"},
    inceptions={
        "SPY": date(1993, 1, 29),
        "SSO": date(2006, 6, 21),
        # Not 2008. UPRO listed after the crisis, so the 3x sleeve is modelled
        # through it here just as TQQQ is on the Nasdaq side.
        "UPRO": date(2009, 6, 25),
    },
)

LEVERAGE = {"QLD": 2.0, "TQQQ": 3.0}

#: Crash windows are where a claim about tail behaviour has to hold; ``real``
#: is where both markets have observed prices for every sleeve (UPRO from
#: 2009-06-25, TQQQ from 2010-02-11, so 2010-02-11 clears both).
WINDOWS: tuple[tuple[str, date | None, date | None], ...] = (
    ("full 1996-2026", None, None),
    ("dotcom 1999-2003", date(1999, 3, 10), date(2003, 12, 31)),
    ("gfc 2007-2009", date(2007, 1, 1), date(2009, 12, 31)),
    ("real 2010-2026", date(2010, 2, 11), None),
)


@dataclass(frozen=True, slots=True)
class Sweep:
    """One parameter family, its grid, and what §2.11 expects of it."""

    family: str
    values: tuple[float, ...]
    expectation: str


SWEEPS: tuple[Sweep, ...] = (
    Sweep(
        "max_leverage_below", (0.0, 0.25, 0.5, 1.0, 1.5),
        "monotone in drawdown (§2.11 calls this the one real effect)",
    ),
    Sweep(
        "minimum_duration_days", (20.0, 30.0, 45.0, 60.0, 75.0, 90.0, 105.0, 120.0, 150.0),
        "sawtooth in performance, monotone in turnover only",
    ),
    Sweep(
        "hysteresis", (0.0, 2.0, 5.0, 8.0, 12.0),
        "sawtooth; direction flips between windows",
    ),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=None,
        help="full-history database (scripts/collect_full_history.py). Supplies the "
             "Nasdaq side and the sentiment/rate series both markets share.",
    )
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--cache", type=Path, default=None,
        help="JSON to keep the fetched S&P prices and fitted drags in, so re-runs need no network",
    )
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    return parser


# ------------------------------------------------------------------- the data


def fit_drag(
    base: FinanceDataReaderPriceProvider,
    spec: MarketSpec,
    symbol: str,
    *,
    leverage: float,
    financing: Series | None,
    end: date,
) -> float:
    """The all-in annual carry this fund actually bore, over its own real life.

    ``MEASURED_DRAG`` holds the same quantity for QLD and TQQQ, fitted the same
    way. Borrowing those numbers for SSO/UPRO would import the Nasdaq funds'
    fee structure into a result about the S&P, which is exactly the kind of
    shared input that would make a "cross-market" check less independent than
    it looks.

    **Expect the S&P sleeves to fit a negative drag, and do not read that as a
    free lunch.** This number is a residual, not a fee. The replication identity
    is driven by *price* returns on both sides, so it silently omits the index
    dividend, and the leveraged fund gets that dividend back through its swap
    while distributing only its own smaller one. The residual is therefore
    roughly ``L × index yield − fund yield − costs``, and it turns negative
    wherever the index yields enough. It does on the S&P: SPY yields about three
    times what QQQ does, which is the whole of the difference between QLD's
    +0.68%/yr and SSO's −1.2%/yr. Fitting per fund is what keeps that asymmetry
    out of the comparison instead of inside it.
    """
    inception = spec.inceptions[symbol]
    real = base.fetch(symbol, inception, end)["close"].dropna().astype("float64")
    underlying = (
        base.fetch(spec.underlying, inception - WARMUP, end)["close"]
        .dropna()
        .astype("float64")
    )
    rates = (
        daily_financing(financing, underlying.index)
        if financing is not None
        else Series(0.0, index=underlying.index)
    )
    drag = implied_drag(underlying, real, leverage=leverage, financing=rates)
    if drag is None:
        raise SystemExit(f"{symbol}: no plausible carry explains the model/fund gap")
    logger.info(
        "%s: fitted drag %.2f%%/yr over %d real days from %s",
        symbol, drag * 100, len(real), inception,
    )
    return drag


def fetch_prices(spec: MarketSpec, *, end: date, financing: Series | None) -> Prices:
    """Close prices for the family, keyed by the engine sleeve each one stands in for.

    Reconstruction of the leveraged sleeves goes through the *same provider* the
    operational path uses, with this family's inceptions and fitted drags passed
    in. It is the identical model, not a second implementation of it.
    """
    base = FinanceDataReaderPriceProvider(
        underlying_sources=dict.fromkeys(
            spec.symbols.values(), "FinanceDataReader US equity feed"
        )
    )
    drags = {
        symbol: fit_drag(
            base, spec, symbol, leverage=LEVERAGE[engine], financing=financing, end=end
        )
        for engine, symbol in spec.sleeves.items()
    }
    provider = SyntheticLeveragedProvider(
        base=base,
        underlying_symbol=spec.underlying,
        leverages={symbol: LEVERAGE[engine] for engine, symbol in spec.sleeves.items()},
        drags=drags,
        financing_rates=financing,
        inceptions=spec.inceptions,
    )
    closes, opens = {}, {}
    for engine, symbol in spec.symbols.items():
        frame = provider.fetch(symbol, START, end)
        closes[engine] = frame["close"].astype("float64")
        # A reconstructed day has no open. Falling back to its close is what
        # load_market_data does for the Nasdaq sleeves, so NEXT_OPEN degrades to
        # NEXT_CLOSE on exactly the same days in both markets.
        opens[engine] = pd.to_numeric(frame["open"], errors="coerce").fillna(closes[engine])
        logger.info(
            "%s -> %s: %d days %s .. %s",
            symbol, engine, len(frame), frame.index.min(), frame.index.max(),
        )
    return Prices(
        closes=DataFrame(closes).dropna(how="any"),
        opens=DataFrame(opens),
        drags=drags,
    )


def relabelled_data(
    repository: MarketObservationRepository, config: AppConfig, prices: Prices
) -> MarketData:
    """The frozen inputs with one market's prices swapped in for another's.

    The scalar inputs (VIX, sentiment, the bill rate) come from the database
    unchanged — they are not Nasdaq-specific, and holding them fixed is what
    isolates the price series as the only thing that differs.
    """
    scalars = [
        symbol
        for symbol in required_symbols(config)
        if symbol not in config.data_sources.price.symbols
    ]
    closes = prices.closes
    series = repository.get_series_frame(scalars)
    series = series.reindex(series.index.union(closes.index)).sort_index()
    for column in closes.columns:
        series[column] = closes[column]

    cash = config.data_sources.cash_rate_series
    rates = series[cash].dropna() if cash in series.columns else None
    return MarketData(
        series=series,
        closes=closes,
        opens=prices.opens.reindex(closes.index),
        cash_rates=rates,
    )


# ---------------------------------------------------------------- the sweeps


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


def measure(
    config: AppConfig, data: MarketData, start: date | None, end: date | None
) -> dict[str, float]:
    run = StrategyBacktest(config).run(data, start=start, end=end, include_benchmarks=False)
    nav = run.result.nav
    stats = window_stats(nav / nav.iloc[0], cash_curve(data.cash_rates, list(nav.index)))
    return {
        "cagr": stats["cagr"],
        "max_drawdown": float((nav / nav.cummax() - 1.0).min()),
        "sharpe": stats["sharpe"],
        "regime_changes": float(run.regime_change_count),
    }


def direction(values: list[float]) -> str:
    """``increasing``/``decreasing`` only when every step agrees. Otherwise ``none``.

    The distinction §2.11 rests on. A monotone response survives a pricing error
    that shifts every row together; a sawtooth one does not survive anything.
    """
    steps = [later - earlier for earlier, later in pairwise(values)]
    if all(step > 0 for step in steps):
        return "increasing"
    if all(step < 0 for step in steps):
        return "decreasing"
    return "none"


def spearman(left: list[float], right: list[float]) -> float:
    return float(Series(left).rank().corr(Series(right).rank(), method="pearson"))


def run_sweeps(
    config: AppConfig, markets: dict[str, MarketData]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    sweeps: list[dict[str, object]] = []
    agreement: list[dict[str, object]] = []

    for sweep in SWEEPS:
        held = frozen_value(config, sweep.family)
        rows: list[dict[str, object]] = []
        for label, start, end in WINDOWS:
            measured: dict[str, dict[float, dict[str, float]]] = {}
            for market, data in markets.items():
                measured[market] = {}
                for value in sweep.values:
                    stats = measure(variant(config, sweep.family, value), data, start, end)
                    measured[market][value] = stats
                    rows.append(
                        {
                            "window": label,
                            "market": market,
                            "value": value,
                            "frozen": value == held,
                            **stats,
                        }
                    )
                    logger.info(
                        "%-18s %-9s %-22s=%-6g CAGR %6.2f%%  MDD %6.1f%%  "
                        "Sharpe %5.2f  changes %3d%s",
                        label, market, sweep.family, value, stats["cagr"] * 100,
                        stats["max_drawdown"] * 100, stats["sharpe"],
                        int(stats["regime_changes"]), "  *" if value == held else "",
                    )
            agreement.extend(_agreement(sweep, label, measured))
        sweeps.append(
            {
                "family": sweep.family,
                "expectation": sweep.expectation,
                "frozen_value": held,
                "values": list(sweep.values),
                "rows": rows,
            }
        )
    return sweeps, agreement


def _agreement(
    sweep: Sweep, window: str, measured: dict[str, dict[float, dict[str, float]]]
) -> list[dict[str, object]]:
    """Per metric: is each market monotone, and do the two rank alike?"""
    rows: list[dict[str, object]] = []
    markets = sorted(measured)
    for metric in ("cagr", "max_drawdown", "sharpe"):
        series = {
            market: [measured[market][value][metric] for value in sweep.values]
            for market in markets
        }
        row: dict[str, object] = {
            "family": sweep.family,
            "window": window,
            "metric": metric,
            "direction": {market: direction(values) for market, values in series.items()},
        }
        if len(markets) == 2:
            row["spearman"] = spearman(series[markets[0]], series[markets[1]])
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    end = args.end or date.today()

    reconstruction = build_registry(config.data_sources).price
    financing = getattr(reconstruction, "financing_rates", None)
    if financing is None:
        logger.warning(
            "no financing series: both markets are reconstructed without borrowing "
            "cost, which overstates every leveraged sleeve before its inception"
        )

    if args.cache and args.cache.is_file():
        prices = Prices.read(args.cache)
        logger.info("read %d cached S&P days from %s", len(prices.closes), args.cache)
    else:
        prices = fetch_prices(SP500, end=end, financing=financing)
        if args.cache:
            prices.write(args.cache)
            logger.info("cached S&P prices to %s", args.cache)

    with SQLiteUnitOfWork(args.db) as uow:
        markets = {
            "nasdaq100": load_market_data(uow.observations, config, end=args.end),
            SP500.name: relabelled_data(uow.observations, config, prices),
        }
    for name, data in markets.items():
        logger.info(
            "%s: %d trading days %s .. %s",
            name, len(data.closes), data.closes.index.min(), data.closes.index.max(),
        )

    sweeps, agreement = run_sweeps(config, markets)
    report = {
        "task": "TASK-182",
        "strategy_version": config.strategy.strategy_version,
        "markets": {
            "nasdaq100": {
                "symbols": {symbol: symbol for symbol in config.data_sources.price.symbols},
                "inceptions": {
                    symbol: str(spec.inception)
                    for symbol, spec in config.data_sources.price.symbols.items()
                },
                "fitted_drag": dict(MEASURED_DRAG),
            },
            SP500.name: {
                "symbols": dict(SP500.symbols),
                "inceptions": {
                    symbol: str(day) for symbol, day in SP500.inceptions.items()
                },
                "fitted_drag": prices.drags,
            },
        },
        "windows": [
            {"window": label, "start": str(start or ""), "end": str(end_ or "")}
            for label, start, end_ in WINDOWS
        ],
        "sweeps": sweeps,
        "agreement": agreement,
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

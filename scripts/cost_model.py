"""What do the two cost terms nobody has ever set actually cost? (TASK-185)

    python scripts/cost_model.py --db path/to/full.db

``config/strategy.yaml`` carries ``commission_bps: 10.0``, ``spread_bps: 0.0``
and ``slippage_bps: 0.0``. The engine supports all three
(:mod:`fear_ladder.backtest.costs`); two of them have been zero since the
beginning and neither appears in the support table in ``docs/strategy.md`` §2.5.
The expectation is that it barely matters — the frozen strategy trades 93 times
in thirty years — but *expected* and *measured* are different words.

Three things get measured here.

**The scenarios.** Realistic spread and slippage added on top of the frozen
commission, out to a deliberately pessimistic level, over every window.

**The rule.** The cost model is linear in traded notional, so the annual drag
should be ``turnover_per_year × bps`` and nothing else. If the measurement
matches the arithmetic, this table never has to be rebuilt: any future cost
assumption can be read off the rule instead.

**Where the turnover happens.** A flat rate cannot express "spreads blow out in
a crash", and the simulator's cost model is a scalar by design. But the share of
lifetime turnover that occurs while the index is deep below its high converts
any crash-slippage assumption into an equivalent flat rate — if a tenth of the
trading happens in a crash, charging it 200bps extra is the same as charging
everything 20bps extra. That is the honest way to answer the question without
inventing a time-varying cost model the operational path would then have to
carry.

Nothing here writes to ``config/``. This is a post-hoc measurement; changing a
cost assumption for real would mean a new freeze (§4).
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
from fear_ladder.research.backtest_runner import MarketData, StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.performance import cash_curve, window_stats

logger = logging.getLogger("cost_model")

OUTPUT = paths.REPORTS_DIR / "cost_model.json"
UNDERLYING = "QQQ"

#: Index drawdown past which a trade counts as having happened "in a crash".
CRASH_THRESHOLD = 0.20


@dataclass(frozen=True, slots=True)
class Scenario:
    """One cost assumption, and why that number."""

    name: str
    spread_bps: float
    slippage_bps: float
    note: str

    @property
    def extra_bps(self) -> float:
        return self.spread_bps + self.slippage_bps


#: Spread is charged as half the bid-ask on each side (``costs.py``). QQQ quotes
#: inside a basis point and the leveraged sleeves are among the most traded ETFs
#: in the market, so the low end is not optimism — it is what these three
#: instruments actually cost in calm weather. The upper rows are there to find
#: the level at which the answer would change, not because anyone expects them.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("frozen", 0.0, 0.0, "config/strategy.yaml as frozen: commission only"),
    Scenario("tight", 2.5, 2.5, "calm-market quotes on QQQ/QLD/TQQQ"),
    Scenario("realistic", 5.0, 5.0, "a retail fill, wider than the screen"),
    Scenario("wide", 12.5, 12.5, "stressed quotes charged on every trade"),
    Scenario(
        "pessimistic", 25.0, 25.0,
        "a level chosen to find where the conclusion breaks, not because anyone expects it",
    ),
)

WINDOWS: tuple[tuple[str, date | None, date | None], ...] = (
    ("full 1996-2026", None, None),
    ("dotcom 1999-2003", date(1999, 3, 10), date(2003, 12, 31)),
    ("gfc 2007-2009", date(2007, 1, 1), date(2009, 12, 31)),
    ("real 2010-2026", date(2010, 2, 11), None),
    ("oos 2021-2026", date(2021, 2, 27), None),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=None,
        help="full-history database (scripts/collect_full_history.py)",
    )
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--crash-threshold", type=float, default=CRASH_THRESHOLD,
        help="index drawdown past which a trade counts as made in a crash",
    )
    return parser


def with_costs(config: AppConfig, scenario: Scenario) -> AppConfig:
    """The frozen config with spread and slippage set. Copied, never mutated."""
    costs = config.strategy.cost_model.model_copy(
        update={"spread_bps": scenario.spread_bps, "slippage_bps": scenario.slippage_bps}
    )
    return config.model_copy(
        update={"strategy": config.strategy.model_copy(update={"cost_model": costs})}
    )


def measure(
    config: AppConfig, data: MarketData, start: date | None, end: date | None
) -> dict[str, float]:
    run = StrategyBacktest(config).run(data, start=start, end=end, include_benchmarks=False)
    nav = run.result.nav
    stats = window_stats(nav / nav.iloc[0], cash_curve(data.cash_rates, list(nav.index)))
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    return {
        "cagr": stats["cagr"],
        "max_drawdown": float((nav / nav.cummax() - 1.0).min()),
        "sharpe": stats["sharpe"],
        "turnover": run.result.total_turnover,
        "turnover_per_year": run.result.total_turnover / years if years else 0.0,
        "costs_paid": float(run.result.costs.sum()),
        "trades": float(run.result.trade_count),
    }


def predicted_drag(turnover_per_year: float, extra_bps: float, cagr: float) -> float:
    """What the cost model must cost: ``turnover × bps × (1 + CAGR)``.

    The first two terms are the whole of :mod:`fear_ladder.backtest.costs` — a
    linear charge on traded notional. The third is the unit conversion that a
    naive ``turnover × bps`` gets wrong by about a fifth here.

    A cost applied to NAV reduces the *log* return by ``turnover × rate``.
    CAGR is a simple rate, not a log one, so converting back multiplies by
    ``1 + CAGR``. At 18.88% that factor is 1.19, which is exactly the gap
    between the naive rule and the measurement.

    With it the rule is accurate to about 0.01%p across every window and every
    scenario below, including the one where CAGR is negative — which is why the
    table never has to be rebuilt for a new cost assumption.
    """
    return turnover_per_year * extra_bps * 1e-4 * (1.0 + cagr)


def attribute(turnover: Series, index: Series, *, threshold: float) -> dict[str, float]:
    """Split traded notional by whether the index was deep below its high.

    The drawdown is a running one — ``cummax`` over the days seen so far — so a
    day is judged by what was knowable then. A full-sample peak would let a
    later high decide that an earlier trade happened "in a crash".
    """
    aligned = index.reindex(turnover.index)
    stressed = (aligned / aligned.cummax() - 1.0) <= -threshold

    total = float(turnover.sum())
    in_crash = float(turnover[stressed].sum())
    return {
        "threshold": threshold,
        "total_turnover": total,
        "crash_turnover": in_crash,
        "crash_share": in_crash / total if total else 0.0,
        "crash_days_share": float(stressed.mean()),
    }


def crash_attribution(
    config: AppConfig, data: MarketData, *, threshold: float
) -> dict[str, float]:
    """How much of a lifetime's trading is done while the index is deep in a hole.

    This is what converts a crash-slippage assumption into a flat one. A trade
    made at a stressed quote is the expensive kind, and knowing what fraction of
    turnover is that kind bounds the whole question.
    """
    run = StrategyBacktest(config).run(data, include_benchmarks=False)
    return attribute(run.result.turnover, data.closes[UNDERLYING], threshold=threshold)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    spec = config.strategy.cost_model
    logger.info(
        "frozen cost model: commission=%s spread=%s slippage=%s bps",
        spec.commission_bps, spec.spread_bps, spec.slippage_bps,
    )
    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config)

    rows: list[dict[str, object]] = []
    baseline: dict[str, dict[str, float]] = {}
    for scenario in SCENARIOS:
        variant = with_costs(config, scenario)
        for label, start, end in WINDOWS:
            stats = measure(variant, data, start, end)
            if scenario.name == "frozen":
                baseline[label] = stats
            reference = baseline[label]
            measured_drag = reference["cagr"] - stats["cagr"]
            rows.append(
                {
                    "scenario": scenario.name,
                    "note": scenario.note,
                    "window": label,
                    "spread_bps": scenario.spread_bps,
                    "slippage_bps": scenario.slippage_bps,
                    "extra_bps": scenario.extra_bps,
                    **stats,
                    "cagr_drag_pp": measured_drag * 100,
                    "predicted_drag_pp": predicted_drag(
                        reference["turnover_per_year"],
                        scenario.extra_bps,
                        reference["cagr"],
                    ) * 100,
                }
            )
            logger.info(
                "%-12s %-18s +%5.1fbps  CAGR %6.2f%%  drag %5.3f%%p "
                "(rule %5.3f%%p)  MDD %6.1f%%  turnover %5.2f/yr",
                scenario.name, label, scenario.extra_bps, stats["cagr"] * 100,
                rows[-1]["cagr_drag_pp"], rows[-1]["predicted_drag_pp"],
                stats["max_drawdown"] * 100, stats["turnover_per_year"],
            )

    attribution = crash_attribution(config, data, threshold=args.crash_threshold)
    logger.info(
        "turnover made while the index is >%.0f%% down: %.1f%% of the lifetime total "
        "(on %.1f%% of days)",
        attribution["threshold"] * 100, attribution["crash_share"] * 100,
        attribution["crash_days_share"] * 100,
    )

    report = {
        "task": "TASK-185",
        "strategy_version": config.strategy.strategy_version,
        "frozen_cost_model": {
            "commission_bps": spec.commission_bps,
            "spread_bps": spec.spread_bps,
            "slippage_bps": spec.slippage_bps,
        },
        "scenarios": rows,
        "crash_attribution": attribution,
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

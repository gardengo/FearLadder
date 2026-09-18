"""Shared plumbing for the post-freeze measurement scripts.

``docs/research-backlog.md`` §3 produced nine one-off scripts under ``scripts/``
that all ask the same shape of question: *take the frozen strategy, change one
constant, and see what that does over a named stretch of history.* Each grew its
own copy of the three moving parts — the named eras, the config variant, the
headline numbers — and the copies had begun to drift apart.

``ARCHITECTURE.md`` §3 says the scripts are thin CLIs and the logic lives in the
package, so the three parts live here.

Nothing in this module writes to ``config/``: variants are **copies**. The
models are frozen and ``config/`` is the freeze's evidence (``BACKTEST_SPEC.md``
§27), so a measurement that mutated them would destroy the thing it is measured
against.
"""

from __future__ import annotations

from datetime import date
from typing import Any, NamedTuple

from fear_ladder.config.schema import AppConfig
from fear_ladder.research.backtest_runner import BacktestRun, MarketData
from fear_ladder.research.performance import cash_curve, window_stats

# ------------------------------------------------------------------ the eras


class Era(NamedTuple):
    """One named stretch of history, as ``(label, start, end)``.

    A :class:`~typing.NamedTuple` rather than a dataclass so that it still
    unpacks as the plain triple the report rows are built from.
    """

    label: str
    start: date | None
    end: date | None


#: The split that matters to these measurements is not research/validation/OOS
#: but *crash versus calm* and — separately — *modelled versus real* sleeve
#: prices. The leveraged sleeves are reconstructed before 2010-02-11
#: (``docs/strategy.md`` §2.11), so every drawdown drawn from an earlier era is
#: priced by a model rather than observed; ``real`` is the era that is not.
#:
#: The labels carry their own date range because they are written verbatim into
#: the report JSON under ``reports/``, where a reader has no other context.
ERAS: dict[str, Era] = {
    "full": Era("full 1996-2026", None, None),
    "dotcom": Era("dotcom 1999-2003", date(1999, 3, 10), date(2003, 12, 31)),
    "gfc": Era("gfc 2007-2009", date(2007, 1, 1), date(2009, 12, 31)),
    "real": Era("real 2010-2026", date(2010, 2, 11), None),
    "oos": Era("oos 2021-2026", date(2021, 2, 27), None),
}


def eras(*names: str) -> tuple[Era, ...]:
    """The named eras, in the order asked for.

    Each script reports a different subset in a different order, so the order is
    the caller's; misspelling a name is a mistake worth failing on rather than
    silently measuring four eras where five were meant.
    """
    unknown = [name for name in names if name not in ERAS]
    if unknown:
        raise KeyError(f"unknown era(s) {unknown}; known: {sorted(ERAS)}")
    return tuple(ERAS[name] for name in names)


# -------------------------------------------------------------- the variants

#: Which strategy block each swept constant lives in, and how its value is
#: typed. ``minimum_duration_days`` and ``confirmation_days`` are counts of
#: trading days: a sweep hands them floats, and the config rejects those.
PARAMETER_FAMILIES: dict[str, tuple[str, type]] = {
    "max_leverage_below": ("trend_filter", float),
    "min_depth_to_engage": ("trend_filter", float),
    "minimum_duration_days": ("transition", int),
    "confirmation_days": ("transition", int),
    "hysteresis": ("transition", float),
}


def with_block(config: AppConfig, block: str, **changes: Any) -> AppConfig:
    """``config`` with one strategy sub-block's fields replaced.

    Copied, never mutated — see the module docstring.
    """
    section = getattr(config.strategy, block).model_copy(update=changes)
    strategy = config.strategy.model_copy(update={block: section})
    return config.model_copy(update={"strategy": strategy})


def with_parameter(config: AppConfig, family: str, value: float) -> AppConfig:
    """``config`` with one swept constant replaced, by its bare name."""
    block, cast = _family(family)
    return with_block(config, block, **{family: cast(value)})


def frozen_value(config: AppConfig, family: str) -> float:
    """What the frozen strategy actually holds for ``family``.

    Every sweep includes the frozen value so the table has a row to read the
    others against.
    """
    block, _ = _family(family)
    return float(getattr(getattr(config.strategy, block), family))


def _family(family: str) -> tuple[str, type]:
    try:
        return PARAMETER_FAMILIES[family]
    except KeyError:
        raise ValueError(
            f"unknown parameter family {family!r}; known: {sorted(PARAMETER_FAMILIES)}"
        ) from None


# ------------------------------------------------------------- the numbers


def headline(run: BacktestRun, data: MarketData) -> dict[str, float]:
    """The four numbers every one of these tables reports.

    Sharpe is measured net of cash (:func:`window_stats`), which is what makes a
    cash-heavy variant comparable to a leveraged one at all.
    """
    nav = run.result.nav
    stats = window_stats(nav, cash_curve(data.cash_rates, list(nav.index)))
    return {
        "cagr": stats["cagr"],
        "max_drawdown": stats["max_drawdown"],
        "sharpe": stats["sharpe"],
        "regime_changes": float(run.regime_change_count),
    }

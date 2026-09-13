"""Benchmarks (TASK-074).

``BACKTEST_SPEC.md`` 3 names exactly three::

    QQQ 100%
    QLD 100%
    QLD 70% + Cash 30%

and §17 requires the same cost model for all of them, so they are expressed as
ordinary target streams and run through the *same* simulator as the strategy.
No benchmark gets its own code path, which is the only way "동일 조건으로"
can be checked rather than asserted.

One judgement call is recorded here. "QLD 70% + Cash 30%" does not say whether
it is rebalanced. Left to drift, a 2x sleeve grows to ~100% of the book over a
long sample, so the label stops describing the portfolio. The default is
therefore a monthly rebalance, with ``rebalance`` exposed so the drifting
variant can be run and reported alongside.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd
from pandas import DatetimeIndex, Index

from fear_ladder.constants import Asset

RebalanceFrequency = Literal["none", "monthly", "quarterly", "annual"]

Targets = dict[date, dict[Asset, float]]


@dataclass(frozen=True, slots=True)
class BenchmarkSpec:
    """A static portfolio and how often it is restored to its weights."""

    name: str
    weights: Mapping[Asset, float]
    rebalance: RebalanceFrequency = "none"

    def __post_init__(self) -> None:
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"benchmark {self.name!r} sums to {total!r}, not 1.0")
        if any(weight < 0 for weight in self.weights.values()):
            raise ValueError(f"benchmark {self.name!r} has a negative weight")


def build_targets(spec: BenchmarkSpec, index: Index) -> tuple[Targets, list[date]]:
    """Target stream and forced-rebalance dates for a static benchmark.

    The target is dated on the first available day, so the simulator executes it
    on the next one — the same t+1 rule the strategy obeys.
    """
    if len(index) == 0:
        return {}, []
    days = list(index)
    targets: Targets = {days[0]: dict(spec.weights)}
    forced = rebalance_dates(index, spec.rebalance)
    return targets, forced


def rebalance_dates(index: Index, frequency: RebalanceFrequency) -> list[date]:
    """Last trading day of each period, excluding the first day."""
    if frequency == "none" or len(index) == 0:
        return []
    moments = DatetimeIndex(pd.to_datetime(list(index)))
    rule = {"monthly": "ME", "quarterly": "QE", "annual": "YE"}[frequency]
    grouped = pd.Series(range(len(moments)), index=moments).resample(rule).last().dropna()
    positions = sorted({int(position) for position in grouped})
    return [index[position] for position in positions if position > 0]


def standard_benchmarks(*, mix_rebalance: RebalanceFrequency = "monthly") -> list[BenchmarkSpec]:
    """The three comparisons ``BACKTEST_SPEC.md`` 3 requires."""
    return [
        BenchmarkSpec(name="QQQ 100%", weights={Asset.QQQ: 1.0}),
        BenchmarkSpec(name="QLD 100%", weights={Asset.QLD: 1.0}),
        BenchmarkSpec(
            name="QLD 70% / Cash 30%",
            weights={Asset.QLD: 0.7, Asset.CASH: 0.3},
            rebalance=mix_rebalance,
        ),
    ]


def baseline_position() -> BenchmarkSpec:
    """The starting position ``PRD.md`` 2.1 describes, for reference.

    Not one of the three required benchmarks, but the portfolio the strategy is
    meant to improve on, so it is worth being able to plot.
    """
    return BenchmarkSpec(
        name="Baseline QLD 70 / Cash 30",
        weights={Asset.QLD: 0.7, Asset.CASH: 0.3},
        rebalance="monthly",
    )

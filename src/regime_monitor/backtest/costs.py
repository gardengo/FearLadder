"""Transaction cost model (TASK-072).

``BACKTEST_SPEC.md`` 17 requires the same model for the strategy and for every
benchmark, and requires both the zero-cost and the with-cost result to be kept.
The three components are modelled separately because they behave differently in
reality even though they combine linearly here:

``commission``
    Broker fee, per traded notional.
``spread``
    Half the bid-ask spread paid on entry and exit.
``slippage``
    Market impact and the gap between the assumed and achieved price.

Turnover is measured over the **risky sleeves only**. Cash is a residual, not a
tradable instrument: moving 30% from cash into QLD trades 30% of notional, and
counting the cash leg as well would double the cost.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from regime_monitor.config.schema import CostModelSpec
from regime_monitor.constants import Asset

BPS = 1e-4


@dataclass(frozen=True, slots=True)
class CostModel:
    """Linear cost as a fraction of traded notional."""

    commission_bps: float = 0.0
    spread_bps: float = 0.0
    slippage_bps: float = 0.0

    def __post_init__(self) -> None:
        for name in ("commission_bps", "spread_bps", "slippage_bps"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} cannot be negative")

    @classmethod
    def from_spec(cls, spec: CostModelSpec) -> Self:
        if not spec.is_resolved:
            raise ValueError(
                "cost_model is an unresolved research parameter (TASK-072); "
                "a backtest must state its costs explicitly"
            )
        return cls(
            commission_bps=float(spec.commission_bps),  # type: ignore[arg-type]
            spread_bps=float(spec.spread_bps),  # type: ignore[arg-type]
            slippage_bps=float(spec.slippage_bps),  # type: ignore[arg-type]
        )

    @classmethod
    def zero(cls) -> Self:
        """The cost-free comparison run ``BACKTEST_SPEC.md`` 17 also requires."""
        return cls()

    @property
    def total_bps(self) -> float:
        return self.commission_bps + self.spread_bps + self.slippage_bps

    @property
    def rate(self) -> float:
        """Cost per unit of traded notional."""
        return self.total_bps * BPS

    @property
    def is_free(self) -> bool:
        return self.total_bps == 0.0

    def cost_of(self, turnover: float) -> float:
        """Cost as a fraction of NAV for a given traded notional."""
        if turnover < 0:
            raise ValueError("turnover cannot be negative")
        return turnover * self.rate

    def describe(self) -> str:
        return (
            f"commission={self.commission_bps:g}bps, spread={self.spread_bps:g}bps, "
            f"slippage={self.slippage_bps:g}bps (total {self.total_bps:g}bps)"
        )


def turnover_between(
    current: Mapping[Asset, float], target: Mapping[Asset, float]
) -> float:
    """Traded notional, as a fraction of NAV, to move from one book to another.

    Cash is excluded: it is the residual of the risky sleeves, so including it
    would count every trade twice.
    """
    risky = (set(current) | set(target)) - {Asset.CASH}
    return sum(abs(target.get(asset, 0.0) - current.get(asset, 0.0)) for asset in risky)

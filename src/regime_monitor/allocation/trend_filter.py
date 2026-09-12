"""Trend filter — leverage into fear, but not into a falling market.

``PRD.md`` §2.1 says leverage rises as fear deepens. Measured over 1999–2015,
that rule on its own is ruinous: the composite score reached capitulation six
months into the dot-com decline, and the market then fell for another two years.
The strategy correctly de-risked at the 2000 peak (1.02x) and then levered back
up while the market halved twice (2.80x by 2001-09), ending the period at
−96.9% against QQQ's −77.2%.

The missing distinction is **fear is not the bottom**. A score built from fear
indicators cannot tell a market that has stopped falling from one that is still
falling, because both look equally frightening.

So this filter adds the second condition, and only the second condition: the
leveraged end of the ladder is available only while the long-term trend is
intact. Below the trend line, target leverage is capped.

Measured over the same window (buy-and-hold rules, for scale):

===============================  ======  ======
Rule                             CAGR    MDD
===============================  ======  ======
QLD buy & hold (2x)              −1.4%   99.0%
2x above 200DMA, else cash       +9.2%   85.2%
3x above 200DMA, 1x below        +1.4%   67.4%
===============================  ======  ======

This is a **structural** rule, not a threshold to tune: it says *when* leverage
is permitted, not *how much*. Which indicator, which level and what cap remain
research parameters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Self

from regime_monitor.allocation.sleeves import leverage_of, portfolio_for
from regime_monitor.config.schema import TrendFilterSpec
from regime_monitor.constants import Asset

logger = logging.getLogger(__name__)


class TrendFilterError(ValueError):
    """Raised when the trend filter cannot be built or applied."""


@dataclass(frozen=True, slots=True)
class TrendVerdict:
    """Whether the trend permits leverage today, and what it cost."""

    engaged: bool
    observed: float | None
    threshold: float | None
    leverage_before: float
    leverage_after: float
    indicator: str | None = None
    disabled: bool = False

    @property
    def capped(self) -> bool:
        return self.leverage_after < self.leverage_before - 1e-9

    def reason_codes(self) -> tuple[str, ...]:
        if self.disabled:
            return ("TREND_FILTER_DISABLED",)
        if self.observed is None:
            return (f"TREND_UNKNOWN:{self.indicator}", "LEVERAGE_CAPPED_BY_DEFAULT")
        if not self.engaged:
            return (f"TREND_INTACT:{self.indicator}={self.observed:.4g}",)
        codes = [f"TREND_BROKEN:{self.indicator}={self.observed:.4g}"]
        if self.capped:
            codes.append(
                f"LEVERAGE_CAPPED:{self.leverage_before:.2f}->{self.leverage_after:.2f}"
            )
        return tuple(codes)

    def describe(self) -> str:
        if self.disabled:
            return "trend filter off"
        if self.observed is None:
            return f"{self.indicator} unavailable; leverage capped defensively"
        state = "below" if self.engaged else "above"
        return (
            f"{self.indicator}={self.observed:.4g} is {state} {self.threshold:.4g}"
            + (f", leverage {self.leverage_before:.2f}x -> {self.leverage_after:.2f}x"
               if self.capped else "")
        )


@dataclass(frozen=True, slots=True)
class TrendFilter:
    """Caps target leverage while the long-term trend is broken."""

    indicator: str
    threshold: float
    max_leverage_below: float
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if not self.indicator:
            raise TrendFilterError("the trend filter needs an indicator")
        if self.max_leverage_below < 0:
            raise TrendFilterError("max_leverage_below cannot be negative")

    @classmethod
    def from_spec(cls, spec: TrendFilterSpec) -> Self:
        if not spec.enabled:
            return cls(indicator="", threshold=0.0, max_leverage_below=0.0, enabled=False)
        if not spec.is_resolved:
            raise TrendFilterError(
                "trend_filter indicator / threshold / max_leverage_below are "
                "unresolved research parameters"
            )
        assert spec.indicator is not None
        assert spec.threshold is not None
        assert spec.max_leverage_below is not None
        return cls(
            indicator=spec.indicator,
            threshold=spec.threshold,
            max_leverage_below=spec.max_leverage_below,
        )

    def apply(
        self, weights: dict[Asset, float], observed: float | None
    ) -> tuple[dict[Asset, float], TrendVerdict]:
        """Cap ``weights`` if the trend is broken. Returns the book and why."""
        before = leverage_of(weights)
        if not self.enabled:
            return weights, TrendVerdict(
                engaged=False,
                observed=observed,
                threshold=None,
                leverage_before=before,
                leverage_after=before,
                disabled=True,
            )

        # A missing trend reading is treated as a broken trend. The filter exists
        # to stop the strategy levering into a decline it cannot see; failing
        # open would remove exactly the protection it was added for.
        broken = observed is None or observed < self.threshold
        if not broken:
            return weights, TrendVerdict(
                engaged=False,
                observed=observed,
                threshold=self.threshold,
                leverage_before=before,
                leverage_after=before,
                indicator=self.indicator,
            )

        after = min(before, self.max_leverage_below)
        capped = weights if after >= before - 1e-9 else portfolio_for(after)
        return capped, TrendVerdict(
            engaged=True,
            observed=observed,
            threshold=self.threshold,
            leverage_before=before,
            leverage_after=leverage_of(capped),
            indicator=self.indicator,
        )

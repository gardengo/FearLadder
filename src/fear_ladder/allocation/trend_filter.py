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

import pandas as pd
from pandas import Series

from fear_ladder.allocation.sleeves import leverage_of, portfolio_for
from fear_ladder.config.schema import TrendFilterSpec
from fear_ladder.constants import Asset

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
    #: Level the trend must climb back above to release the cap. Equal to
    #: ``threshold`` means no hysteresis.
    reentry_threshold: float | None = None
    #: Indicator measuring how far the market has already fallen, and how deep
    #: that must be before the filter may engage. Both or neither.
    depth_indicator: str | None = None
    min_depth_to_engage: float | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if not self.indicator:
            raise TrendFilterError("the trend filter needs an indicator")
        if self.max_leverage_below < 0:
            raise TrendFilterError("max_leverage_below cannot be negative")
        below = self.reentry_threshold is not None and self.reentry_threshold < self.threshold
        if below:
            raise TrendFilterError(
                f"reentry_threshold {self.reentry_threshold} is below threshold "
                f"{self.threshold}; that inverts the band"
            )
        if (self.depth_indicator is None) != (self.min_depth_to_engage is None):
            raise TrendFilterError(
                "depth_indicator and min_depth_to_engage are meaningless apart"
            )

    @property
    def release_at(self) -> float:
        return self.threshold if self.reentry_threshold is None else self.reentry_threshold

    @property
    def gated_on_depth(self) -> bool:
        return self.depth_indicator is not None and self.min_depth_to_engage is not None

    def _deep_enough(self, depth: float | None) -> bool:
        """Whether the fall is already deep enough to permit engaging.

        An unreadable depth counts as deep, for the same reason an unreadable
        trend counts as broken: this is the defensive direction, and failing
        open would remove the protection the filter exists for.
        """
        if not self.gated_on_depth:
            return True
        if depth is None or pd.isna(depth):
            return True
        assert self.min_depth_to_engage is not None
        return float(depth) >= self.min_depth_to_engage

    def engaged_series(self, values: Series, depth: Series | None = None) -> Series:
        """Whether the cap is on, day by day, with hysteresis.

        A state machine rather than a comparison: engage when the trend falls
        below ``threshold``, release only once it climbs back above
        ``release_at``. Between the two the previous state persists, so a level
        that is merely brushed does not flip the book.

        ``depth`` carries ``depth_indicator`` over the same index. When the
        filter is gated on depth, engaging additionally requires the market to
        have already fallen ``min_depth_to_engage``. Only *engaging* is gated —
        once the cap is on, release is governed by the trend alone, so a
        recovering depth reading cannot lift the cap while price is still under
        the line.

        Strictly causal — each day's state depends only on that day's readings
        and the state carried from the day before.
        """
        if not self.enabled:
            return Series(False, index=values.index, dtype=bool)

        depths = self._aligned_depth(values, depth)
        state = False
        states: list[bool] = []
        for day, value in values.items():
            if pd.isna(value):
                # An unreadable trend is a broken trend: failing open would
                # remove exactly the protection this exists for.
                state = True
            elif state:
                state = value < self.release_at
            else:
                state = value < self.threshold and self._deep_enough(depths.get(day))
            states.append(state)
        return Series(states, index=values.index, dtype=bool, name="trend_broken")

    def _aligned_depth(
        self, values: Series, depth: Series | None
    ) -> dict[object, float | None]:
        if not self.gated_on_depth:
            return {}
        if depth is None:
            # The depth floor can only relax the filter, so losing the reading
            # has to fall back to the stricter, un-gated behaviour rather than
            # silently holding leverage through a decline.
            logger.warning(
                "trend filter is gated on %s but no depth series was supplied; "
                "engaging on the trend alone",
                self.depth_indicator,
            )
            return {}
        return depth.reindex(values.index).to_dict()

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
            reentry_threshold=spec.reentry_threshold,
            depth_indicator=spec.depth_indicator,
            min_depth_to_engage=spec.min_depth_to_engage,
        )

    def apply(
        self,
        weights: dict[Asset, float],
        observed: float | None,
        *,
        engaged: bool | None = None,
        depth_observed: float | None = None,
    ) -> tuple[dict[Asset, float], TrendVerdict]:
        """Cap ``weights`` if the trend is broken. Returns the book and why.

        ``engaged`` carries a state already resolved by :meth:`engaged_series`,
        which is how hysteresis reaches this per-day call. Without it the
        threshold is compared directly — against ``depth_observed`` too, when
        the filter is gated on depth — and the filter has no memory.
        """
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
        fresh = observed is None or (
            observed < self.threshold and self._deep_enough(depth_observed)
        )
        broken = engaged if engaged is not None else fresh
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

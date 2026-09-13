"""How much raw history the operational database has to keep.

The daily workflow commits ``data/fear_ladder.db`` back to the repository on
every trading day, and a SQLite file does not delta-compress well, so an
unbounded database turns into unbounded git history. Keeping a rolling window
bounds the blob committed each day.

The window cannot be chosen freely. Every indicator reads trailing data, and
pruning past the longest lookback would not shrink the database so much as
silently corrupt the next day's signal — the score would be computed from a
half-filled window and nothing would say so. :func:`required_trading_days`
derives that floor from the configuration rather than hard-coding it, so adding
a longer-lookback indicator moves the floor with it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from fear_ladder.config.schema import AppConfig, IndicatorSpec

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
#: Sources published once a week rather than once a trading day. Their windows
#: are counted in weekly observations, so they span five times as many days.
WEEKLY_SOURCES = frozenset({"AAII_SENTIMENT"})
#: Margin over the bare requirement. A window sized exactly to the longest
#: lookback leaves the oldest indicator with no burn-in at all.
SAFETY_FACTOR = 2.0


def _span_of(spec: IndicatorSpec) -> int:
    """Longest trailing span this indicator reads, in trading days."""
    window = spec.normalization.window or 0
    if spec.source in WEEKLY_SOURCES:
        window *= 5
    params = [value for value in (spec.params or {}).values() if isinstance(value, int)]
    return max([window, *params], default=0)


def required_trading_days(config: AppConfig) -> int:
    """Trailing days the enabled indicators need before a score is meaningful."""
    return max(
        (_span_of(spec) for spec in config.indicators.enabled_indicators.values()),
        default=0,
    )


def minimum_years(config: AppConfig) -> float:
    """The shortest retention window that still produces an honest signal."""
    return required_trading_days(config) * SAFETY_FACTOR / TRADING_DAYS_PER_YEAR


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """What a prune would do, decided before anything is deleted."""

    keep_years: float
    cutoff: date
    required_days: int
    minimum_years: float

    @property
    def safe(self) -> bool:
        return self.keep_years >= self.minimum_years

    def describe(self) -> str:
        verdict = "ok" if self.safe else "TOO SHORT"
        return (
            f"keep {self.keep_years:g}y (cutoff {self.cutoff}); indicators need "
            f"{self.required_days} trading days, so the floor is "
            f"{self.minimum_years:.2f}y — {verdict}"
        )


def plan(config: AppConfig, *, keep_years: float, as_of: date) -> RetentionPlan:
    """Work out the cutoff date and whether keeping that much is defensible."""
    if keep_years <= 0:
        raise ValueError(f"keep_years must be positive, got {keep_years}")
    return RetentionPlan(
        keep_years=keep_years,
        cutoff=as_of - timedelta(days=round(keep_years * 365.25)),
        required_days=required_trading_days(config),
        minimum_years=minimum_years(config),
    )

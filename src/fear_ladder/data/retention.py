"""How much raw history the operational database has to keep.

The daily workflow commits ``data/fear_ladder.db`` back to the repository on
every trading day, so a rolling window keeps the working tree and a fresh clone
small. Git itself copes better than expected — measured, a day's change adds
about 6 KB of pack — so the window is chosen for tidiness, not to rescue the
history from a size it could not survive.

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
#: Margin over the requirement, for the days a calendar window does not
#: deliver: holidays, a feed that skipped a session, a source that starts late.
#:
#: It was 2.0 while :func:`_span_of` took the *larger* of an indicator's own
#: lookback and its normalisation window, and the doubling was standing in for
#: the burn-in that formula left out. The span now counts the burn-in itself,
#: so the margin only has to cover missing days.
SAFETY_FACTOR = 1.25


def _span_of(spec: IndicatorSpec) -> int:
    """Trailing days this indicator needs before its score is the settled one.

    The two windows stack. A 12-month momentum needs 252 days to produce one
    value, and its score is that value's rank among the last 504 values — each
    of which needed its own 252 days. So the first day whose score no longer
    depends on where the history starts is day 252 + 504, not day 504.

    Taking the larger of the two said 520 for the shipped strategy (AAII's 104
    weekly observations) where the answer is 756, and it was measured: the same
    date scored differently from 700 days of history than from 1,300. The
    operational window (five years) was never short enough to show it, but the
    first weeks of ``scripts/backfill_history.py``'s 2024 replay were.

    Every integer parameter is counted, not just the largest, because they
    chain as well: a 21-day slope of a 200-day average reads 221 days back.
    That overstates a pair that overlaps (the 50- and 200-day averages of one
    ratio) by the shorter of the two, which errs towards keeping more.
    """
    window = spec.normalization.window or 0
    if spec.source in WEEKLY_SOURCES:
        window *= 5
    lookback = sum(
        value
        for value in (spec.params or {}).values()
        if isinstance(value, int) and not isinstance(value, bool)
    )
    return window + lookback


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

"""Normalization onto the 0-100 fear/greed axis (TASK-040, TASK-041).

``PRD.md`` 7 fixes the axis::

    0   = 극단적 공포 / Risk-Off
    100 = 극단적 탐욕 / Risk-On

``BACKTEST_SPEC.md`` 9 fixes how a raw value may reach it::

    금지: 전체 데이터의 평균/표준편차/분위수를 계산한 뒤 과거 전체에 적용

Every normalizer here is therefore **causal by construction**: the score at
``t`` is a function of observations up to and including ``t`` and nothing else.
The abstraction exists precisely so that a full-sample normalizer cannot be
added by accident — there is no place to put one.

Direction is applied last and uniformly: an indicator declared
``HIGHER_IS_FEAR`` (VIX, drawdown depth) has its score mirrored, so every
indicator speaks the same language before the weighted sum.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd
from pandas import Series

from fear_ladder.config.schema import NormalizationSpec
from fear_ladder.constants import SCORE_MAX, SCORE_MIN, IndicatorDirection


class NormalizationError(ValueError):
    """Raised when a series cannot be normalized as configured."""


def _mirror(scores: Series, direction: IndicatorDirection) -> Series:
    if direction is IndicatorDirection.HIGHER_IS_FEAR:
        return SCORE_MAX - scores
    return scores


@dataclass(frozen=True, slots=True)
class Normalizer(ABC):
    """Maps a raw indicator series onto 0-100 using past data only."""

    direction: IndicatorDirection = IndicatorDirection.HIGHER_IS_GREED

    @abstractmethod
    def _raw_scores(self, values: Series) -> Series:
        """Scores before direction is applied. Must be causal."""

    @property
    @abstractmethod
    def method(self) -> str: ...

    @property
    def window(self) -> int | None:
        return None

    def normalize(self, values: Series) -> Series:
        if values.empty:
            return values.astype("float64")
        numeric = pd.to_numeric(values, errors="coerce").astype("float64")
        scores = self._raw_scores(numeric)
        scores = _mirror(scores, self.direction)
        return scores.clip(lower=SCORE_MIN, upper=SCORE_MAX)


@dataclass(frozen=True, slots=True)
class RollingPercentileNormalizer(Normalizer):
    """Rank inside a trailing window, rescaled to 0-100.

    Robust to outliers and to a regime shift in the raw units, which is why it
    is the default for RSI, momentum, drawdown and VIX level.
    """

    window: int = 252  # type: ignore[assignment]
    min_periods: int | None = None

    def __post_init__(self) -> None:
        if self.window < 2:
            raise NormalizationError("rolling_percentile needs a window of at least 2")
        if self.min_periods is not None and not 1 <= self.min_periods <= self.window:
            raise NormalizationError("min_periods must be between 1 and window")

    @property
    def method(self) -> str:
        return "rolling_percentile"

    def _raw_scores(self, values: Series) -> Series:
        ranks = values.rolling(
            self.window, min_periods=self.min_periods or self.window
        ).rank(pct=True)
        return ranks * SCORE_MAX


@dataclass(frozen=True, slots=True)
class RollingZScoreNormalizer(Normalizer):
    """Trailing z-score, clipped and mapped linearly onto 0-100.

    Keeps the *size* of a move, unlike a percentile, at the cost of assuming the
    trailing distribution is roughly symmetric.
    """

    window: int = 252  # type: ignore[assignment]
    min_periods: int | None = None
    clip_sigma: float = 3.0

    def __post_init__(self) -> None:
        if self.window < 2:
            raise NormalizationError("rolling_zscore needs a window of at least 2")
        if self.clip_sigma <= 0:
            raise NormalizationError("clip_sigma must be positive")

    @property
    def method(self) -> str:
        return "rolling_zscore"

    def _raw_scores(self, values: Series) -> Series:
        periods = self.min_periods or self.window
        rolling = values.rolling(self.window, min_periods=periods)
        mean = rolling.mean()
        # ddof=1: the trailing window is a sample, not the population.
        std = rolling.std(ddof=1)
        z = (values - mean) / std
        # A flat window has zero dispersion; "no information" is the midpoint,
        # not an infinite score.
        z = z.where(std > 0, 0.0)
        z = z.clip(lower=-self.clip_sigma, upper=self.clip_sigma)
        return (z + self.clip_sigma) / (2 * self.clip_sigma) * SCORE_MAX


@dataclass(frozen=True, slots=True)
class LinearMappingNormalizer(Normalizer):
    """Fixed linear map from a known raw range onto 0-100.

    Only legitimate when the raw range is *definitional* rather than empirical:
    the CNN index is already published on 0-100, and a percentile is already
    bounded by 0..1. Using it on an open-ended series would be inventing a
    threshold, which ``CONTRIBUTING.md`` 10 forbids.
    """

    raw_at_score_min: float = 0.0
    raw_at_score_max: float = 100.0
    method_name: str = "fixed_mapping"

    def __post_init__(self) -> None:
        if self.raw_at_score_min == self.raw_at_score_max:
            raise NormalizationError("raw_at_score_min and raw_at_score_max must differ")

    @property
    def method(self) -> str:
        return self.method_name

    def _raw_scores(self, values: Series) -> Series:
        span = self.raw_at_score_max - self.raw_at_score_min
        return (values - self.raw_at_score_min) / span * SCORE_MAX


def build_normalizer(spec: NormalizationSpec, direction: IndicatorDirection) -> Normalizer:
    """Construct the normalizer a configuration entry describes."""
    match spec.method:
        case "rolling_percentile":
            if spec.window is None:
                raise NormalizationError(
                    "rolling_percentile window is an unresolved research parameter"
                )
            return RollingPercentileNormalizer(
                direction=direction, window=spec.window, min_periods=spec.min_periods
            )
        case "rolling_zscore":
            if spec.window is None:
                raise NormalizationError(
                    "rolling_zscore window is an unresolved research parameter"
                )
            return RollingZScoreNormalizer(
                direction=direction,
                window=spec.window,
                min_periods=spec.min_periods,
                clip_sigma=spec.clip_sigma or 3.0,
            )
        case "fixed_mapping" | "bounded":
            if spec.raw_at_score_min is None or spec.raw_at_score_max is None:
                raise NormalizationError(f"{spec.method} needs both raw_at_score_* bounds")
            return LinearMappingNormalizer(
                direction=direction,
                raw_at_score_min=spec.raw_at_score_min,
                raw_at_score_max=spec.raw_at_score_max,
                method_name=spec.method,
            )
        case unknown:  # pragma: no cover - the config schema already restricts this
            raise NormalizationError(f"unknown normalization method {unknown!r}")

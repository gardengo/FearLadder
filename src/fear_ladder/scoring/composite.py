"""Composite market score (TASK-042).

``BACKTEST_SPEC.md`` 10::

    Score_t = Σ(weight_i × normalized_indicator_i,t)
    weight_i >= 0,  Σweight = 1,  Score ∈ [0,100]

The one design decision worth stating: when an indicator is missing on a given
day, its weight is **redistributed across the indicators that are present**
rather than treated as a zero. A zero would silently read as "extreme fear",
which is the most dangerous possible default in a strategy that adds leverage
into fear. The redistribution is recorded in the breakdown, and a day with too
little coverage produces no score at all.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from pandas import DataFrame, Series

from fear_ladder.config.schema import AppConfig, IndicatorsConfig, ScoreSpec
from fear_ladder.constants import SCORE_MAX, SCORE_MIN, DataQualityStatus
from fear_ladder.data.models import IndicatorScore
from fear_ladder.indicators.engine import IndicatorResult
from fear_ladder.scoring.normalizers import Normalizer, build_normalizer

logger = logging.getLogger(__name__)

#: Minimum share of total configured weight that must be present for a day to
#: get a score at all. Below this the day is reported as uncomputable rather
#: than scored from a fragment of the indicator set.
DEFAULT_MIN_WEIGHT_COVERAGE = 0.6


class ScoringError(ValueError):
    """Raised when a composite score cannot be computed as configured."""


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Why the composite score is what it is (``CLAUDE_CODE_INITIAL_PROMPT.md`` 12)."""

    observation_date: date
    composite: float | None
    contributions: dict[str, float] = field(default_factory=dict)
    indicator_scores: dict[str, float] = field(default_factory=dict)
    effective_weights: dict[str, float] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    weight_coverage: float = 0.0

    @property
    def is_computable(self) -> bool:
        return self.composite is not None

    def top_contributors(self, count: int = 3) -> list[tuple[str, float]]:
        """Largest absolute deviations from the neutral midpoint."""
        midpoint = (SCORE_MIN + SCORE_MAX) / 2
        ranked = sorted(
            self.indicator_scores.items(),
            key=lambda item: abs(item[1] - midpoint),
            reverse=True,
        )
        return ranked[:count]


@dataclass(frozen=True, slots=True)
class ScoreEngine:
    """Normalizes indicators and combines them into the market score."""

    indicators: IndicatorsConfig
    score: ScoreSpec
    min_weight_coverage: float = DEFAULT_MIN_WEIGHT_COVERAGE

    @classmethod
    def from_config(cls, config: AppConfig, **kwargs: object) -> ScoreEngine:
        return cls(indicators=config.indicators, score=config.strategy.score, **kwargs)  # type: ignore[arg-type]

    # -- weights -----------------------------------------------------------
    @property
    def weights(self) -> dict[str, float]:
        if self.score.weights is None:
            raise ScoringError(
                "score.weights is an unresolved research parameter (TASK-081); "
                "a composite score cannot be computed yet"
            )
        unknown = set(self.score.weights) - set(self.indicators.enabled_indicators)
        if unknown:
            raise ScoringError(
                f"score.weights names disabled/unknown indicators: {sorted(unknown)}"
            )
        return dict(self.score.weights)

    def normalizers(self) -> dict[str, Normalizer]:
        return {
            name: build_normalizer(spec.normalization, spec.direction)
            for name, spec in self.indicators.enabled_indicators.items()
            if name in self.weights
        }

    # -- normalization -----------------------------------------------------
    def normalize(self, results: Mapping[str, IndicatorResult]) -> DataFrame:
        """0-100 scores, one column per weighted indicator."""
        normalizers = self.normalizers()
        columns: dict[str, Series] = {}
        for name, normalizer in normalizers.items():
            result = results.get(name)
            if result is None:
                logger.warning("indicator %s was not computed; it will count as missing", name)
                continue
            columns[name] = normalizer.normalize(result.values)
        if not columns:
            return DataFrame()
        return DataFrame(columns).sort_index()

    # -- composition -------------------------------------------------------
    def composite_series(self, scores: DataFrame) -> Series:
        """Weighted mean over whichever indicators are present each day."""
        if scores.empty:
            return Series(dtype="float64")
        weights = self.weights
        columns = [name for name in scores.columns if name in weights]
        if not columns:
            raise ScoringError("none of the scored indicators carry a weight")

        matrix = scores[columns]
        weight_vector = Series({name: weights[name] for name in columns}, dtype="float64")
        present = matrix.notna()

        # Renormalise over present indicators. A missing indicator must not be
        # read as a zero score, i.e. as maximum fear.
        available_weight = present.mul(weight_vector, axis=1).sum(axis=1)
        weighted_sum = matrix.fillna(0.0).mul(weight_vector, axis=1).sum(axis=1)

        total_weight = float(weight_vector.sum())
        coverage = available_weight / total_weight
        composite = weighted_sum / available_weight.replace(0.0, pd.NA)
        composite = composite.where(coverage >= self.min_weight_coverage)
        return composite.clip(lower=SCORE_MIN, upper=SCORE_MAX).rename("composite_score")

    def breakdown(self, scores: DataFrame, day: date) -> ScoreBreakdown:
        """Explain one day's score."""
        weights = self.weights
        if scores.empty or day not in scores.index:
            return ScoreBreakdown(
                observation_date=day, composite=None, missing=tuple(sorted(weights))
            )

        row = scores.loc[day]
        present = {
            name: float(row[name])
            for name in scores.columns
            if name in weights and pd.notna(row.get(name))
        }
        missing = tuple(sorted(set(weights) - set(present)))

        total_weight = sum(weights.values())
        available = sum(weights[name] for name in present)
        coverage = available / total_weight if total_weight else 0.0

        if not present or coverage < self.min_weight_coverage:
            return ScoreBreakdown(
                observation_date=day,
                composite=None,
                indicator_scores=present,
                missing=missing,
                weight_coverage=coverage,
            )

        effective = {name: weights[name] / available for name in present}
        contributions = {name: effective[name] * present[name] for name in present}
        composite = min(max(sum(contributions.values()), SCORE_MIN), SCORE_MAX)
        return ScoreBreakdown(
            observation_date=day,
            composite=composite,
            contributions=contributions,
            indicator_scores=present,
            effective_weights=effective,
            missing=missing,
            weight_coverage=coverage,
        )

    def to_domain_scores(self, scores: DataFrame, day: date) -> list[IndicatorScore]:
        """Persistable per-indicator scores for one day."""
        if scores.empty or day not in scores.index:
            return []
        row = scores.loc[day]
        specs = self.indicators.enabled_indicators
        domain: list[IndicatorScore] = []
        for name in scores.columns:
            spec = specs[name]
            value = row.get(name)
            domain.append(
                IndicatorScore(
                    indicator_name=name,
                    observation_date=day,
                    score=None if pd.isna(value) else float(value),
                    normalization_method=spec.normalization.method,
                    normalization_window=spec.normalization.window,
                    quality_status=(
                        DataQualityStatus.MISSING if pd.isna(value) else DataQualityStatus.OK
                    ),
                )
            )
        return domain

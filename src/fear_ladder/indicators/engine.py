"""Indicator engine — turns configuration plus input series into indicator values.

Responsibilities are deliberately narrow: resolve each configured indicator to a
registered computation, feed it the right source series, and hand back raw
values. Normalization onto the 0-100 axis happens later, in
:mod:`fear_ladder.scoring`, because the normalization window is a strategy
parameter while the indicator lengths are definitional.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import pandas as pd
from pandas import DataFrame, Series

from fear_ladder.config.schema import IndicatorsConfig, IndicatorSpec
from fear_ladder.constants import DataQualityStatus
from fear_ladder.data.models import IndicatorValue
from fear_ladder.indicators.computations import IndicatorError, compute

logger = logging.getLogger(__name__)


class MissingSourceError(IndicatorError):
    """A configured indicator has no input series to read."""


@dataclass(frozen=True, slots=True)
class IndicatorResult:
    """One indicator's full history plus how it was produced."""

    name: str
    spec: IndicatorSpec
    values: Series

    @property
    def latest(self) -> float | None:
        usable = self.values.dropna()
        return None if usable.empty else float(usable.iloc[-1])

    def value_on(self, day: date) -> float | None:
        if day not in self.values.index:
            return None
        value = self.values.loc[day]
        return None if pd.isna(value) else float(value)

    def to_domain(self, day: date) -> IndicatorValue:
        value = self.value_on(day)
        return IndicatorValue(
            indicator_name=self.name,
            observation_date=day,
            value=value,
            source_symbol=self.spec.source,
            params=dict(self.spec.params),
            quality_status=(
                DataQualityStatus.OK if value is not None else DataQualityStatus.MISSING
            ),
        )


@dataclass(frozen=True, slots=True)
class IndicatorEngine:
    """Computes every enabled indicator from a set of input series."""

    config: IndicatorsConfig

    def compute_all(
        self,
        sources: Mapping[str, Series],
        *,
        strict: bool = False,
    ) -> dict[str, IndicatorResult]:
        """Compute every enabled indicator.

        ``strict=False`` lets an optional indicator fail without taking the
        whole pipeline down — a missing AAII file should cost one indicator, not
        the day's signal. Mandatory indicators always raise, because a wrong
        signal is worse than no signal (``BACKTEST_SPEC.md`` 7).
        """
        results: dict[str, IndicatorResult] = {}
        for name, spec in self.config.enabled_indicators.items():
            try:
                results[name] = self.compute_one(name, spec, sources)
            except IndicatorError as exc:
                if strict or spec.mandatory:
                    raise
                logger.warning("skipping optional indicator %s: %s", name, exc)
        return results

    def compute_one(
        self,
        name: str,
        spec: IndicatorSpec,
        sources: Mapping[str, Series],
    ) -> IndicatorResult:
        series = sources.get(spec.source)
        if series is None:
            raise MissingSourceError(
                f"indicator {name!r} needs source {spec.source!r}, which was not provided "
                f"(available: {sorted(sources)})"
            )
        if series.empty:
            raise MissingSourceError(f"indicator {name!r}: source {spec.source!r} is empty")

        params = {key: value for key, value in spec.params.items() if value is not None}
        # A deliberate null (drawdown's expanding peak) must still reach the
        # computation, while an unresolved research parameter must not.
        for key, value in spec.params.items():
            if value is None and key not in spec.research_params:
                params[key] = None
        if spec.unresolved_params:
            raise IndicatorError(
                f"indicator {name!r} has unresolved research parameters: "
                f"{list(spec.unresolved_params)}"
            )

        try:
            values = compute(spec.compute, series.sort_index(), params)
        except IndicatorError:
            raise
        except Exception as exc:
            raise IndicatorError(f"indicator {name!r} failed: {exc}") from exc

        values.name = name
        return IndicatorResult(name=name, spec=spec, values=values)

    def to_frame(self, results: Mapping[str, IndicatorResult]) -> DataFrame:
        """Wide frame of raw indicator values, one column per indicator."""
        if not results:
            return DataFrame()
        frame = DataFrame({name: result.values for name, result in results.items()})
        return frame.sort_index()


def sources_from_frame(frame: DataFrame) -> dict[str, Series]:
    """Split a wide observation frame into per-symbol series.

    Each column is dropped to its own non-null observations: a weekly series
    like AAII must not acquire daily rows full of ``NaN`` just because it shares
    an index with the price series.
    """
    return {str(column): frame[column].dropna() for column in frame.columns}

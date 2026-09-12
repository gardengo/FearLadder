"""Builds providers from ``config/data_sources.yaml``.

Keeping the wiring in one place is what lets the pipeline honour
``CLAUDE_CODE_INITIAL_PROMPT.md`` 6.5: if a configured provider fails, the
answer is a recorded failure, never an ad-hoc substitute source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from regime_monitor import paths
from regime_monitor.config.schema import DataSourcesConfig, SeriesSourceSpec
from regime_monitor.data.collectors.base import PriceProvider, SeriesProvider
from regime_monitor.data.collectors.breadth import UnavailableBreadthProvider
from regime_monitor.data.collectors.cnn import CnnFearGreedProvider
from regime_monitor.data.collectors.fdr import (
    FinanceDataReaderPriceProvider,
    FinanceDataReaderSeriesProvider,
)
from regime_monitor.data.collectors.files import AaiiSentimentProvider, CsvSeriesProvider

logger = logging.getLogger(__name__)

REFERENCE_DIR = paths.DATA_DIR / "reference"


class UnknownProviderError(ValueError):
    """The configuration names a provider this build does not implement."""


@dataclass(frozen=True, slots=True)
class ProviderRegistry:
    """Resolved providers for one configuration."""

    price: PriceProvider
    series: dict[str, SeriesProvider]
    disabled: dict[str, str]

    def series_provider(self, name: str) -> SeriesProvider:
        if name in self.disabled:
            raise UnknownProviderError(f"{name} is disabled: {self.disabled[name]}")
        try:
            return self.series[name]
        except KeyError as exc:
            raise UnknownProviderError(f"no provider configured for series {name!r}") from exc


def build_registry(
    config: DataSourcesConfig, *, reference_dir: Path | None = None
) -> ProviderRegistry:
    reference = reference_dir or REFERENCE_DIR
    price = FinanceDataReaderPriceProvider(
        underlying_sources=dict.fromkeys(
            config.price.symbols, "FinanceDataReader US equity feed"
        )
    )

    series: dict[str, SeriesProvider] = {}
    disabled: dict[str, str] = {}
    for name, spec in config.series.items():
        if not spec.enabled:
            disabled[name] = spec.disabled_reason or "disabled"
            continue
        series[name] = _build_series_provider(name, spec, reference)
    return ProviderRegistry(price=price, series=series, disabled=disabled)


def _build_series_provider(
    name: str, spec: SeriesSourceSpec, reference: Path
) -> SeriesProvider:
    match spec.provider:
        case "finance_datareader":
            return FinanceDataReaderSeriesProvider(
                symbol=spec.series_id or name,
                underlying_source=spec.underlying_source,
            )
        case "cnn":
            return CnnFearGreedProvider()
        case "aaii":
            return AaiiSentimentProvider(
                path=reference / "aaii" / "sentiment.csv",
                underlying_source=spec.underlying_source,
            )
        case "csv":
            return CsvSeriesProvider(
                path=reference / f"{name.lower()}.csv",
                underlying_source=spec.underlying_source,
                quality_status=spec.quality_status,
            )
        case "none":
            return UnavailableBreadthProvider()
        case unknown:
            raise UnknownProviderError(
                f"series {name!r} names provider {unknown!r}, which is not implemented"
            )


def historical_cnn_provider(reference_dir: Path | None = None) -> CsvSeriesProvider:
    """The reconstructed CNN history used for backtests (``PRD.md`` 6.5).

    Separate from the live provider on purpose, and REVIEW-grade by
    construction, so a backtest cannot present reconstructed sentiment as if CNN
    had published it.
    """
    reference = reference_dir or REFERENCE_DIR
    return CsvSeriesProvider(
        path=reference / "cnn" / "fear_greed_history.csv",
        underlying_source="CNN Fear & Greed — reconstructed secondary dataset",
        source_ref="reconstructed",
    )

"""Builds providers from ``config/data_sources.yaml``.

Keeping the wiring in one place is what lets the pipeline honour
``CONTRIBUTING.md`` 6.5: if a configured provider fails, the
answer is a recorded failure, never an ad-hoc substitute source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fear_ladder import paths
from fear_ladder.config.schema import DataSourcesConfig, SeriesSourceSpec
from fear_ladder.data.collectors.base import PriceProvider, SeriesProvider
from fear_ladder.data.collectors.breadth import UnavailableBreadthProvider
from fear_ladder.data.collectors.cnn import CnnFearGreedProvider
from fear_ladder.data.collectors.fdr import (
    FinanceDataReaderPriceProvider,
    FinanceDataReaderSeriesProvider,
)
from fear_ladder.data.collectors.files import (
    AaiiSentimentProvider,
    CompositeSeriesProvider,
    CsvSeriesProvider,
)
from fear_ladder.data.collectors.synthetic import (
    SplicedIndexProvider,
    SyntheticLeveragedProvider,
)

if TYPE_CHECKING:  # pandas is imported lazily below, inside the one function
    from pandas import Series  # that needs it, so importing it here would undo that

logger = logging.getLogger(__name__)


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
    reference = reference_dir or paths.REFERENCE_DIR
    price: PriceProvider = FinanceDataReaderPriceProvider(
        underlying_sources=dict.fromkeys(
            config.price.symbols, "FinanceDataReader US equity feed"
        )
    )
    if config.price.reconstruction.enabled:
        price = _wrap_with_reconstruction(price, config)

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
            # CNN's endpoint serves about a year. For anything longer the
            # reconstructed file has to carry the history, with the live
            # endpoint layered on top for the most recent days (PRD.md 6.5).
            history = reference / "cnn" / "fear_greed_history.csv"
            live = CnnFearGreedProvider()
            if not history.is_file():
                logger.warning(
                    "no CNN history at %s; only the last ~year is available. "
                    "Run scripts/fetch_reference.py.",
                    history,
                )
                return live
            return CompositeSeriesProvider(
                history=CsvSeriesProvider(
                    path=history,
                    underlying_source=(
                        "CNN Fear & Greed - reconstructed secondary dataset"
                    ),
                    source_ref="reconstructed",
                ),
                live=live,
            )
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


def _wrap_with_reconstruction(
    price: PriceProvider, config: DataSourcesConfig
) -> PriceProvider:
    """Layer the two reconstructions in dependency order.

    The splice runs first so the leveraged reconstruction has a QQQ history long
    enough to build on; wrapping them the other way round would leave the
    synthetic sleeves with nothing to extend.
    """
    spec = config.price.reconstruction
    spliced = SplicedIndexProvider(
        base=price,
        index_provider=FinanceDataReaderSeriesProvider(
            symbol=spec.index_symbol,
            underlying_source="FRED Nasdaq-100 index",
            column=None,
        ),
        index_symbol=spec.index_symbol,
    )
    return SyntheticLeveragedProvider(
        base=spliced,
        leverages={
            symbol: symbol_spec.leverage
            for symbol, symbol_spec in config.price.symbols.items()
            if symbol_spec.leverage > 1.0
        },
        financing_rates=_financing_rates(spec.financing_symbol),
    )


def _financing_rates(symbol: str | None) -> Series | None:
    """Fetch the borrowing-cost series, or ``None`` if it is unavailable.

    A missing rate does not stop the reconstruction; it makes it optimistic, and
    :class:`SyntheticLeveragedProvider` says so loudly in the log.
    """
    if not symbol:
        return None
    try:
        import FinanceDataReader as fdr  # noqa: N813

        frame = fdr.DataReader(symbol, "1985-01-01")
    except Exception as exc:
        logger.warning("financing rate %s unavailable (%s); reconstruction will "
                       "ignore borrowing cost", symbol, exc)
        return None

    import pandas as pd

    series = frame.iloc[:, 0].dropna()
    series.index = pd.to_datetime(series.index).normalize()
    return series


def historical_cnn_provider(reference_dir: Path | None = None) -> CsvSeriesProvider:
    """The reconstructed CNN history used for backtests (``PRD.md`` 6.5).

    Separate from the live provider on purpose, and REVIEW-grade by
    construction, so a backtest cannot present reconstructed sentiment as if CNN
    had published it.
    """
    reference = reference_dir or paths.REFERENCE_DIR
    return CsvSeriesProvider(
        path=reference / "cnn" / "fear_greed_history.csv",
        underlying_source="CNN Fear & Greed — reconstructed secondary dataset",
        source_ref="reconstructed",
    )

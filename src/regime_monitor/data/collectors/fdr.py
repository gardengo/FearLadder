"""FinanceDataReader adapters (TASK-027, TASK-021, TASK-023).

FinanceDataReader is this project's *collection interface* for US prices. It is
explicitly not treated as the originating data source, so every observation also
records the underlying feed the configuration declares (``PRD.md`` 6.5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from pandas import DataFrame

from regime_monitor.data.collectors.base import (
    CollectorError,
    DataUnavailableError,
    SourceDescription,
    standardize_prices,
    standardize_series,
)

logger = logging.getLogger(__name__)

LIBRARY_NAME = "FinanceDataReader"


@lru_cache(maxsize=1)
def library_version() -> str:
    """Pin the exact FDR build into provenance (``BACKTEST_SPEC.md`` 5.5)."""
    try:
        import FinanceDataReader as fdr  # noqa: N813
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise CollectorError("FinanceDataReader is not installed") from exc
    return str(getattr(fdr, "__version__", "unknown"))


def _read(symbol: str, start: date, end: date) -> DataFrame:
    import FinanceDataReader as fdr  # noqa: N813

    if start > end:
        raise ValueError(f"{symbol}: start {start} is after end {end}")
    try:
        frame = fdr.DataReader(symbol, start.isoformat(), end.isoformat())
    except Exception as exc:
        raise CollectorError(f"FinanceDataReader failed for {symbol}: {exc}") from exc
    if frame is None or frame.empty:
        raise DataUnavailableError(f"FinanceDataReader returned nothing for {symbol}")
    return frame


@dataclass(frozen=True, slots=True)
class FinanceDataReaderPriceProvider:
    """Daily OHLCV for US ETFs (QQQ / QLD / TQQQ).

    ``underlying_sources`` maps a symbol to the feed FDR reads behind the
    scenes, as declared in ``config/data_sources.yaml``. It is configuration
    rather than introspection because FDR does not report it.
    """

    underlying_sources: dict[str, str]
    default_underlying_source: str = "FinanceDataReader US equity feed"

    def fetch(self, symbol: str, start: date, end: date) -> DataFrame:
        return standardize_prices(_read(symbol, start, end), symbol=symbol)

    def describe(self, symbol: str) -> SourceDescription:
        return SourceDescription(
            provider_library=LIBRARY_NAME,
            provider_library_version=library_version(),
            underlying_source=self.underlying_sources.get(
                symbol, self.default_underlying_source
            ),
            source_ref=f"fdr:{symbol}",
        )


@dataclass(frozen=True, slots=True)
class FinanceDataReaderSeriesProvider:
    """A scalar series read through FDR — VIX by default (TASK-023).

    ``PRD.md`` 6.5 wants VIX cross-checked against Cboe official history and
    FRED VIXCLS. Both are reachable through this same adapter by pointing
    ``symbol`` at ``VIX`` or ``FRED:VIXCLS``, which is why the underlying source
    is carried explicitly instead of being inferred.
    """

    symbol: str
    underlying_source: str
    column: str | None = "close"

    def fetch(self, start: date, end: date) -> DataFrame:
        frame = _read(self.symbol, start, end)
        column = self._resolve_column(frame)
        return standardize_series(frame, name=self.symbol, column=column)

    def _resolve_column(self, frame: DataFrame) -> str | None:
        if frame.shape[1] == 1:
            # FRED-style series: one unnamed-by-convention column.
            return str(frame.columns[0])
        if self.column is None:
            return None
        for candidate in frame.columns:
            if str(candidate).strip().lower() == self.column:
                return str(candidate)
        raise CollectorError(
            f"{self.symbol}: column {self.column!r} not in {list(frame.columns)}"
        )

    def describe(self) -> SourceDescription:
        return SourceDescription(
            provider_library=LIBRARY_NAME,
            provider_library_version=library_version(),
            underlying_source=self.underlying_source,
            source_ref=f"fdr:{self.symbol}",
        )

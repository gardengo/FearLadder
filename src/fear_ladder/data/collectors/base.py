"""Collection ports and shared plumbing (TASK-021 .. TASK-027).

``ARCHITECTURE.md`` 3.5 fixes the shape of the price port::

    class PriceProvider(Protocol):
        def fetch(self, symbol, start, end) -> DataFrame

and requires four provenance fields on everything collected. A provider
therefore describes itself (:class:`SourceDescription`) separately from the data
it returns, and :class:`Collector` turns the two into domain observations.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Protocol, TypeVar, runtime_checkable
from zoneinfo import ZoneInfo

import pandas as pd
from pandas import DataFrame

from fear_ladder.constants import DataQualityStatus
from fear_ladder.data.models import MarketObservation, Provenance

logger = logging.getLogger(__name__)

#: Exchange whose close defines when a US daily value becomes knowable.
EXCHANGE_TZ = ZoneInfo("America/New_York")
MARKET_CLOSE = dtime(16, 0)

#: Canonical OHLCV column names every price provider must produce.
PRICE_COLUMNS = ("open", "high", "low", "close", "adj_close", "volume")
#: Canonical column for a scalar series.
VALUE_COLUMN = "value"


class CollectorError(RuntimeError):
    """A collection attempt failed."""


class DataUnavailableError(CollectorError):
    """The source responded, but has no data for the requested window."""


@dataclass(frozen=True, slots=True)
class SourceDescription:
    """Who collected the data and where it actually came from.

    ``PRD.md`` 6.5: "수집 라이브러리와 데이터 원천을 동일한 개념으로 취급하지
    않는다" — the library and the source are two different facts.
    """

    provider_library: str
    provider_library_version: str
    underlying_source: str
    source_ref: str | None = None

    def to_provenance(self, retrieved_at: datetime) -> Provenance:
        return Provenance(
            provider_library=self.provider_library,
            provider_library_version=self.provider_library_version,
            underlying_source=self.underlying_source,
            retrieved_at=retrieved_at,
            source_ref=self.source_ref,
        )


# ------------------------------------------------------------------- timing


def market_close_utc(day: date) -> datetime:
    """16:00 America/New_York on ``day``, in UTC.

    DST-aware on purpose: the close is 21:00 UTC in winter and 20:00 UTC in
    summer, and a fixed offset would quietly shift availability by an hour.
    """
    return datetime.combine(day, MARKET_CLOSE, tzinfo=EXCHANGE_TZ).astimezone(UTC)


def availability_for(observation_date: date, *, lag_days: int = 0) -> datetime:
    """When a value observed on ``observation_date`` may first be used.

    ``lag_days`` models publication delay: AAII's weekly survey describes a week
    ending Wednesday but is only published later, and using it earlier would be
    the availability leak ``BACKTEST_SPEC.md`` 24.6 tests for.
    """
    if lag_days < 0:
        raise ValueError("lag_days cannot be negative")
    return market_close_utc(observation_date + timedelta(days=lag_days))


# -------------------------------------------------------------------- retry

T = TypeVar("T")


def with_retry(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    description: str = "operation",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry a flaky network call with linear backoff.

    Deliberately does not fall back to a different provider:
    ``CLAUDE_CODE_INITIAL_PROMPT.md`` 6.5 forbids inventing a substitute source
    when the configured one fails.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except DataUnavailableError:
            raise
        except Exception as exc:
            last = exc
            logger.warning(
                "%s failed (attempt %d/%d): %s", description, attempt, attempts, exc
            )
            if attempt < attempts:
                sleep(backoff_seconds * attempt)
    raise CollectorError(f"{description} failed after {attempts} attempts: {last}") from last


# ----------------------------------------------------------------- the ports


@runtime_checkable
class PriceProvider(Protocol):
    """Daily OHLCV for one symbol (``ARCHITECTURE.md`` 3.5)."""

    def fetch(self, symbol: str, start: date, end: date) -> DataFrame:
        """Standardized OHLCV indexed by ``date``, ascending, no duplicates."""
        ...

    def describe(self, symbol: str) -> SourceDescription: ...


@runtime_checkable
class SeriesProvider(Protocol):
    """A daily or weekly scalar series (VIX, CNN F&G, AAII)."""

    def fetch(self, start: date, end: date) -> DataFrame:
        """Single ``value`` column indexed by ``date``, ascending."""
        ...

    def describe(self) -> SourceDescription: ...


# -------------------------------------------------------------- standardizing


def standardize_prices(frame: DataFrame, *, symbol: str) -> DataFrame:
    """Coerce a provider frame into the canonical OHLCV shape.

    Raises rather than guessing: a missing close means the symbol cannot be
    used, and silently inventing one is how look-ahead and bad signals start.
    """
    if frame is None or frame.empty:
        raise DataUnavailableError(f"{symbol}: provider returned no rows")

    result = frame.copy()
    result.columns = [str(column).strip().lower().replace(" ", "_") for column in result.columns]
    if "close" not in result.columns:
        raise CollectorError(f"{symbol}: provider frame has no close column {list(frame.columns)}")

    for column in PRICE_COLUMNS:
        if column not in result.columns:
            result[column] = pd.NA
    result = result[list(PRICE_COLUMNS)]
    result = _index_to_dates(result, symbol=symbol)

    result = result.dropna(subset=["close"])
    if result.empty:
        raise DataUnavailableError(f"{symbol}: every row had a missing close")
    return result


def standardize_series(frame: DataFrame, *, name: str, column: str | None = None) -> DataFrame:
    """Coerce a provider frame into a single ``value`` column."""
    if frame is None or frame.empty:
        raise DataUnavailableError(f"{name}: provider returned no rows")

    result = frame.copy()
    if column is None:
        if result.shape[1] != 1:
            raise CollectorError(
                f"{name}: expected one column, got {list(result.columns)}; name one explicitly"
            )
        column = str(result.columns[0])
    if column not in result.columns:
        raise CollectorError(f"{name}: column {column!r} missing from {list(result.columns)}")

    result = result[[column]].rename(columns={column: VALUE_COLUMN})
    result = _index_to_dates(result, symbol=name)
    result = result.dropna(subset=[VALUE_COLUMN])
    if result.empty:
        raise DataUnavailableError(f"{name}: every row had a missing value")
    return result


def _index_to_dates(frame: DataFrame, *, symbol: str) -> DataFrame:
    index = pd.to_datetime(frame.index, errors="coerce")
    if index.isna().any():
        raise CollectorError(f"{symbol}: provider frame has unparseable dates in its index")
    result = frame.copy()
    result.index = pd.Index([value.date() for value in index], name="observation_date")
    result = result[~result.index.duplicated(keep="last")]
    return result.sort_index()


# ----------------------------------------------------------------- collectors


@dataclass(frozen=True, slots=True)
class Collector:
    """Turns a provider's frame into domain observations with provenance.

    Collection and interpretation stay separate so the provider can be swapped
    (or faked in tests) without touching availability or provenance rules.
    """

    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC)

    def price_observations(
        self,
        provider: PriceProvider,
        symbol: str,
        start: date,
        end: date,
        *,
        attempts: int = 3,
        backoff_seconds: float = 2.0,
        quality_status: DataQualityStatus = DataQualityStatus.OK,
    ) -> list[MarketObservation]:
        frame = standardize_prices(
            with_retry(
                lambda: provider.fetch(symbol, start, end),
                attempts=attempts,
                backoff_seconds=backoff_seconds,
                description=f"fetch {symbol}",
            ),
            symbol=symbol,
        )
        provenance = provider.describe(symbol).to_provenance(self.clock())
        return [
            MarketObservation(
                symbol=symbol,
                observation_date=day,
                availability_datetime=availability_for(day),
                provenance=provenance,
                open=_opt(row.open),
                high=_opt(row.high),
                low=_opt(row.low),
                close=float(row.close),
                adj_close=_opt(row.adj_close),
                volume=_opt(row.volume),
                quality_status=quality_status,
            )
            for day, row in zip(frame.index, frame.itertuples(index=False), strict=True)
        ]

    def series_observations(
        self,
        provider: SeriesProvider,
        name: str,
        start: date,
        end: date,
        *,
        availability_lag_days: int = 0,
        attempts: int = 3,
        backoff_seconds: float = 2.0,
        quality_status: DataQualityStatus = DataQualityStatus.OK,
    ) -> list[MarketObservation]:
        frame = standardize_series(
            with_retry(
                lambda: provider.fetch(start, end),
                attempts=attempts,
                backoff_seconds=backoff_seconds,
                description=f"fetch {name}",
            ),
            name=name,
            column=None,
        )
        provenance = provider.describe().to_provenance(self.clock())
        return [
            MarketObservation(
                symbol=name,
                observation_date=day,
                availability_datetime=availability_for(day, lag_days=availability_lag_days),
                provenance=provenance,
                value=float(value),
                quality_status=quality_status,
            )
            for day, value in frame[VALUE_COLUMN].items()
        ]


def _opt(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)  # type: ignore[arg-type]

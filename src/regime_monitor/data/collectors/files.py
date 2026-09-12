"""File-backed providers for sources without a usable public API (TASK-022, TASK-024).

Two inputs cannot be fetched programmatically in a way this project is willing
to depend on:

* **CNN Fear & Greed history.** The live endpoint serves roughly one year, so a
  long backtest needs a reconstructed secondary dataset (``PRD.md`` 6.5).
* **AAII weekly sentiment.** The official survey history is distributed as a
  spreadsheet download for members, not as an open API.

Both therefore arrive as files the operator places under ``data/reference/``.
Each is stamped with its own ``quality_status`` and source string, so a
reconstructed series is never mistaken for an official one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from pandas import DataFrame

from regime_monitor.constants import DataQualityStatus
from regime_monitor.data.collectors.base import (
    CollectorError,
    DataUnavailableError,
    SourceDescription,
    standardize_series,
)

logger = logging.getLogger(__name__)


def _read_table(path: Path) -> DataFrame:
    if not path.is_file():
        raise DataUnavailableError(
            f"reference dataset not found: {path}. "
            "See docs/operations.md for how to place it."
        )
    suffix = path.suffix.lower()
    try:
        if suffix in (".csv", ".txt"):
            return pd.read_csv(path)
        if suffix in (".xls", ".xlsx"):
            return pd.read_excel(path)
        if suffix in (".parquet", ".pq"):
            return pd.read_parquet(path)
    except Exception as exc:
        raise CollectorError(f"could not read {path}: {exc}") from exc
    raise CollectorError(f"unsupported reference file type: {path.suffix!r}")


@dataclass(frozen=True, slots=True)
class CsvSeriesProvider:
    """A scalar series read from a local file.

    ``quality_status`` is part of the provider's identity rather than a
    parameter of one call: a reconstructed dataset is REVIEW-grade for its whole
    life, and callers should not be able to talk themselves out of that.
    """

    path: Path
    underlying_source: str
    date_column: str = "date"
    value_column: str = "value"
    quality_status: DataQualityStatus = DataQualityStatus.REVIEW
    source_ref: str | None = None

    def fetch(self, start: date, end: date) -> DataFrame:
        raw = _read_table(self.path)
        columns = {str(column).strip().lower(): column for column in raw.columns}
        date_key = columns.get(self.date_column.lower())
        value_key = columns.get(self.value_column.lower())
        if date_key is None or value_key is None:
            raise CollectorError(
                f"{self.path.name}: expected columns "
                f"{self.date_column!r} and {self.value_column!r}, found {list(raw.columns)}"
            )

        frame = raw[[date_key, value_key]].rename(
            columns={date_key: "observation_date", value_key: "value"}
        )
        frame["observation_date"] = pd.to_datetime(frame["observation_date"], errors="coerce")
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        frame = frame.dropna()
        if frame.empty:
            raise DataUnavailableError(f"{self.path.name}: no usable rows")

        frame = standardize_series(
            frame.set_index("observation_date"), name=self.path.name, column="value"
        )
        window = frame.loc[(frame.index >= start) & (frame.index <= end)]
        if window.empty:
            raise DataUnavailableError(
                f"{self.path.name}: no rows between {start} and {end}"
            )
        return window

    def describe(self) -> SourceDescription:
        return SourceDescription(
            provider_library="pandas",
            provider_library_version=pd.__version__,
            underlying_source=self.underlying_source,
            source_ref=self.source_ref or self.path.name,
        )


@dataclass(frozen=True, slots=True)
class AaiiSentimentProvider:
    """AAII weekly sentiment, reduced to the bull-bear spread (TASK-024).

    The survey covers a week ending Wednesday and is published afterwards, so
    the caller must pass the configured ``availability_lag_days``; this provider
    only reports the observation dates. Treating the publication date as the
    observation date is exactly the availability leak ``BACKTEST_SPEC.md`` 24.6
    checks for.
    """

    path: Path
    underlying_source: str = "AAII weekly Investor Sentiment Survey"
    date_column: str = "date"
    bullish_column: str = "bullish"
    bearish_column: str = "bearish"
    quality_status: DataQualityStatus = DataQualityStatus.OK

    def fetch(self, start: date, end: date) -> DataFrame:
        raw = _read_table(self.path)
        columns = {str(column).strip().lower(): column for column in raw.columns}
        missing = [
            wanted
            for wanted in (self.date_column, self.bullish_column, self.bearish_column)
            if wanted.lower() not in columns
        ]
        if missing:
            raise CollectorError(
                f"{self.path.name}: missing columns {missing}; found {list(raw.columns)}"
            )

        frame = DataFrame(
            {
                "observation_date": pd.to_datetime(
                    raw[columns[self.date_column.lower()]], errors="coerce"
                ),
                "bullish": _as_fraction(raw[columns[self.bullish_column.lower()]]),
                "bearish": _as_fraction(raw[columns[self.bearish_column.lower()]]),
            }
        ).dropna()
        if frame.empty:
            raise DataUnavailableError(f"{self.path.name}: no usable AAII rows")

        frame["value"] = frame["bullish"] - frame["bearish"]
        frame = standardize_series(
            frame.set_index("observation_date")[["value"]],
            name=self.path.name,
            column="value",
        )
        window = frame.loc[(frame.index >= start) & (frame.index <= end)]
        if window.empty:
            raise DataUnavailableError(f"{self.path.name}: no AAII rows between {start} and {end}")
        return window

    def describe(self) -> SourceDescription:
        return SourceDescription(
            provider_library="pandas",
            provider_library_version=pd.__version__,
            underlying_source=self.underlying_source,
            source_ref=self.path.name,
        )


def _as_fraction(column: pd.Series) -> pd.Series:
    """Accept both 38.5 and 0.385 for "38.5% bullish"."""
    numeric = pd.to_numeric(
        column.astype(str).str.replace("%", "", regex=False), errors="coerce"
    )
    if numeric.dropna().empty:
        return numeric
    return numeric / 100.0 if numeric.dropna().abs().max() > 1.5 else numeric


@dataclass(frozen=True, slots=True)
class CompositeSeriesProvider:
    """A long file history with a live feed layered on top.

    CNN needs this: its endpoint serves about a year, while a backtest needs a
    decade. The file supplies the history, the endpoint supplies the days the
    file has not caught up with, and the live values win where both have an
    opinion — the endpoint is the publisher, the file is a copy of it.

    The whole series is REVIEW-grade regardless. A series that contains
    reconstructed values is a reconstructed series, and downgrading only the old
    rows would let a summary statistic quietly mix the two.
    """

    history: CsvSeriesProvider
    live: object
    quality_status: DataQualityStatus = DataQualityStatus.REVIEW

    def fetch(self, start: date, end: date) -> DataFrame:
        frames: list[DataFrame] = []
        for source, label in ((self.history, "history"), (self.live, "live")):
            try:
                frames.append(source.fetch(start, end))  # type: ignore[attr-defined]
            except (CollectorError, DataUnavailableError) as exc:
                logger.info("composite series: %s unavailable (%s)", label, exc)

        if not frames:
            raise DataUnavailableError(
                f"neither the file history nor the live feed covered {start}..{end}"
            )

        combined = pd.concat(frames)
        # Later frames win, so the live feed overrides the file where they overlap.
        combined = combined[~combined.index.duplicated(keep="last")]
        return combined.sort_index()

    def describe(self) -> SourceDescription:
        base = self.history.describe()
        return SourceDescription(
            provider_library=base.provider_library,
            provider_library_version=base.provider_library_version,
            underlying_source=f"{base.underlying_source} + live endpoint",
            source_ref="composite",
        )

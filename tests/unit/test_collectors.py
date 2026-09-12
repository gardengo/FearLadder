"""TASK-021 .. TASK-027 — collection ports, standardization, retry, provenance."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
from pandas import DataFrame

from regime_monitor.constants import DataQualityStatus
from regime_monitor.data.collectors.base import (
    Collector,
    CollectorError,
    DataUnavailableError,
    PriceProvider,
    SeriesProvider,
    SourceDescription,
    availability_for,
    market_close_utc,
    standardize_prices,
    standardize_series,
    with_retry,
)
from regime_monitor.data.collectors.breadth import (
    BreadthUnavailableError,
    UnavailableBreadthProvider,
)
from regime_monitor.data.collectors.files import AaiiSentimentProvider, CsvSeriesProvider
from regime_monitor.data.collectors.registry import (
    ProviderRegistry,
    UnknownProviderError,
    build_registry,
)

DAY = date(2024, 1, 3)


# --------------------------------------------------------------------- timing


def test_market_close_is_dst_aware() -> None:
    # A fixed UTC offset would silently move availability by an hour each spring.
    assert market_close_utc(date(2024, 1, 3)) == datetime(2024, 1, 3, 21, tzinfo=UTC)
    assert market_close_utc(date(2024, 7, 3)) == datetime(2024, 7, 3, 20, tzinfo=UTC)


def test_publication_lag_delays_availability() -> None:
    # AAII describes a week ending Wednesday but publishes later (PRD.md 6.5).
    assert availability_for(DAY) == market_close_utc(DAY)
    assert availability_for(DAY, lag_days=1) == market_close_utc(date(2024, 1, 4))
    with pytest.raises(ValueError, match="cannot be negative"):
        availability_for(DAY, lag_days=-1)


# ---------------------------------------------------------------------- retry


def test_retry_succeeds_after_transient_failures() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("boom")
        return "ok"

    assert with_retry(flaky, attempts=3, sleep=lambda _: None) == "ok"
    assert calls["n"] == 3


def test_retry_gives_up_and_says_so() -> None:
    with pytest.raises(CollectorError, match="failed after 2 attempts"):
        with_retry(
            lambda: (_ for _ in ()).throw(ConnectionError("down")),
            attempts=2,
            description="fetch QQQ",
            sleep=lambda _: None,
        )


def test_retry_does_not_retry_a_definitive_empty_answer() -> None:
    # "there is no data" is an answer, not a transient fault.
    calls = {"n": 0}

    def empty() -> None:
        calls["n"] += 1
        raise DataUnavailableError("no rows")

    with pytest.raises(DataUnavailableError):
        with_retry(empty, attempts=3, sleep=lambda _: None)
    assert calls["n"] == 1


# ------------------------------------------------------------ standardization


def _raw_prices() -> DataFrame:
    return DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.0, 102.0],
            "Volume": [1000, 1100],
            "Adj Close": [100.5, 101.5],
        },
        index=pd.to_datetime(["2024-01-03", "2024-01-04"]),
    )


def test_prices_are_standardized_to_canonical_columns() -> None:
    frame = standardize_prices(_raw_prices(), symbol="QQQ")
    assert list(frame.columns) == ["open", "high", "low", "close", "adj_close", "volume"]
    assert list(frame.index) == [date(2024, 1, 3), date(2024, 1, 4)]


def test_standardization_fills_absent_optional_columns() -> None:
    raw = DataFrame({"Close": [101.0]}, index=pd.to_datetime(["2024-01-03"]))
    frame = standardize_prices(raw, symbol="X")
    assert frame.loc[DAY, "close"] == 101.0
    assert pd.isna(frame.loc[DAY, "open"])


def test_standardization_refuses_a_frame_without_a_close() -> None:
    raw = DataFrame({"Open": [101.0]}, index=pd.to_datetime(["2024-01-03"]))
    with pytest.raises(CollectorError, match="no close column"):
        standardize_prices(raw, symbol="X")


def test_standardization_sorts_and_deduplicates() -> None:
    raw = DataFrame(
        {"Close": [102.0, 100.0, 101.0]},
        index=pd.to_datetime(["2024-01-04", "2024-01-03", "2024-01-04"]),
    )
    frame = standardize_prices(raw, symbol="X")
    assert list(frame.index) == [date(2024, 1, 3), date(2024, 1, 4)]
    assert frame.loc[date(2024, 1, 4), "close"] == 101.0  # last wins


def test_empty_provider_output_is_unavailable_not_a_crash() -> None:
    with pytest.raises(DataUnavailableError):
        standardize_prices(DataFrame(), symbol="X")


def test_series_standardization_picks_the_named_column() -> None:
    raw = DataFrame(
        {"Close": [13.5, 14.0], "Open": [13.0, 13.8]},
        index=pd.to_datetime(["2024-01-03", "2024-01-04"]),
    )
    frame = standardize_series(raw, name="VIX", column="Close")
    assert list(frame.columns) == ["value"]
    assert frame.loc[DAY, "value"] == 13.5


def test_series_standardization_needs_disambiguation_when_ambiguous() -> None:
    raw = DataFrame({"a": [1.0], "b": [2.0]}, index=pd.to_datetime(["2024-01-03"]))
    with pytest.raises(CollectorError, match="expected one column"):
        standardize_series(raw, name="X")


# -------------------------------------------------------------- the collector


class FakePriceProvider:
    """A provider that satisfies the port without touching the network."""

    def __init__(self, frame: DataFrame | None = None) -> None:
        self.frame = _raw_prices() if frame is None else frame
        self.calls: list[tuple[str, date, date]] = []

    def fetch(self, symbol: str, start: date, end: date) -> DataFrame:
        self.calls.append((symbol, start, end))
        return standardize_prices(self.frame, symbol=symbol)

    def describe(self, symbol: str) -> SourceDescription:
        return SourceDescription(
            provider_library="FinanceDataReader",
            provider_library_version="0.9.202",
            underlying_source="test feed",
            source_ref=f"fdr:{symbol}",
        )


class FakeSeriesProvider:
    def __init__(self, values: dict[date, float]) -> None:
        self.values = values

    def fetch(self, start: date, end: date) -> DataFrame:
        return DataFrame(
            {"value": list(self.values.values())},
            index=pd.to_datetime(list(self.values.keys())),
        )

    def describe(self) -> SourceDescription:
        return SourceDescription(
            provider_library="pandas",
            provider_library_version=pd.__version__,
            underlying_source="reconstructed dataset",
        )


def test_fakes_satisfy_the_ports() -> None:
    assert isinstance(FakePriceProvider(), PriceProvider)
    assert isinstance(FakeSeriesProvider({}), SeriesProvider)


def test_collector_produces_observations_with_full_provenance() -> None:
    # ARCHITECTURE.md 3.5 requires all four provenance fields.
    clock = lambda: datetime(2024, 1, 5, 3, 0, tzinfo=UTC)  # noqa: E731
    observations = Collector(clock=clock).price_observations(
        FakePriceProvider(), "QQQ", DAY, date(2024, 1, 4)
    )
    assert [obs.observation_date for obs in observations] == [DAY, date(2024, 1, 4)]

    first = observations[0]
    assert first.close == 101.0
    assert first.provenance.provider_library == "FinanceDataReader"
    assert first.provenance.provider_library_version == "0.9.202"
    assert first.provenance.underlying_source == "test feed"
    assert first.provenance.retrieved_at == clock()
    assert first.availability_datetime == market_close_utc(DAY)


def test_collector_applies_the_publication_lag_to_series() -> None:
    observations = Collector().series_observations(
        FakeSeriesProvider({DAY: 0.12}),
        "AAII_SENTIMENT",
        DAY,
        DAY,
        availability_lag_days=1,
    )
    assert observations[0].value == pytest.approx(0.12)
    assert observations[0].availability_datetime == market_close_utc(date(2024, 1, 4))


def test_collector_can_mark_a_whole_series_review_grade() -> None:
    observations = Collector().series_observations(
        FakeSeriesProvider({DAY: 40.0}),
        "CNN_FEAR_GREED",
        DAY,
        DAY,
        quality_status=DataQualityStatus.REVIEW,
    )
    assert observations[0].quality_status is DataQualityStatus.REVIEW


# ------------------------------------------------------------- file providers


def test_csv_series_provider_reads_and_windows(tmp_path: Path) -> None:
    path = tmp_path / "series.csv"
    path.write_text(
        "date,value\n2024-01-02,30\n2024-01-03,35\n2024-01-04,40\n", encoding="utf-8"
    )
    provider = CsvSeriesProvider(path=path, underlying_source="reconstructed")
    frame = provider.fetch(date(2024, 1, 3), date(2024, 1, 4))
    assert list(frame["value"]) == [35.0, 40.0]


def test_reconstructed_datasets_are_review_grade_by_construction(tmp_path: Path) -> None:
    # PRD.md 6.5: reconstructed sentiment must never look official.
    path = tmp_path / "series.csv"
    path.write_text("date,value\n2024-01-03,35\n", encoding="utf-8")
    provider = CsvSeriesProvider(path=path, underlying_source="reconstructed")
    assert provider.quality_status is DataQualityStatus.REVIEW


def test_missing_reference_file_explains_itself(tmp_path: Path) -> None:
    provider = CsvSeriesProvider(path=tmp_path / "nope.csv", underlying_source="x")
    with pytest.raises(DataUnavailableError, match="reference dataset not found"):
        provider.fetch(DAY, DAY)


def test_csv_provider_rejects_a_file_with_the_wrong_columns(tmp_path: Path) -> None:
    path = tmp_path / "series.csv"
    path.write_text("when,how_much\n2024-01-03,35\n", encoding="utf-8")
    with pytest.raises(CollectorError, match="expected columns"):
        CsvSeriesProvider(path=path, underlying_source="x").fetch(DAY, DAY)


@pytest.mark.parametrize(
    ("bullish", "bearish", "expected"),
    [("38.5", "30.5", 0.08), ("0.385", "0.305", 0.08), ("38.5%", "30.5%", 0.08)],
)
def test_aaii_spread_accepts_percent_or_fraction(
    tmp_path: Path, bullish: str, bearish: str, expected: float
) -> None:
    path = tmp_path / "aaii.csv"
    path.write_text(f"date,bullish,bearish\n2024-01-03,{bullish},{bearish}\n", encoding="utf-8")
    frame = AaiiSentimentProvider(path=path).fetch(DAY, DAY)
    assert frame.loc[DAY, "value"] == pytest.approx(expected)


def test_aaii_provider_requires_both_survey_columns(tmp_path: Path) -> None:
    path = tmp_path / "aaii.csv"
    path.write_text("date,bullish\n2024-01-03,38.5\n", encoding="utf-8")
    with pytest.raises(CollectorError, match="missing columns"):
        AaiiSentimentProvider(path=path).fetch(DAY, DAY)


# ------------------------------------------------------------------- breadth


def test_breadth_fails_loudly_instead_of_returning_a_biased_series() -> None:
    # TASK-025 / PRD.md 6.5 — survivorship bias is worse than no indicator.
    provider = UnavailableBreadthProvider()
    with pytest.raises(BreadthUnavailableError, match="Point-in-time"):
        provider.fetch(DAY, DAY)
    with pytest.raises(BreadthUnavailableError):
        provider.describe()


# ------------------------------------------------------------------ registry


def test_registry_builds_every_enabled_series(placeholder_config, tmp_path: Path) -> None:
    registry = build_registry(placeholder_config.data_sources, reference_dir=tmp_path)
    assert isinstance(registry, ProviderRegistry)
    assert set(registry.series) == {"VIX", "CNN_FEAR_GREED", "AAII_SENTIMENT"}
    assert "BREADTH_NDX" in registry.disabled


def test_registry_refuses_a_disabled_series_with_its_reason(
    placeholder_config, tmp_path: Path
) -> None:
    registry = build_registry(placeholder_config.data_sources, reference_dir=tmp_path)
    with pytest.raises(UnknownProviderError, match=r"(?i)survivorship"):
        registry.series_provider("BREADTH_NDX")


def test_registry_rejects_an_unimplemented_provider(placeholder_config, tmp_path: Path) -> None:
    from regime_monitor.config.schema import SeriesSourceSpec
    from regime_monitor.data.collectors.registry import _build_series_provider

    spec = SeriesSourceSpec(provider="bloomberg", underlying_source="x")
    with pytest.raises(UnknownProviderError, match="not implemented"):
        _build_series_provider("X", spec, tmp_path)

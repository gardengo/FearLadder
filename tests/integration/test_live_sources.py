"""Live-source smoke tests.

These hit the real network and are excluded from the default run and from CI
(``-m "not network"``). They exist so a human can check, on demand, that a
provider still behaves the way the adapters assume — which is the failure mode a
mocked test cannot catch.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from regime_monitor.data.collectors.base import Collector, DataUnavailableError
from regime_monitor.data.collectors.cnn import CnnFearGreedProvider
from regime_monitor.data.collectors.fdr import (
    FinanceDataReaderPriceProvider,
    FinanceDataReaderSeriesProvider,
    library_version,
)

pytestmark = pytest.mark.network

START = date(2024, 1, 2)
END = date(2024, 1, 12)


@pytest.mark.parametrize("symbol", ["QQQ", "QLD", "TQQQ"])
def test_fdr_returns_standardized_prices(symbol: str) -> None:
    provider = FinanceDataReaderPriceProvider(underlying_sources={})
    frame = provider.fetch(symbol, START, END)
    assert list(frame.columns) == ["open", "high", "low", "close", "adj_close", "volume"]
    assert frame.index.is_monotonic_increasing
    assert (frame["close"] > 0).all()


def test_fdr_provenance_records_the_real_library_version() -> None:
    description = FinanceDataReaderPriceProvider(underlying_sources={}).describe("QQQ")
    assert description.provider_library == "FinanceDataReader"
    assert description.provider_library_version == library_version()
    assert description.provider_library_version != "unknown"


def test_vix_is_reachable_through_the_same_adapter() -> None:
    provider = FinanceDataReaderSeriesProvider(symbol="VIX", underlying_source="Cboe")
    frame = provider.fetch(START, END)
    assert list(frame.columns) == ["value"]
    assert (frame["value"] > 0).all()


def test_fred_vixcls_is_reachable_as_the_cross_check() -> None:
    # PRD.md 6.5 names FRED VIXCLS as a VIX cross-validation reference.
    provider = FinanceDataReaderSeriesProvider(
        symbol="FRED:VIXCLS", underlying_source="FRED", column=None
    )
    frame = provider.fetch(START, END)
    assert (frame["value"] > 0).all()


def test_collector_builds_observations_from_live_prices() -> None:
    observations = Collector().price_observations(
        FinanceDataReaderPriceProvider(underlying_sources={"QQQ": "FDR US equity feed"}),
        "QQQ",
        START,
        END,
    )
    assert observations
    first = observations[0]
    assert first.provenance.underlying_source == "FDR US equity feed"
    assert first.close is not None


def test_cnn_live_endpoint_serves_recent_history_only() -> None:
    """CNN is reachable, but only for roughly a year.

    This is the observation behind ``PRD.md`` 6.5's live/historical split, so it
    is asserted rather than assumed.
    """
    provider = CnnFearGreedProvider()
    recent_end = date.today()
    recent = provider.fetch(recent_end - timedelta(days=30), recent_end)
    assert not recent.empty
    assert recent["value"].between(0, 100).all()

    with pytest.raises(DataUnavailableError):
        provider.fetch(date(2008, 1, 1), date(2008, 3, 1))

"""Shared fixtures.

Every test runs against a throwaway database; nothing may touch the operational
``data/regime_monitor.db``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from regime_monitor.config.loader import load_research_placeholder_config
from regime_monitor.config.schema import AppConfig
from regime_monitor.constants import DataQualityStatus
from regime_monitor.data.models import MarketObservation, Provenance
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork, create_database


@pytest.fixture(autouse=True)
def _isolate_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every default DB lookup at a per-test file."""
    monkeypatch.setenv("REGIME_MONITOR_DB", str(tmp_path / "test.db"))


@pytest.fixture(autouse=True)
def _closed_research_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never inherit a developer's open research-parameter gate."""
    monkeypatch.delenv("REGIME_MONITOR_ALLOW_RESEARCH_PARAMS", raising=False)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return create_database(tmp_path / "test.db")


@pytest.fixture
def uow(db_path: Path) -> Iterator[SQLiteUnitOfWork]:
    with SQLiteUnitOfWork(db_path) as unit:
        yield unit


@pytest.fixture(scope="session")
def placeholder_config() -> AppConfig:
    """The RESEARCH_PLACEHOLDER profile — the only config with concrete numbers."""
    return load_research_placeholder_config()


@pytest.fixture
def provenance() -> Provenance:
    return Provenance(
        provider_library="FinanceDataReader",
        provider_library_version="0.9.202",
        underlying_source="test-fixture",
        retrieved_at=datetime(2024, 1, 3, 23, 0, tzinfo=UTC),
        source_ref="unit-test",
    )


def make_price_observation(
    symbol: str,
    observation_date: date,
    close: float,
    provenance: Provenance,
    *,
    available_at: datetime | None = None,
    quality_status: DataQualityStatus = DataQualityStatus.OK,
) -> MarketObservation:
    """A minimal but valid OHLCV row."""
    return MarketObservation(
        symbol=symbol,
        observation_date=observation_date,
        availability_datetime=available_at
        or datetime.combine(observation_date, datetime.min.time(), tzinfo=UTC).replace(hour=21),
        provenance=provenance,
        open=close * 0.99,
        high=close * 1.01,
        low=close * 0.98,
        close=close,
        adj_close=close,
        volume=1_000_000,
        quality_status=quality_status,
    )


def make_scalar_observation(
    symbol: str,
    observation_date: date,
    value: float,
    provenance: Provenance,
    *,
    available_at: datetime | None = None,
) -> MarketObservation:
    return MarketObservation(
        symbol=symbol,
        observation_date=observation_date,
        availability_datetime=available_at
        or datetime.combine(observation_date, datetime.min.time(), tzinfo=UTC).replace(hour=21),
        provenance=provenance,
        value=value,
    )

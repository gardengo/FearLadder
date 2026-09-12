"""Breadth adapter (TASK-025) — deliberately not implemented.

``TASK-025`` says: "데이터 품질이 불충분하면 명시적으로 제외한다."

Breadth for the Nasdaq-100 requires knowing which stocks were *in* the index on
each historical date. That point-in-time membership is not available here, and
``PRD.md`` 6.5 explicitly forbids the tempting shortcut:

    현재 구성종목으로 과거 breadth를 재구성하여 장기 백테스트에 사용하는 것은
    금지한다.

Rebuilding 2008 breadth from today's members would silently drop every company
that was removed from the index, biasing the indicator toward survivors exactly
during the crashes it is meant to measure.

So breadth stays a research candidate. ``BREADTH_NDX`` is disabled in
``config/data_sources.yaml`` with that reason recorded, and
``breadth_pct_above_200dma`` is disabled in ``config/indicators.yaml``. The
scaffolding below exists so that the day point-in-time constituents are
obtained, only :meth:`ConstituentSource.members_on` needs an implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from pandas import DataFrame

from regime_monitor.data.collectors.base import CollectorError, SourceDescription


class BreadthUnavailableError(CollectorError):
    """Raised when breadth is requested without survivorship-free constituents."""


@runtime_checkable
class ConstituentSource(Protocol):
    """Point-in-time index membership."""

    def members_on(self, day: date) -> tuple[str, ...]:
        """Tickers that were in the index on ``day`` — not the members today."""
        ...


@dataclass(frozen=True, slots=True)
class UnavailableBreadthProvider:
    """Placeholder that fails loudly instead of returning a biased series."""

    reason: str = (
        "Point-in-time Nasdaq-100 constituents are not available. Reconstructing "
        "historical breadth from current members is forbidden (PRD.md 6.5), so "
        "breadth remains a research candidate (TASK-025, TASK-036)."
    )

    def fetch(self, start: date, end: date) -> DataFrame:  # noqa: ARG002
        raise BreadthUnavailableError(self.reason)

    def describe(self) -> SourceDescription:
        raise BreadthUnavailableError(self.reason)

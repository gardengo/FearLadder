"""Ports the application layer talks to (TASK-011, ``ARCHITECTURE.md`` 7).

Nothing above this module may import ``sqlite3``. Swapping SQLite for
PostgreSQL later means writing new adapters, not touching the pipeline.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from pandas import DataFrame

from fear_ladder.constants import EventType
from fear_ladder.data.models import (
    AlertEvent,
    DataQualityFinding,
    IndicatorScore,
    IndicatorValue,
    MarketObservation,
    MarketState,
    PipelineRun,
    RegimeEvent,
    StrategyVersionRecord,
    TargetAllocation,
)


@runtime_checkable
class MarketObservationRepository(Protocol):
    """Raw input series."""

    def save_observations(self, observations: list[MarketObservation]) -> int:
        """Upsert observations. Returns the number of rows written."""
        ...

    def get_observation(self, symbol: str, observation_date: date) -> MarketObservation | None: ...

    def get_observations(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
        available_at: datetime | None = None,
    ) -> list[MarketObservation]:
        """Ordered history.

        ``available_at`` restricts the result to rows that were already knowable
        at that instant, which is how the backtester avoids look-ahead
        (``BACKTEST_SPEC.md`` 4).
        """
        ...

    def get_series_frame(
        self,
        symbols: list[str],
        *,
        start: date | None = None,
        end: date | None = None,
        available_at: datetime | None = None,
    ) -> DataFrame:
        """Wide frame indexed by observation_date, one column per symbol."""
        ...

    def latest_observation_date(
        self, symbol: str, *, on_or_before: date | None = None
    ) -> date | None:
        """Newest observation for ``symbol``, optionally capped at a date.

        The cap is what makes replaying a past day honest: without it the query
        sees rows the run is not entitled to know about yet.
        """
        ...

    def save_finding(self, finding: DataQualityFinding) -> None: ...

    def get_open_findings(self, *, symbol: str | None = None) -> list[DataQualityFinding]: ...


@runtime_checkable
class IndicatorRepository(Protocol):
    """Raw indicator values and their normalized scores."""

    def save_values(self, values: list[IndicatorValue]) -> int: ...

    def get_values(
        self,
        indicator_name: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[IndicatorValue]: ...

    def save_scores(self, scores: list[IndicatorScore], *, strategy_version: str) -> int: ...

    def get_scores(
        self,
        observation_date: date,
        *,
        strategy_version: str,
    ) -> list[IndicatorScore]: ...


@runtime_checkable
class MarketStateRepository(Protocol):
    """The daily verdict and its allocation."""

    def save_state(self, state: MarketState, allocation: TargetAllocation | None = None) -> int:
        """Upsert one day's state (and allocation) atomically. Returns its id."""
        ...

    def get_state(self, observation_date: date, *, strategy_version: str) -> MarketState | None: ...

    def get_latest_state(self, *, strategy_version: str | None = None) -> MarketState | None: ...

    def get_previous_state(
        self,
        before: date,
        *,
        strategy_version: str,
        exclude_unknown: bool = True,
    ) -> MarketState | None:
        """Most recent state strictly before ``before``.

        ``exclude_unknown`` skips failure-safe rows so that a day of bad data
        does not masquerade as the previous regime.
        """
        ...

    def get_state_history(
        self,
        *,
        strategy_version: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[MarketState]: ...

    def get_allocation(
        self, observation_date: date, *, strategy_version: str
    ) -> TargetAllocation | None: ...


@runtime_checkable
class EventRepository(Protocol):
    """Regime transitions and outbound alerts."""

    def save_regime_event(self, event: RegimeEvent) -> bool:
        """Insert if new. Returns ``False`` when the event already existed."""
        ...

    def get_regime_events(
        self,
        *,
        strategy_version: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[RegimeEvent]: ...

    def save_alert(self, alert: AlertEvent) -> bool:
        """Insert if new. Returns ``False`` when ``dedupe_key`` already existed."""
        ...

    def get_alert(self, dedupe_key: str) -> AlertEvent | None: ...

    def get_pending_alerts(self) -> list[AlertEvent]: ...

    def mark_alert_delivered(self, alert: AlertEvent) -> None: ...

    def last_alert_date(
        self, event_type: EventType, *, strategy_version: str
    ) -> date | None: ...

    def get_alerts(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        event_type: EventType | None = None,
    ) -> list[AlertEvent]: ...


@runtime_checkable
class StrategyRepository(Protocol):
    """Strategy manifests and pipeline run audit trail."""

    def save_version(self, record: StrategyVersionRecord) -> None: ...

    def get_version(self, strategy_version: str) -> StrategyVersionRecord | None: ...

    def get_active_version(self) -> StrategyVersionRecord | None: ...

    def activate(self, strategy_version: str) -> None: ...

    def save_run(self, run: PipelineRun) -> None: ...

    def get_run(self, run_id: str) -> PipelineRun | None: ...

    def get_runs(self, *, run_date: date | None = None, limit: int = 50) -> list[PipelineRun]: ...

    def last_successful_run(self) -> PipelineRun | None: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Transaction boundary shared by the repositories.

    The daily worker persists state, events and alerts inside one of these, so a
    failure half way through cannot leave a state without its allocation
    (``CONTRIBUTING.md`` 5: never push a wrong "normal" state).
    """

    observations: MarketObservationRepository
    indicators: IndicatorRepository
    states: MarketStateRepository
    events: EventRepository
    strategies: StrategyRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

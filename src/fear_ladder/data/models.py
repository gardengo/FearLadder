"""Domain models (TASK-020).

Plain frozen dataclasses, deliberately free of any persistence concern: the
repository layer maps them onto SQLite today and could map them onto PostgreSQL
tomorrow (``ARCHITECTURE.md`` 7).

Two date-like fields appear everywhere and must not be confused
(``BACKTEST_SPEC.md`` 4):

``observation_date``
    The trading day the value *describes*.
``availability_datetime``
    The instant the value could first legitimately be known. A strategy running
    at time ``T`` may only read rows whose ``availability_datetime <= T``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any, Self

from fear_ladder.constants import (
    ASSET_LEVERAGE,
    SCORE_MAX,
    SCORE_MIN,
    UNKNOWN_REGIME,
    Asset,
    DataQualityStatus,
    EventType,
    PipelineStatus,
)

WEIGHT_SUM_TOLERANCE = 1e-6


class DomainError(ValueError):
    """Raised when a domain object would violate an invariant."""


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise DomainError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_score(value: float, field_name: str) -> float:
    if not SCORE_MIN <= value <= SCORE_MAX:
        raise DomainError(f"{field_name}={value!r} is outside [{SCORE_MIN}, {SCORE_MAX}]")
    return float(value)


# ---------------------------------------------------------------- provenance


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a number actually came from.

    ``PRD.md`` 6.5 is emphatic that the collection library is not the source, so
    both are recorded: FinanceDataReader is ``provider_library`` while the feed
    it read is ``underlying_source``.
    """

    provider_library: str
    provider_library_version: str
    underlying_source: str
    retrieved_at: datetime
    source_ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("provider_library", "provider_library_version", "underlying_source"):
            if not getattr(self, name):
                raise DomainError(f"provenance.{name} is required")
        object.__setattr__(self, "retrieved_at", _require_utc(self.retrieved_at, "retrieved_at"))


# --------------------------------------------------------------- observations


@dataclass(frozen=True, slots=True)
class MarketObservation:
    """One day of one input series.

    Price series populate the OHLCV fields; scalar series (VIX, CNN Fear &
    Greed, AAII) populate ``value``. Keeping both in one table is what lets the
    freshness and provenance rules be applied uniformly.
    """

    symbol: str
    observation_date: date
    availability_datetime: datetime
    provenance: Provenance
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    adj_close: float | None = None
    volume: float | None = None
    value: float | None = None
    quality_status: DataQualityStatus = DataQualityStatus.OK
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise DomainError("symbol is required")
        object.__setattr__(
            self,
            "availability_datetime",
            _require_utc(self.availability_datetime, "availability_datetime"),
        )
        if self.close is None and self.value is None:
            raise DomainError(
                f"{self.symbol} {self.observation_date}: an observation needs either "
                "a close (price series) or a value (scalar series)"
            )
        for name in ("open", "high", "low", "close", "adj_close"):
            price = getattr(self, name)
            if price is not None and price <= 0:
                raise DomainError(f"{self.symbol} {self.observation_date}: {name}={price!r} <= 0")
        if self.volume is not None and self.volume < 0:
            raise DomainError(f"{self.symbol} {self.observation_date}: negative volume")
        if None not in (self.high, self.low) and self.high < self.low:
            raise DomainError(f"{self.symbol} {self.observation_date}: high < low")

    @property
    def is_price(self) -> bool:
        return self.close is not None

    @property
    def primary_value(self) -> float:
        """The single number downstream indicators consume."""
        return float(self.close if self.close is not None else self.value)  # type: ignore[arg-type]

    def is_available_at(self, moment: datetime) -> bool:
        return self.availability_datetime <= _require_utc(moment, "moment")

    def flagged(self, status: DataQualityStatus, note: str) -> Self:
        """Return a copy marked with a quality verdict, never mutating in place."""
        return replace(self, quality_status=status, note=note)


# ----------------------------------------------------------------- indicators


@dataclass(frozen=True, slots=True)
class IndicatorValue:
    """A raw indicator reading, before normalization."""

    indicator_name: str
    observation_date: date
    value: float | None
    source_symbol: str
    params: dict[str, Any] = field(default_factory=dict)
    availability_datetime: datetime | None = None
    quality_status: DataQualityStatus = DataQualityStatus.OK

    def __post_init__(self) -> None:
        if not self.indicator_name:
            raise DomainError("indicator_name is required")
        if self.availability_datetime is not None:
            object.__setattr__(
                self,
                "availability_datetime",
                _require_utc(self.availability_datetime, "availability_datetime"),
            )

    @property
    def params_key(self) -> str:
        """Stable identity for the parameterisation that produced this value."""
        return json.dumps(self.params, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True, slots=True)
class IndicatorScore:
    """A normalized indicator on the 0-100 fear/greed axis (``PRD.md`` 7)."""

    indicator_name: str
    observation_date: date
    score: float | None
    normalization_method: str
    normalization_window: int | None = None
    raw_value: float | None = None
    quality_status: DataQualityStatus = DataQualityStatus.OK

    def __post_init__(self) -> None:
        if self.score is not None:
            object.__setattr__(self, "score", _require_score(self.score, "score"))


# --------------------------------------------------------------- market state


@dataclass(frozen=True, slots=True)
class MarketState:
    """The pipeline's verdict for one trading day.

    ``regime`` is the *confirmed* regime after transition logic; ``raw_regime``
    is what the classifier said before confirmation/hysteresis. Both are stored
    so a transition can be explained after the fact (TASK-052).
    """

    observation_date: date
    composite_score: float | None
    regime: str
    raw_regime: str | None = None
    previous_regime: str | None = None
    previous_score: float | None = None
    target_leverage: float | None = None
    data_quality_status: DataQualityStatus = DataQualityStatus.OK
    reason_codes: tuple[str, ...] = ()
    score_breakdown: dict[str, float] = field(default_factory=dict)
    strategy_version: str = ""
    data_version: str | None = None
    parameter_version: str | None = None
    code_commit: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.regime:
            raise DomainError("regime is required")
        if not self.strategy_version:
            raise DomainError("strategy_version is required on a persisted state")
        if self.composite_score is not None:
            object.__setattr__(
                self, "composite_score", _require_score(self.composite_score, "composite_score")
            )
        if self.previous_score is not None:
            _require_score(self.previous_score, "previous_score")
        if self.is_unknown and self.target_leverage is not None:
            raise DomainError("an UNKNOWN regime must not carry a target leverage")
        if not self.is_unknown and self.composite_score is None:
            raise DomainError(f"regime {self.regime!r} requires a composite score")
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))

    @property
    def is_unknown(self) -> bool:
        return self.regime == UNKNOWN_REGIME

    @property
    def score_change(self) -> float | None:
        if self.composite_score is None or self.previous_score is None:
            return None
        return self.composite_score - self.previous_score

    @property
    def regime_changed(self) -> bool:
        return self.previous_regime is not None and self.previous_regime != self.regime

    @classmethod
    def unknown(
        cls,
        observation_date: date,
        *,
        strategy_version: str,
        reason_codes: tuple[str, ...],
        quality_status: DataQualityStatus = DataQualityStatus.MISSING,
        previous_regime: str | None = None,
        **kwargs: Any,
    ) -> Self:
        """Failure-safe state (``ARCHITECTURE.md`` 15).

        Used whenever mandatory data is missing, stale or corrupt. It carries no
        score and no leverage, so it cannot be mistaken for investment advice.
        """
        return cls(
            observation_date=observation_date,
            composite_score=None,
            regime=UNKNOWN_REGIME,
            previous_regime=previous_regime,
            target_leverage=None,
            data_quality_status=quality_status,
            reason_codes=reason_codes,
            strategy_version=strategy_version,
            **kwargs,
        )


@dataclass(frozen=True, slots=True)
class TargetAllocation:
    """Target sleeve weights for one day, plus the derived research metric.

    ``BACKTEST_SPEC.md`` 14: target leverage is a way of *describing* combined
    exposure, not a promise about realised returns.
    """

    observation_date: date
    weights: dict[Asset, float]
    strategy_version: str
    regime: str

    def __post_init__(self) -> None:
        if not self.weights:
            raise DomainError("an allocation needs at least one sleeve")
        for asset, weight in self.weights.items():
            if not isinstance(asset, Asset):
                raise DomainError(f"allocation key {asset!r} is not an Asset")
            if weight < 0:
                raise DomainError(f"allocation {asset} has negative weight {weight!r}")
        total = sum(self.weights.values())
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise DomainError(f"allocation weights sum to {total!r}, not 1.0")

    @property
    def target_leverage(self) -> float:
        return sum(ASSET_LEVERAGE[asset] * weight for asset, weight in self.weights.items())

    @property
    def market_exposure(self) -> float:
        """Share of capital not in cash."""
        return 1.0 - self.weights.get(Asset.CASH, 0.0)

    def weight(self, asset: Asset) -> float:
        return self.weights.get(asset, 0.0)


# --------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class RegimeEvent:
    """TASK-052 — a confirmed regime transition."""

    event_date: date
    previous_regime: str | None
    new_regime: str
    previous_score: float | None
    new_score: float | None
    reason_codes: tuple[str, ...]
    strategy_version: str
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.previous_regime == self.new_regime:
            raise DomainError("a regime event must record an actual change")
        if not self.strategy_version:
            raise DomainError("strategy_version is required")
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """One notification, identified by a key that makes re-sending impossible."""

    event_date: date
    event_type: EventType
    severity: str
    title: str
    body: str
    strategy_version: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    sent_at: datetime | None = None
    delivery_status: str = "PENDING"
    provider: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.strategy_version:
            raise DomainError("strategy_version is required")
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))
        if self.sent_at is not None:
            object.__setattr__(self, "sent_at", _require_utc(self.sent_at, "sent_at"))

    @property
    def dedupe_key(self) -> str:
        """``event_date + event_type + strategy_version`` (``ARCHITECTURE.md`` 11)."""
        return f"{self.event_date.isoformat()}|{self.event_type.value}|{self.strategy_version}"

    def delivered(self, provider: str, at: datetime | None = None) -> Self:
        return replace(
            self,
            provider=provider,
            sent_at=at or utcnow(),
            delivery_status="SENT",
            error=None,
        )

    def failed(self, provider: str, error: str) -> Self:
        return replace(self, provider=provider, delivery_status="FAILED", error=error)


# ------------------------------------------------------------- pipeline runs


@dataclass(frozen=True, slots=True)
class PipelineRun:
    """Audit record of one daily worker execution (``BACKTEST_SPEC.md`` 25)."""

    run_id: str
    run_date: date
    started_at: datetime
    status: PipelineStatus = PipelineStatus.SUCCESS
    finished_at: datetime | None = None
    stage: str | None = None
    error_message: str | None = None
    strategy_version: str | None = None
    data_version: str | None = None
    parameter_version: str | None = None
    code_commit: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id:
            raise DomainError("run_id is required")
        object.__setattr__(self, "started_at", _require_utc(self.started_at, "started_at"))
        if self.finished_at is not None:
            object.__setattr__(self, "finished_at", _require_utc(self.finished_at, "finished_at"))

    def completed(self, status: PipelineStatus, *, error: str | None = None) -> Self:
        return replace(self, status=status, finished_at=utcnow(), error_message=error)

    @property
    def succeeded(self) -> bool:
        return self.status is PipelineStatus.SUCCESS


@dataclass(frozen=True, slots=True)
class StrategyVersionRecord:
    """TASK-101 — the manifest of a strategy the system has run."""

    strategy_version: str
    parameter_status: str
    parameter_version: str | None = None
    data_version: str | None = None
    frozen_at: datetime | None = None
    code_commit: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    is_active: bool = False
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.strategy_version:
            raise DomainError("strategy_version is required")
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))
        if self.frozen_at is not None:
            object.__setattr__(self, "frozen_at", _require_utc(self.frozen_at, "frozen_at"))


@dataclass(frozen=True, slots=True)
class DataQualityFinding:
    """TASK-028 — a cross-validation disagreement, recorded rather than repaired.

    ``BACKTEST_SPEC.md`` 5.5 forbids silently "fixing" a mismatch, so findings
    are their own durable records that a human reviews.
    """

    symbol: str
    observation_date: date
    check_name: str
    status: DataQualityStatus
    detail: str
    primary_value: float | None = None
    reference_value: float | None = None
    reference_source: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.status is DataQualityStatus.OK:
            raise DomainError("a finding records a problem; OK is not a finding")
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))

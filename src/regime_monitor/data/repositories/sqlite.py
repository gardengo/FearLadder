"""SQLite adapters for the repository ports (TASK-011).

Every write is an UPSERT on the table's natural key, so re-running the daily
worker for a date that already exists updates that row instead of duplicating it
(``ARCHITECTURE.md`` 11). Alert and regime-event writes are the exception: they
are insert-or-skip, because a *second* notification is exactly what
deduplication has to prevent.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Self

import pandas as pd
from pandas import DataFrame

from regime_monitor.constants import (
    Asset,
    DataQualityStatus,
    EventType,
    PipelineStatus,
)
from regime_monitor.data.models import (
    AlertEvent,
    DataQualityFinding,
    IndicatorScore,
    IndicatorValue,
    MarketObservation,
    MarketState,
    PipelineRun,
    Provenance,
    RegimeEvent,
    StrategyVersionRecord,
    TargetAllocation,
    utcnow,
)
from regime_monitor.data.repositories.connection import (
    checkpoint,
    connect,
    from_db_date,
    from_db_datetime,
    from_json,
    initialize_schema,
    opt_date,
    opt_datetime,
    to_db_date,
    to_db_datetime,
    to_json,
    transaction,
)


class _Base:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @staticmethod
    def _range(column: str, start: date | None, end: date | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            clauses.append(f"{column} >= ?")
            params.append(to_db_date(start))
        if end is not None:
            clauses.append(f"{column} <= ?")
            params.append(to_db_date(end))
        return (" AND " + " AND ".join(clauses) if clauses else ""), params


# ------------------------------------------------------------- observations


class SQLiteMarketObservationRepository(_Base):
    def save_observations(self, observations: list[MarketObservation]) -> int:
        if not observations:
            return 0
        now = to_db_datetime(utcnow())
        rows = [
            (
                obs.symbol,
                to_db_date(obs.observation_date),
                to_db_datetime(obs.availability_datetime),
                obs.open,
                obs.high,
                obs.low,
                obs.close,
                obs.adj_close,
                obs.volume,
                obs.value,
                obs.quality_status.value,
                obs.note,
                obs.provenance.provider_library,
                obs.provenance.provider_library_version,
                obs.provenance.underlying_source,
                obs.provenance.source_ref,
                to_db_datetime(obs.provenance.retrieved_at),
                now,
                now,
            )
            for obs in observations
        ]
        self._connection.executemany(
            """
            INSERT INTO market_observations (
                symbol, observation_date, availability_datetime,
                open, high, low, close, adj_close, volume, value,
                quality_status, note,
                provider_library, provider_library_version, underlying_source,
                source_ref, retrieved_at, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (symbol, observation_date) DO UPDATE SET
                availability_datetime = excluded.availability_datetime,
                open = excluded.open, high = excluded.high, low = excluded.low,
                close = excluded.close, adj_close = excluded.adj_close,
                volume = excluded.volume, value = excluded.value,
                quality_status = excluded.quality_status, note = excluded.note,
                provider_library = excluded.provider_library,
                provider_library_version = excluded.provider_library_version,
                underlying_source = excluded.underlying_source,
                source_ref = excluded.source_ref,
                retrieved_at = excluded.retrieved_at,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        return len(rows)

    def get_observation(self, symbol: str, observation_date: date) -> MarketObservation | None:
        row = self._connection.execute(
            "SELECT * FROM market_observations WHERE symbol = ? AND observation_date = ?",
            (symbol, to_db_date(observation_date)),
        ).fetchone()
        return None if row is None else _observation_from_row(row)

    def get_observations(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
        available_at: datetime | None = None,
    ) -> list[MarketObservation]:
        clause, params = self._range("observation_date", start, end)
        availability = ""
        if available_at is not None:
            availability = " AND availability_datetime <= ?"
            params.append(to_db_datetime(available_at))
        rows = self._connection.execute(
            "SELECT * FROM market_observations WHERE symbol = ?"
            f"{clause}{availability} ORDER BY observation_date",
            [symbol, *params],
        ).fetchall()
        return [_observation_from_row(row) for row in rows]

    def get_series_frame(
        self,
        symbols: list[str],
        *,
        start: date | None = None,
        end: date | None = None,
        available_at: datetime | None = None,
    ) -> DataFrame:
        if not symbols:
            return DataFrame()
        placeholders = ",".join("?" for _ in symbols)
        clause, params = self._range("observation_date", start, end)
        availability = ""
        if available_at is not None:
            availability = " AND availability_datetime <= ?"
            params.append(to_db_datetime(available_at))
        rows = self._connection.execute(
            "SELECT symbol, observation_date, COALESCE(close, value) AS v "
            f"FROM market_observations WHERE symbol IN ({placeholders})"
            f"{clause}{availability} ORDER BY observation_date",
            [*symbols, *params],
        ).fetchall()
        if not rows:
            return DataFrame(columns=symbols)
        frame = DataFrame(
            [(row["symbol"], from_db_date(row["observation_date"]), row["v"]) for row in rows],
            columns=["symbol", "observation_date", "value"],
        )
        wide = frame.pivot(index="observation_date", columns="symbol", values="value")
        wide.columns.name = None
        return wide.reindex(columns=symbols)

    def latest_observation_date(
        self, symbol: str, *, on_or_before: date | None = None
    ) -> date | None:
        sql = "SELECT MAX(observation_date) AS d FROM market_observations WHERE symbol = ?"
        params: list[object] = [symbol]
        if on_or_before is not None:
            sql += " AND observation_date <= ?"
            params.append(on_or_before.isoformat())
        row = self._connection.execute(sql, params).fetchone()
        return opt_date(row["d"]) if row else None

    def save_finding(self, finding: DataQualityFinding) -> None:
        self._connection.execute(
            """
            INSERT INTO data_quality_findings (
                symbol, observation_date, check_name, status, detail,
                primary_value, reference_value, reference_source, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (symbol, observation_date, check_name) DO UPDATE SET
                status = excluded.status,
                detail = excluded.detail,
                primary_value = excluded.primary_value,
                reference_value = excluded.reference_value,
                reference_source = excluded.reference_source
            """,
            (
                finding.symbol,
                to_db_date(finding.observation_date),
                finding.check_name,
                finding.status.value,
                finding.detail,
                finding.primary_value,
                finding.reference_value,
                finding.reference_source,
                to_db_datetime(finding.created_at),
            ),
        )

    def get_open_findings(self, *, symbol: str | None = None) -> list[DataQualityFinding]:
        sql = "SELECT * FROM data_quality_findings WHERE resolved_at IS NULL"
        params: list[Any] = []
        if symbol is not None:
            sql += " AND symbol = ?"
            params.append(symbol)
        rows = self._connection.execute(sql + " ORDER BY observation_date", params).fetchall()
        return [
            DataQualityFinding(
                symbol=row["symbol"],
                observation_date=from_db_date(row["observation_date"]),
                check_name=row["check_name"],
                status=DataQualityStatus(row["status"]),
                detail=row["detail"],
                primary_value=row["primary_value"],
                reference_value=row["reference_value"],
                reference_source=row["reference_source"],
                created_at=from_db_datetime(row["created_at"]),
            )
            for row in rows
        ]


def _observation_from_row(row: sqlite3.Row) -> MarketObservation:
    return MarketObservation(
        symbol=row["symbol"],
        observation_date=from_db_date(row["observation_date"]),
        availability_datetime=from_db_datetime(row["availability_datetime"]),
        provenance=Provenance(
            provider_library=row["provider_library"],
            provider_library_version=row["provider_library_version"],
            underlying_source=row["underlying_source"],
            retrieved_at=from_db_datetime(row["retrieved_at"]),
            source_ref=row["source_ref"],
        ),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        adj_close=row["adj_close"],
        volume=row["volume"],
        value=row["value"],
        quality_status=DataQualityStatus(row["quality_status"]),
        note=row["note"],
    )


# --------------------------------------------------------------- indicators


class SQLiteIndicatorRepository(_Base):
    def save_values(self, values: list[IndicatorValue]) -> int:
        if not values:
            return 0
        now = to_db_datetime(utcnow())
        rows = [
            (
                item.indicator_name,
                to_db_date(item.observation_date),
                item.params_key,
                item.value,
                item.source_symbol,
                None
                if item.availability_datetime is None
                else to_db_datetime(item.availability_datetime),
                item.quality_status.value,
                now,
                now,
            )
            for item in values
        ]
        self._connection.executemany(
            """
            INSERT INTO indicator_values (
                indicator_name, observation_date, params_key, value, source_symbol,
                availability_datetime, quality_status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (indicator_name, observation_date, params_key) DO UPDATE SET
                value = excluded.value,
                source_symbol = excluded.source_symbol,
                availability_datetime = excluded.availability_datetime,
                quality_status = excluded.quality_status,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        return len(rows)

    def get_values(
        self,
        indicator_name: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[IndicatorValue]:
        clause, params = self._range("observation_date", start, end)
        rows = self._connection.execute(
            f"SELECT * FROM indicator_values WHERE indicator_name = ?{clause} "
            "ORDER BY observation_date",
            [indicator_name, *params],
        ).fetchall()
        return [
            IndicatorValue(
                indicator_name=row["indicator_name"],
                observation_date=from_db_date(row["observation_date"]),
                value=row["value"],
                source_symbol=row["source_symbol"],
                params=from_json(row["params_key"], {}),
                availability_datetime=opt_datetime(row["availability_datetime"]),
                quality_status=DataQualityStatus(row["quality_status"]),
            )
            for row in rows
        ]

    def save_scores(self, scores: list[IndicatorScore], *, strategy_version: str) -> int:
        if not scores:
            return 0
        now = to_db_datetime(utcnow())
        rows = [
            (
                score.indicator_name,
                to_db_date(score.observation_date),
                strategy_version,
                score.score,
                score.raw_value,
                score.normalization_method,
                score.normalization_window,
                score.quality_status.value,
                now,
                now,
            )
            for score in scores
        ]
        self._connection.executemany(
            """
            INSERT INTO indicator_scores (
                indicator_name, observation_date, strategy_version, score, raw_value,
                normalization_method, normalization_window, quality_status,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (indicator_name, observation_date, strategy_version) DO UPDATE SET
                score = excluded.score,
                raw_value = excluded.raw_value,
                normalization_method = excluded.normalization_method,
                normalization_window = excluded.normalization_window,
                quality_status = excluded.quality_status,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        return len(rows)

    def get_scores(self, observation_date: date, *, strategy_version: str) -> list[IndicatorScore]:
        rows = self._connection.execute(
            "SELECT * FROM indicator_scores WHERE observation_date = ? AND strategy_version = ? "
            "ORDER BY indicator_name",
            (to_db_date(observation_date), strategy_version),
        ).fetchall()
        return [_score_from_row(row) for row in rows]

    def get_score_history(
        self,
        indicator_name: str,
        *,
        strategy_version: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[IndicatorScore]:
        clause, params = self._range("observation_date", start, end)
        rows = self._connection.execute(
            "SELECT * FROM indicator_scores WHERE indicator_name = ? AND strategy_version = ?"
            f"{clause} ORDER BY observation_date",
            [indicator_name, strategy_version, *params],
        ).fetchall()
        return [_score_from_row(row) for row in rows]


def _score_from_row(row: sqlite3.Row) -> IndicatorScore:
    return IndicatorScore(
        indicator_name=row["indicator_name"],
        observation_date=from_db_date(row["observation_date"]),
        score=row["score"],
        normalization_method=row["normalization_method"],
        normalization_window=row["normalization_window"],
        raw_value=row["raw_value"],
        quality_status=DataQualityStatus(row["quality_status"]),
    )


# ------------------------------------------------------------- market states


class SQLiteMarketStateRepository(_Base):
    def save_state(self, state: MarketState, allocation: TargetAllocation | None = None) -> int:
        if allocation is not None:
            if allocation.observation_date != state.observation_date:
                raise ValueError("allocation date does not match the state date")
            if state.is_unknown:
                raise ValueError("an UNKNOWN state must not be given an allocation")
        now = to_db_datetime(utcnow())
        self._connection.execute(
            """
            INSERT INTO market_states (
                observation_date, strategy_version, composite_score, regime, raw_regime,
                previous_regime, previous_score, target_leverage, data_quality_status,
                reason_codes, score_breakdown, data_version, parameter_version,
                code_commit, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (observation_date, strategy_version) DO UPDATE SET
                composite_score = excluded.composite_score,
                regime = excluded.regime,
                raw_regime = excluded.raw_regime,
                previous_regime = excluded.previous_regime,
                previous_score = excluded.previous_score,
                target_leverage = excluded.target_leverage,
                data_quality_status = excluded.data_quality_status,
                reason_codes = excluded.reason_codes,
                score_breakdown = excluded.score_breakdown,
                data_version = excluded.data_version,
                parameter_version = excluded.parameter_version,
                code_commit = excluded.code_commit,
                updated_at = excluded.updated_at
            """,
            (
                to_db_date(state.observation_date),
                state.strategy_version,
                state.composite_score,
                state.regime,
                state.raw_regime,
                state.previous_regime,
                state.previous_score,
                state.target_leverage,
                state.data_quality_status.value,
                to_json(list(state.reason_codes)),
                to_json(state.score_breakdown),
                state.data_version,
                state.parameter_version,
                state.code_commit,
                now,
                now,
            ),
        )
        row = self._connection.execute(
            "SELECT id FROM market_states WHERE observation_date = ? AND strategy_version = ?",
            (to_db_date(state.observation_date), state.strategy_version),
        ).fetchone()
        state_id = int(row["id"])

        # A re-run may legitimately change the allocation, and a re-run that
        # produces UNKNOWN must leave no stale advice behind.
        self._connection.execute(
            "DELETE FROM target_allocations WHERE observation_date = ? AND strategy_version = ?",
            (to_db_date(state.observation_date), state.strategy_version),
        )
        if allocation is not None:
            self._connection.executemany(
                """
                INSERT INTO target_allocations (
                    market_state_id, observation_date, strategy_version, asset, weight,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                [
                    (
                        state_id,
                        to_db_date(allocation.observation_date),
                        allocation.strategy_version,
                        asset.value,
                        weight,
                        now,
                        now,
                    )
                    for asset, weight in allocation.weights.items()
                ],
            )
        return state_id

    def get_state(self, observation_date: date, *, strategy_version: str) -> MarketState | None:
        row = self._connection.execute(
            "SELECT * FROM market_states WHERE observation_date = ? AND strategy_version = ?",
            (to_db_date(observation_date), strategy_version),
        ).fetchone()
        return None if row is None else _state_from_row(row)

    def get_latest_state(self, *, strategy_version: str | None = None) -> MarketState | None:
        sql = "SELECT * FROM market_states"
        params: list[Any] = []
        if strategy_version is not None:
            sql += " WHERE strategy_version = ?"
            params.append(strategy_version)
        row = self._connection.execute(
            sql + " ORDER BY observation_date DESC LIMIT 1", params
        ).fetchone()
        return None if row is None else _state_from_row(row)

    def get_previous_state(
        self,
        before: date,
        *,
        strategy_version: str,
        exclude_unknown: bool = True,
    ) -> MarketState | None:
        sql = (
            "SELECT * FROM market_states "
            "WHERE observation_date < ? AND strategy_version = ?"
        )
        if exclude_unknown:
            sql += " AND regime <> 'UNKNOWN'"
        row = self._connection.execute(
            sql + " ORDER BY observation_date DESC LIMIT 1",
            (to_db_date(before), strategy_version),
        ).fetchone()
        return None if row is None else _state_from_row(row)

    def get_state_history(
        self,
        *,
        strategy_version: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[MarketState]:
        clause, params = self._range("observation_date", start, end)
        rows = self._connection.execute(
            f"SELECT * FROM market_states WHERE strategy_version = ?{clause} "
            "ORDER BY observation_date",
            [strategy_version, *params],
        ).fetchall()
        return [_state_from_row(row) for row in rows]

    def get_allocation(
        self, observation_date: date, *, strategy_version: str
    ) -> TargetAllocation | None:
        rows = self._connection.execute(
            "SELECT a.asset, a.weight, s.regime FROM target_allocations a "
            "JOIN market_states s ON s.id = a.market_state_id "
            "WHERE a.observation_date = ? AND a.strategy_version = ?",
            (to_db_date(observation_date), strategy_version),
        ).fetchall()
        if not rows:
            return None
        return TargetAllocation(
            observation_date=observation_date,
            weights={Asset(row["asset"]): row["weight"] for row in rows},
            strategy_version=strategy_version,
            regime=rows[0]["regime"],
        )


def _state_from_row(row: sqlite3.Row) -> MarketState:
    return MarketState(
        observation_date=from_db_date(row["observation_date"]),
        composite_score=row["composite_score"],
        regime=row["regime"],
        raw_regime=row["raw_regime"],
        previous_regime=row["previous_regime"],
        previous_score=row["previous_score"],
        target_leverage=row["target_leverage"],
        data_quality_status=DataQualityStatus(row["data_quality_status"]),
        reason_codes=tuple(from_json(row["reason_codes"], [])),
        score_breakdown=from_json(row["score_breakdown"], {}),
        strategy_version=row["strategy_version"],
        data_version=row["data_version"],
        parameter_version=row["parameter_version"],
        code_commit=row["code_commit"],
        created_at=from_db_datetime(row["created_at"]),
    )


# ------------------------------------------------------------------- events


class SQLiteEventRepository(_Base):
    def save_regime_event(self, event: RegimeEvent) -> bool:
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO regime_events (
                event_date, strategy_version, previous_regime, new_regime,
                previous_score, new_score, reason_codes, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                to_db_date(event.event_date),
                event.strategy_version,
                event.previous_regime,
                event.new_regime,
                event.previous_score,
                event.new_score,
                to_json(list(event.reason_codes)),
                to_db_datetime(event.created_at),
            ),
        )
        return cursor.rowcount == 1

    def get_regime_events(
        self,
        *,
        strategy_version: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[RegimeEvent]:
        clause, params = self._range("event_date", start, end)
        sql = "SELECT * FROM regime_events WHERE 1=1"
        if strategy_version is not None:
            sql += " AND strategy_version = ?"
            params.insert(0, strategy_version)
        rows = self._connection.execute(
            sql + clause + " ORDER BY event_date", params
        ).fetchall()
        return [
            RegimeEvent(
                event_date=from_db_date(row["event_date"]),
                previous_regime=row["previous_regime"],
                new_regime=row["new_regime"],
                previous_score=row["previous_score"],
                new_score=row["new_score"],
                reason_codes=tuple(from_json(row["reason_codes"], [])),
                strategy_version=row["strategy_version"],
                created_at=from_db_datetime(row["created_at"]),
            )
            for row in rows
        ]

    def save_alert(self, alert: AlertEvent) -> bool:
        """Insert-or-skip. ``False`` means this alert was already recorded."""
        now = to_db_datetime(utcnow())
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO alert_events (
                dedupe_key, event_date, event_type, strategy_version, severity,
                title, body, payload, delivery_status, provider, error, sent_at,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                alert.dedupe_key,
                to_db_date(alert.event_date),
                alert.event_type.value,
                alert.strategy_version,
                alert.severity,
                alert.title,
                alert.body,
                to_json(alert.payload),
                alert.delivery_status,
                alert.provider,
                alert.error,
                None if alert.sent_at is None else to_db_datetime(alert.sent_at),
                to_db_datetime(alert.created_at),
                now,
            ),
        )
        return cursor.rowcount == 1

    def get_alert(self, dedupe_key: str) -> AlertEvent | None:
        row = self._connection.execute(
            "SELECT * FROM alert_events WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return None if row is None else _alert_from_row(row)

    def get_pending_alerts(self) -> list[AlertEvent]:
        rows = self._connection.execute(
            "SELECT * FROM alert_events WHERE delivery_status = 'PENDING' ORDER BY created_at"
        ).fetchall()
        return [_alert_from_row(row) for row in rows]

    def mark_alert_delivered(self, alert: AlertEvent) -> None:
        self._connection.execute(
            "UPDATE alert_events SET delivery_status = ?, provider = ?, sent_at = ?, "
            "error = ?, updated_at = ? WHERE dedupe_key = ?",
            (
                alert.delivery_status,
                alert.provider,
                None if alert.sent_at is None else to_db_datetime(alert.sent_at),
                alert.error,
                to_db_datetime(utcnow()),
                alert.dedupe_key,
            ),
        )

    def last_alert_date(self, event_type: EventType, *, strategy_version: str) -> date | None:
        row = self._connection.execute(
            "SELECT MAX(event_date) AS d FROM alert_events "
            "WHERE event_type = ? AND strategy_version = ? AND delivery_status = 'SENT'",
            (event_type.value, strategy_version),
        ).fetchone()
        return opt_date(row["d"]) if row else None

    def get_alerts(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        event_type: EventType | None = None,
    ) -> list[AlertEvent]:
        clause, params = self._range("event_date", start, end)
        sql = "SELECT * FROM alert_events WHERE 1=1"
        if event_type is not None:
            sql += " AND event_type = ?"
            params.insert(0, event_type.value)
        rows = self._connection.execute(
            sql + clause + " ORDER BY event_date DESC, id DESC", params
        ).fetchall()
        return [_alert_from_row(row) for row in rows]


def _alert_from_row(row: sqlite3.Row) -> AlertEvent:
    return AlertEvent(
        event_date=from_db_date(row["event_date"]),
        event_type=EventType(row["event_type"]),
        severity=row["severity"],
        title=row["title"],
        body=row["body"],
        strategy_version=row["strategy_version"],
        payload=from_json(row["payload"], {}),
        created_at=from_db_datetime(row["created_at"]),
        sent_at=opt_datetime(row["sent_at"]),
        delivery_status=row["delivery_status"],
        provider=row["provider"],
        error=row["error"],
    )


# --------------------------------------------------------------- strategies


class SQLiteStrategyRepository(_Base):
    def save_version(self, record: StrategyVersionRecord) -> None:
        now = to_db_datetime(utcnow())
        self._connection.execute(
            """
            INSERT INTO strategy_versions (
                strategy_version, parameter_status, parameter_version, data_version,
                frozen_at, code_commit, manifest, is_active, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (strategy_version) DO UPDATE SET
                parameter_status = excluded.parameter_status,
                parameter_version = excluded.parameter_version,
                data_version = excluded.data_version,
                frozen_at = excluded.frozen_at,
                code_commit = excluded.code_commit,
                manifest = excluded.manifest,
                updated_at = excluded.updated_at
            """,
            (
                record.strategy_version,
                record.parameter_status,
                record.parameter_version,
                record.data_version,
                None if record.frozen_at is None else to_db_datetime(record.frozen_at),
                record.code_commit,
                to_json(record.manifest),
                int(record.is_active),
                to_db_datetime(record.created_at),
                now,
            ),
        )
        if record.is_active:
            self.activate(record.strategy_version)

    def get_version(self, strategy_version: str) -> StrategyVersionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM strategy_versions WHERE strategy_version = ?", (strategy_version,)
        ).fetchone()
        return None if row is None else _version_from_row(row)

    def get_active_version(self) -> StrategyVersionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM strategy_versions WHERE is_active = 1"
        ).fetchone()
        return None if row is None else _version_from_row(row)

    def activate(self, strategy_version: str) -> None:
        """Make one version active. The partial unique index allows only one."""
        self._connection.execute(
            "UPDATE strategy_versions SET is_active = 0, updated_at = ? WHERE is_active = 1",
            (to_db_datetime(utcnow()),),
        )
        cursor = self._connection.execute(
            "UPDATE strategy_versions SET is_active = 1, updated_at = ? WHERE strategy_version = ?",
            (to_db_datetime(utcnow()), strategy_version),
        )
        if cursor.rowcount == 0:
            raise LookupError(f"unknown strategy_version {strategy_version!r}")

    def save_run(self, run: PipelineRun) -> None:
        now = to_db_datetime(utcnow())
        self._connection.execute(
            """
            INSERT INTO pipeline_runs (
                run_id, run_date, status, stage, started_at, finished_at, error_message,
                strategy_version, data_version, parameter_version, code_commit,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (run_id) DO UPDATE SET
                status = excluded.status,
                stage = excluded.stage,
                finished_at = excluded.finished_at,
                error_message = excluded.error_message,
                strategy_version = excluded.strategy_version,
                data_version = excluded.data_version,
                parameter_version = excluded.parameter_version,
                code_commit = excluded.code_commit,
                updated_at = excluded.updated_at
            """,
            (
                run.run_id,
                to_db_date(run.run_date),
                run.status.value,
                run.stage,
                to_db_datetime(run.started_at),
                None if run.finished_at is None else to_db_datetime(run.finished_at),
                run.error_message,
                run.strategy_version,
                run.data_version,
                run.parameter_version,
                run.code_commit,
                now,
                now,
            ),
        )

    def get_run(self, run_id: str) -> PipelineRun | None:
        row = self._connection.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _run_from_row(row)

    def get_runs(self, *, run_date: date | None = None, limit: int = 50) -> list[PipelineRun]:
        sql = "SELECT * FROM pipeline_runs"
        params: list[Any] = []
        if run_date is not None:
            sql += " WHERE run_date = ?"
            params.append(to_db_date(run_date))
        rows = self._connection.execute(
            sql + " ORDER BY started_at DESC LIMIT ?", [*params, limit]
        ).fetchall()
        return [_run_from_row(row) for row in rows]

    def last_successful_run(self) -> PipelineRun | None:
        row = self._connection.execute(
            "SELECT * FROM pipeline_runs WHERE status = 'SUCCESS' "
            "ORDER BY run_date DESC, started_at DESC LIMIT 1"
        ).fetchone()
        return None if row is None else _run_from_row(row)


def _version_from_row(row: sqlite3.Row) -> StrategyVersionRecord:
    return StrategyVersionRecord(
        strategy_version=row["strategy_version"],
        parameter_status=row["parameter_status"],
        parameter_version=row["parameter_version"],
        data_version=row["data_version"],
        frozen_at=opt_datetime(row["frozen_at"]),
        code_commit=row["code_commit"],
        manifest=from_json(row["manifest"], {}),
        is_active=bool(row["is_active"]),
        created_at=from_db_datetime(row["created_at"]),
    )


def _run_from_row(row: sqlite3.Row) -> PipelineRun:
    return PipelineRun(
        run_id=row["run_id"],
        run_date=from_db_date(row["run_date"]),
        status=PipelineStatus(row["status"]),
        stage=row["stage"],
        started_at=from_db_datetime(row["started_at"]),
        finished_at=opt_datetime(row["finished_at"]),
        error_message=row["error_message"],
        strategy_version=row["strategy_version"],
        data_version=row["data_version"],
        parameter_version=row["parameter_version"],
        code_commit=row["code_commit"],
    )


# --------------------------------------------------------------- unit of work


class SQLiteUnitOfWork:
    """One transaction spanning every repository.

    The daily worker writes state, allocation, events and alerts inside a single
    unit of work so a mid-pipeline failure cannot leave a half-written day
    behind.
    """

    def __init__(self, db_path: Path | str | None = None, *, create: bool = True) -> None:
        self._db_path = db_path
        self._create = create
        self._connection: sqlite3.Connection | None = None
        self._transaction: Any = None

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> Self:
        self._connection = connect(self._db_path)
        if self._create:
            initialize_schema(self._connection)
        self._transaction = transaction(self._connection)
        self._transaction.__enter__()
        self._bind()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        assert self._transaction is not None
        assert self._connection is not None
        try:
            self._transaction.__exit__(exc_type, exc, tb)
            if exc_type is None:
                # Fold the WAL back into the .db file now that the transaction
                # is closed. The daily workflow commits this file to git, so it
                # has to be complete on its own (ARCHITECTURE.md 8). A
                # checkpoint inside the transaction would fail with
                # "database table is locked".
                checkpoint(self._connection)
        finally:
            self._connection.close()
            self._connection = None
            self._transaction = None

    def _bind(self) -> None:
        assert self._connection is not None
        self.observations = SQLiteMarketObservationRepository(self._connection)
        self.indicators = SQLiteIndicatorRepository(self._connection)
        self.states = SQLiteMarketStateRepository(self._connection)
        self.events = SQLiteEventRepository(self._connection)
        self.strategies = SQLiteStrategyRepository(self._connection)

    # -- explicit control --------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("unit of work is not active")
        return self._connection

    def commit(self) -> None:
        """Commit now and continue in a fresh transaction."""
        self.connection.execute("COMMIT")
        self.connection.execute("BEGIN IMMEDIATE")

    def rollback(self) -> None:
        """Discard everything since the last commit and continue."""
        self.connection.execute("ROLLBACK")
        self.connection.execute("BEGIN IMMEDIATE")


def create_database(db_path: Path | str | None = None) -> Path:
    """Create the database file and schema if they do not exist yet."""
    connection = connect(db_path)
    try:
        initialize_schema(connection)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    from regime_monitor import paths as _paths

    return Path(db_path) if db_path is not None else _paths.default_db_path()


def read_frame(connection: sqlite3.Connection, sql: str, params: Any = ()) -> DataFrame:
    """Read-only helper for the dashboard layer."""
    return pd.read_sql_query(sql, connection, params=params)


__all__ = [
    "SQLiteEventRepository",
    "SQLiteIndicatorRepository",
    "SQLiteMarketObservationRepository",
    "SQLiteMarketStateRepository",
    "SQLiteStrategyRepository",
    "SQLiteUnitOfWork",
    "create_database",
    "read_frame",
]

"""Market observations: prices, scalar series and data-quality findings.

See :mod:`fear_ladder.data.repositories.sqlite` for the write semantics
every repository here shares.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from pandas import DataFrame

from fear_ladder.constants import (
    DataQualityStatus,
)
from fear_ladder.data.models import (
    DataQualityFinding,
    MarketObservation,
    Provenance,
    utcnow,
)
from fear_ladder.data.repositories.base import _Base
from fear_ladder.data.repositories.connection import (
    from_db_date,
    from_db_datetime,
    opt_date,
    to_db_date,
    to_db_datetime,
)


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

"""Regime changes and the alerts they produced.

See :mod:`fear_ladder.data.repositories.sqlite` for the write semantics
every repository here shares.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from fear_ladder.constants import (
    EventType,
)
from fear_ladder.data.models import (
    AlertEvent,
    RegimeEvent,
    utcnow,
)
from fear_ladder.data.repositories.base import _Base
from fear_ladder.data.repositories.connection import (
    from_db_date,
    from_db_datetime,
    from_json,
    opt_date,
    opt_datetime,
    to_db_date,
    to_db_datetime,
    to_json,
)


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

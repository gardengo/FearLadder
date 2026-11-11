"""The transition engine's carried state, one row per stepped day.

See the table comment in ``schema.sql``. This repository upserts like its
neighbours: re-running a day rewrites that day's row with the same values, and
a deliberate replay of a past day is allowed to restate it.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from fear_ladder.data.models import RegimeMemory, utcnow
from fear_ladder.data.repositories.base import _Base
from fear_ladder.data.repositories.connection import (
    from_db_date,
    to_db_date,
    to_db_datetime,
)


class SQLiteRegimeMemoryRepository(_Base):
    def save(self, entries: list[RegimeMemory]) -> None:
        now = to_db_datetime(utcnow())
        self._connection.executemany(
            """
            INSERT INTO regime_memory (
                observation_date, strategy_version, regime, since, days_held,
                pending_regime, pending_days, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (observation_date, strategy_version) DO UPDATE SET
                regime = excluded.regime,
                since = excluded.since,
                days_held = excluded.days_held,
                pending_regime = excluded.pending_regime,
                pending_days = excluded.pending_days,
                updated_at = excluded.updated_at
            """,
            [
                (
                    to_db_date(entry.observation_date),
                    entry.strategy_version,
                    entry.regime,
                    to_db_date(entry.since),
                    entry.days_held,
                    entry.pending_regime,
                    entry.pending_days,
                    now,
                    now,
                )
                for entry in entries
            ],
        )

    def latest_before(self, day: date, *, strategy_version: str) -> RegimeMemory | None:
        row = self._connection.execute(
            "SELECT * FROM regime_memory WHERE strategy_version = ? "
            "AND observation_date < ? ORDER BY observation_date DESC LIMIT 1",
            (strategy_version, to_db_date(day)),
        ).fetchone()
        return None if row is None else _memory_from_row(row)

    def get(self, day: date, *, strategy_version: str) -> RegimeMemory | None:
        row = self._connection.execute(
            "SELECT * FROM regime_memory WHERE strategy_version = ? "
            "AND observation_date = ?",
            (strategy_version, to_db_date(day)),
        ).fetchone()
        return None if row is None else _memory_from_row(row)


def _memory_from_row(row: sqlite3.Row) -> RegimeMemory:
    return RegimeMemory(
        observation_date=from_db_date(row["observation_date"]),
        strategy_version=row["strategy_version"],
        regime=row["regime"],
        since=from_db_date(row["since"]),
        days_held=row["days_held"],
        pending_regime=row["pending_regime"],
        pending_days=row["pending_days"],
    )

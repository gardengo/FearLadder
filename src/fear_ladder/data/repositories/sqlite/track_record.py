"""The paper track record: what following the stored targets has been worth.

Unlike its neighbours this repository does not upsert. See the table comment in
``schema.sql`` — a row is written once and a re-run leaves it alone.
"""

from __future__ import annotations

import sqlite3

from fear_ladder.constants import Asset
from fear_ladder.data.models import TrackRecordEntry, utcnow
from fear_ladder.data.repositories.base import _Base
from fear_ladder.data.repositories.connection import (
    from_db_date,
    from_json,
    to_db_date,
    to_db_datetime,
    to_json,
)


class SQLiteTrackRecordRepository(_Base):
    def append(self, entries: list[TrackRecordEntry]) -> int:
        """Insert the days not stored yet. Returns how many were new."""
        now = to_db_datetime(utcnow())
        before = self._connection.total_changes
        self._connection.executemany(
            """
            INSERT INTO track_record (
                observation_date, strategy_version, strategy_nav, benchmark_nav,
                holdings, executed_target, turnover, cost, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (observation_date, strategy_version) DO NOTHING
            """,
            [
                (
                    to_db_date(entry.observation_date),
                    entry.strategy_version,
                    entry.strategy_nav,
                    entry.benchmark_nav,
                    to_json(_plain(entry.holdings)),
                    None
                    if entry.executed_target is None
                    else to_json(_plain(entry.executed_target)),
                    entry.turnover,
                    entry.cost,
                    now,
                )
                for entry in entries
            ],
        )
        return self._connection.total_changes - before

    def latest(self, *, strategy_version: str) -> TrackRecordEntry | None:
        row = self._connection.execute(
            "SELECT * FROM track_record WHERE strategy_version = ? "
            "ORDER BY observation_date DESC LIMIT 1",
            (strategy_version,),
        ).fetchone()
        return None if row is None else _entry_from_row(row)

    def history(self, *, strategy_version: str) -> list[TrackRecordEntry]:
        rows = self._connection.execute(
            "SELECT * FROM track_record WHERE strategy_version = ? ORDER BY observation_date",
            (strategy_version,),
        ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def clear(self, *, strategy_version: str) -> int:
        """Delete one strategy's record, so it can be rebuilt from stored prices."""
        return self._connection.execute(
            "DELETE FROM track_record WHERE strategy_version = ?", (strategy_version,)
        ).rowcount


def _plain(weights: dict[Asset, float]) -> dict[str, float]:
    return {Asset(asset).value: float(weight) for asset, weight in weights.items()}


def _weights(raw: str | None) -> dict[Asset, float]:
    return {Asset(name): float(weight) for name, weight in from_json(raw, {}).items()}


def _entry_from_row(row: sqlite3.Row) -> TrackRecordEntry:
    return TrackRecordEntry(
        observation_date=from_db_date(row["observation_date"]),
        strategy_version=row["strategy_version"],
        strategy_nav=row["strategy_nav"],
        benchmark_nav=row["benchmark_nav"],
        holdings=_weights(row["holdings"]),
        executed_target=None
        if row["executed_target"] is None
        else _weights(row["executed_target"]),
        turnover=row["turnover"],
        cost=row["cost"],
    )

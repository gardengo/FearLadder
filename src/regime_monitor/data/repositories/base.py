"""Shared base for the SQLite repository adapters."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from regime_monitor.data.repositories.connection import to_db_date


class _Base:
    """A repository bound to one open connection.

    Repositories never open or close connections themselves; the unit of work
    owns the transaction, so every write in a day either lands together or not
    at all.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @staticmethod
    def _range(column: str, start: date | None, end: date | None) -> tuple[str, list[Any]]:
        """An optional ``BETWEEN``-style clause and its parameters."""
        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            clauses.append(f"{column} >= ?")
            params.append(to_db_date(start))
        if end is not None:
            clauses.append(f"{column} <= ?")
            params.append(to_db_date(end))
        return (" AND " + " AND ".join(clauses) if clauses else ""), params

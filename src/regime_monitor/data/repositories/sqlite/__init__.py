"""SQLite adapters for the repository ports (TASK-011).

Every write is an UPSERT on the table's natural key, so re-running the daily
worker for a date that already exists updates that row instead of duplicating it
(``ARCHITECTURE.md`` §11). Alert and regime-event writes are the exception: they
are insert-or-skip, because a *second* notification is exactly what
deduplication has to prevent.

One module per table group; this file is the seam every caller imports through,
so splitting them changed no import anywhere else in the project.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Self

import pandas as pd
from pandas import DataFrame

from regime_monitor.data.repositories.connection import (
    checkpoint,
    connect,
    initialize_schema,
    transaction,
)
from regime_monitor.data.repositories.sqlite.events import SQLiteEventRepository
from regime_monitor.data.repositories.sqlite.indicators import SQLiteIndicatorRepository
from regime_monitor.data.repositories.sqlite.observations import (
    SQLiteMarketObservationRepository,
)
from regime_monitor.data.repositories.sqlite.states import SQLiteMarketStateRepository
from regime_monitor.data.repositories.sqlite.strategies import SQLiteStrategyRepository


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

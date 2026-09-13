"""SQLite connection management and schema bootstrap (TASK-010)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fear_ladder import paths

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1
SCHEMA_DESCRIPTION = "initial schema (TASK-010)"


def connect(db_path: Path | str | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection with the pragmas this project relies on.

    ``WAL`` keeps the Streamlit dashboard readable while the daily worker writes.
    The worker commits the ``.db`` file to git, so the ``-wal``/``-shm``
    sidecars are gitignored and checkpointed on close.
    """
    target = Path(db_path) if db_path is not None else paths.default_db_path()
    if not read_only:
        target.parent.mkdir(parents=True, exist_ok=True)

    if read_only:
        uri = f"file:{target.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    else:
        connection = sqlite3.connect(target, timeout=30.0, isolation_level=None)

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create every table/index if missing and stamp the schema version."""
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at, description) "
        "VALUES (?, ?, ?)",
        (SCHEMA_VERSION, to_db_datetime(datetime.now(tz=UTC)), SCHEMA_DESCRIPTION),
    )


def current_schema_version(connection: sqlite3.Connection) -> int | None:
    row = connection.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return None if row is None else row["v"]


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction with rollback on any exception (TASK-012)."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def checkpoint(connection: sqlite3.Connection) -> None:
    """Fold the WAL back into the .db file so git sees a complete database."""
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# ------------------------------------------------------------- value adapters
# SQLite has no date/datetime type, so conversion is explicit and centralised
# rather than relying on the deprecated sqlite3 default adapters.


def to_db_date(value: date) -> str:
    return value.isoformat()


def from_db_date(value: str) -> date:
    return date.fromisoformat(value)


def to_db_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("refusing to store a naive datetime")
    return value.astimezone(UTC).isoformat()


def from_db_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def opt_date(value: str | None) -> date | None:
    return None if value is None else from_db_date(value)


def opt_datetime(value: str | None) -> datetime | None:
    return None if value is None else from_db_datetime(value)


def to_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def from_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)

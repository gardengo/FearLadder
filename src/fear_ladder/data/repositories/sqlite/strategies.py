"""Pipeline runs and the registry of strategy versions.

See :mod:`fear_ladder.data.repositories.sqlite` for the write semantics
every repository here shares.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from fear_ladder.constants import (
    PipelineStatus,
)
from fear_ladder.data.models import (
    PipelineRun,
    StrategyVersionRecord,
    utcnow,
)
from fear_ladder.data.repositories.base import _Base
from fear_ladder.data.repositories.connection import (
    from_db_date,
    from_db_datetime,
    from_json,
    opt_datetime,
    to_db_date,
    to_db_datetime,
    to_json,
)


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

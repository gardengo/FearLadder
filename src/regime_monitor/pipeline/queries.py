"""Read-only queries for the dashboard (TASK-140 .. TASK-143).

``ARCHITECTURE.md`` §4.2 is firm about the dashboard's role::

    Streamlit 요청 처리에서 새로운 전략 계산을 수행하는 것을
    기본 동작으로 하지 않는다.

So this module only reads what the daily worker already computed. It never
imports an engine, never recomputes a score, and opens the database read-only so
a dashboard session cannot lock out or corrupt the worker.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from pandas import DataFrame

from regime_monitor import paths
from regime_monitor.constants import UNKNOWN_REGIME

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_DAYS = 365 * 3


class DashboardDataError(RuntimeError):
    """Raised when the dashboard cannot read the state it needs."""


@contextmanager
def read_only(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the operational database without being able to change it."""
    target = db_path or paths.default_db_path()
    if not target.is_file():
        raise DashboardDataError(
            f"no database at {target}. Run scripts/daily_runner.py, or point "
            "REGIME_MONITOR_DB at an existing file."
        )
    connection = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class DashboardQueries:
    """Everything the dashboard reads, in one place."""

    db_path: Path | None = None

    # -- current ----------------------------------------------------------
    def active_strategy_version(self) -> str | None:
        with read_only(self.db_path) as connection:
            row = connection.execute(
                "SELECT strategy_version FROM strategy_versions WHERE is_active = 1"
            ).fetchone()
            if row:
                return str(row["strategy_version"])
            # Nothing marked active yet: fall back to whatever most recently
            # produced a state, so a fresh install still shows something.
            row = connection.execute(
                "SELECT strategy_version FROM market_states "
                "ORDER BY observation_date DESC LIMIT 1"
            ).fetchone()
            return str(row["strategy_version"]) if row else None

    def latest_state(self, strategy_version: str | None = None) -> dict[str, object] | None:
        sql = "SELECT * FROM market_states"
        params: list[object] = []
        if strategy_version:
            sql += " WHERE strategy_version = ?"
            params.append(strategy_version)
        with read_only(self.db_path) as connection:
            row = connection.execute(
                sql + " ORDER BY observation_date DESC LIMIT 1", params
            ).fetchone()
        return dict(row) if row else None

    def latest_allocation(self, strategy_version: str | None = None) -> DataFrame:
        state = self.latest_state(strategy_version)
        if state is None:
            return DataFrame(columns=["asset", "weight"])
        with read_only(self.db_path) as connection:
            return pd.read_sql_query(
                "SELECT asset, weight FROM target_allocations "
                "WHERE observation_date = ? AND strategy_version = ? "
                "ORDER BY weight DESC",
                connection,
                params=(state["observation_date"], state["strategy_version"]),
            )

    def indicator_scores(
        self, observation_date: str, strategy_version: str
    ) -> DataFrame:
        with read_only(self.db_path) as connection:
            return pd.read_sql_query(
                "SELECT indicator_name, score, raw_value, normalization_method, "
                "normalization_window, quality_status FROM indicator_scores "
                "WHERE observation_date = ? AND strategy_version = ? "
                "ORDER BY indicator_name",
                connection,
                params=(observation_date, strategy_version),
            )

    # -- history ----------------------------------------------------------
    def state_history(
        self,
        strategy_version: str | None = None,
        *,
        days: int = DEFAULT_HISTORY_DAYS,
    ) -> DataFrame:
        start = (date.today() - timedelta(days=days)).isoformat()
        sql = (
            "SELECT observation_date, composite_score, regime, raw_regime, "
            "target_leverage, data_quality_status, strategy_version "
            "FROM market_states WHERE observation_date >= ?"
        )
        params: list[object] = [start]
        if strategy_version:
            sql += " AND strategy_version = ?"
            params.append(strategy_version)
        with read_only(self.db_path) as connection:
            frame = pd.read_sql_query(
                sql + " ORDER BY observation_date", connection, params=params
            )
        return _with_dates(frame)

    def price_history(
        self, symbols: tuple[str, ...] = ("QQQ",), *, days: int = DEFAULT_HISTORY_DAYS
    ) -> DataFrame:
        start = (date.today() - timedelta(days=days)).isoformat()
        placeholders = ",".join("?" for _ in symbols)
        with read_only(self.db_path) as connection:
            frame = pd.read_sql_query(
                "SELECT symbol, observation_date, COALESCE(close, value) AS value "
                f"FROM market_observations WHERE symbol IN ({placeholders}) "
                "AND observation_date >= ? ORDER BY observation_date",
                connection,
                params=(*symbols, start),
            )
        if frame.empty:
            return DataFrame()
        wide = frame.pivot(index="observation_date", columns="symbol", values="value")
        wide.index = pd.to_datetime(wide.index)
        wide.columns.name = None
        return wide

    def indicator_history(
        self,
        indicator_name: str,
        strategy_version: str,
        *,
        days: int = DEFAULT_HISTORY_DAYS,
    ) -> DataFrame:
        start = (date.today() - timedelta(days=days)).isoformat()
        with read_only(self.db_path) as connection:
            frame = pd.read_sql_query(
                "SELECT observation_date, score, raw_value FROM indicator_scores "
                "WHERE indicator_name = ? AND strategy_version = ? "
                "AND observation_date >= ? ORDER BY observation_date",
                connection,
                params=(indicator_name, strategy_version, start),
            )
        return _with_dates(frame)

    def available_indicators(self, strategy_version: str) -> list[str]:
        with read_only(self.db_path) as connection:
            rows = connection.execute(
                "SELECT DISTINCT indicator_name FROM indicator_scores "
                "WHERE strategy_version = ? ORDER BY indicator_name",
                (strategy_version,),
            ).fetchall()
        return [str(row["indicator_name"]) for row in rows]

    def allocation_history(
        self, strategy_version: str, *, days: int = DEFAULT_HISTORY_DAYS
    ) -> DataFrame:
        start = (date.today() - timedelta(days=days)).isoformat()
        with read_only(self.db_path) as connection:
            frame = pd.read_sql_query(
                "SELECT observation_date, asset, weight FROM target_allocations "
                "WHERE strategy_version = ? AND observation_date >= ? "
                "ORDER BY observation_date",
                connection,
                params=(strategy_version, start),
            )
        if frame.empty:
            return DataFrame()
        wide = frame.pivot(index="observation_date", columns="asset", values="weight")
        wide.index = pd.to_datetime(wide.index)
        wide.columns.name = None
        return wide.fillna(0.0)

    # -- events -----------------------------------------------------------
    def regime_events(
        self, strategy_version: str | None = None, *, limit: int = 100
    ) -> DataFrame:
        sql = (
            "SELECT event_date, previous_regime, new_regime, previous_score, "
            "new_score, reason_codes FROM regime_events"
        )
        params: list[object] = []
        if strategy_version:
            sql += " WHERE strategy_version = ?"
            params.append(strategy_version)
        with read_only(self.db_path) as connection:
            return pd.read_sql_query(
                sql + " ORDER BY event_date DESC LIMIT ?", connection, params=[*params, limit]
            )

    def alert_events(self, *, limit: int = 100) -> DataFrame:
        with read_only(self.db_path) as connection:
            return pd.read_sql_query(
                "SELECT event_date, event_type, severity, title, delivery_status, "
                "provider, sent_at, error FROM alert_events "
                "ORDER BY event_date DESC, id DESC LIMIT ?",
                connection,
                params=(limit,),
            )

    # -- operations -------------------------------------------------------
    def recent_runs(self, *, limit: int = 20) -> DataFrame:
        with read_only(self.db_path) as connection:
            return pd.read_sql_query(
                "SELECT run_date, status, stage, started_at, finished_at, "
                "error_message, strategy_version FROM pipeline_runs "
                "ORDER BY started_at DESC LIMIT ?",
                connection,
                params=(limit,),
            )

    def open_findings(self) -> DataFrame:
        with read_only(self.db_path) as connection:
            return pd.read_sql_query(
                "SELECT symbol, observation_date, check_name, status, detail "
                "FROM data_quality_findings WHERE resolved_at IS NULL "
                "ORDER BY observation_date DESC LIMIT 200",
                connection,
            )

    def data_coverage(self) -> DataFrame:
        with read_only(self.db_path) as connection:
            return pd.read_sql_query(
                "SELECT symbol, COUNT(*) AS rows, MIN(observation_date) AS first, "
                "MAX(observation_date) AS latest, "
                "SUM(quality_status <> 'OK') AS flagged "
                "FROM market_observations GROUP BY symbol ORDER BY symbol",
                connection,
            )

    def strategy_versions(self) -> DataFrame:
        with read_only(self.db_path) as connection:
            return pd.read_sql_query(
                "SELECT strategy_version, parameter_status, parameter_version, "
                "data_version, frozen_at, is_active FROM strategy_versions "
                "ORDER BY created_at DESC",
                connection,
            )


def _with_dates(frame: DataFrame) -> DataFrame:
    if frame.empty or "observation_date" not in frame.columns:
        return frame
    result = frame.copy()
    result["observation_date"] = pd.to_datetime(result["observation_date"])
    return result.set_index("observation_date")


def regime_spans(history: DataFrame) -> list[tuple[pd.Timestamp, pd.Timestamp, str]]:
    """Contiguous regime runs, for shading a price chart.

    Returns ``(start, end, regime)`` triples. ``UNKNOWN`` stretches are kept
    rather than smoothed over: a gap in the signal is information.
    """
    if history.empty or "regime" not in history.columns:
        return []
    spans: list[tuple[pd.Timestamp, pd.Timestamp, str]] = []
    current = history["regime"].iloc[0]
    start = history.index[0]
    previous = start
    for moment, regime in history["regime"].items():
        if regime != current:
            spans.append((start, previous, str(current)))
            current, start = regime, moment
        previous = moment
    spans.append((start, previous, str(current)))
    return spans


def is_signal_stale(latest: dict[str, object] | None, *, tolerance_days: int = 5) -> bool:
    """Whether the newest stored state is too old to be treated as current."""
    if latest is None:
        return True
    if latest.get("regime") == UNKNOWN_REGIME:
        return True
    observed = date.fromisoformat(str(latest["observation_date"]))
    return (date.today() - observed).days > tolerance_days

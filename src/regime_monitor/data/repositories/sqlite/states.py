"""The daily market state and the target allocation that followed it.

See :mod:`regime_monitor.data.repositories.sqlite` for the write semantics
every repository here shares.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from regime_monitor.constants import (
    Asset,
    DataQualityStatus,
)
from regime_monitor.data.models import (
    MarketState,
    TargetAllocation,
    utcnow,
)
from regime_monitor.data.repositories.base import _Base
from regime_monitor.data.repositories.connection import (
    from_db_date,
    from_db_datetime,
    from_json,
    to_db_date,
    to_db_datetime,
    to_json,
)


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

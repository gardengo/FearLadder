"""Computed indicator values and their normalized scores.

See :mod:`fear_ladder.data.repositories.sqlite` for the write semantics
every repository here shares.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from fear_ladder.constants import (
    DataQualityStatus,
)
from fear_ladder.data.models import (
    IndicatorScore,
    IndicatorValue,
    utcnow,
)
from fear_ladder.data.repositories.base import _Base
from fear_ladder.data.repositories.connection import (
    from_db_date,
    from_json,
    opt_datetime,
    to_db_date,
    to_db_datetime,
)


class SQLiteIndicatorRepository(_Base):
    def save_values(self, values: list[IndicatorValue]) -> int:
        if not values:
            return 0
        now = to_db_datetime(utcnow())
        rows = [
            (
                item.indicator_name,
                to_db_date(item.observation_date),
                item.params_key,
                item.value,
                item.source_symbol,
                None
                if item.availability_datetime is None
                else to_db_datetime(item.availability_datetime),
                item.quality_status.value,
                now,
                now,
            )
            for item in values
        ]
        self._connection.executemany(
            """
            INSERT INTO indicator_values (
                indicator_name, observation_date, params_key, value, source_symbol,
                availability_datetime, quality_status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (indicator_name, observation_date, params_key) DO UPDATE SET
                value = excluded.value,
                source_symbol = excluded.source_symbol,
                availability_datetime = excluded.availability_datetime,
                quality_status = excluded.quality_status,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        return len(rows)

    def get_values(
        self,
        indicator_name: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[IndicatorValue]:
        clause, params = self._range("observation_date", start, end)
        rows = self._connection.execute(
            f"SELECT * FROM indicator_values WHERE indicator_name = ?{clause} "
            "ORDER BY observation_date",
            [indicator_name, *params],
        ).fetchall()
        return [
            IndicatorValue(
                indicator_name=row["indicator_name"],
                observation_date=from_db_date(row["observation_date"]),
                value=row["value"],
                source_symbol=row["source_symbol"],
                params=from_json(row["params_key"], {}),
                availability_datetime=opt_datetime(row["availability_datetime"]),
                quality_status=DataQualityStatus(row["quality_status"]),
            )
            for row in rows
        ]

    def save_scores(self, scores: list[IndicatorScore], *, strategy_version: str) -> int:
        if not scores:
            return 0
        now = to_db_datetime(utcnow())
        rows = [
            (
                score.indicator_name,
                to_db_date(score.observation_date),
                strategy_version,
                score.score,
                score.raw_value,
                score.normalization_method,
                score.normalization_window,
                score.quality_status.value,
                now,
                now,
            )
            for score in scores
        ]
        self._connection.executemany(
            """
            INSERT INTO indicator_scores (
                indicator_name, observation_date, strategy_version, score, raw_value,
                normalization_method, normalization_window, quality_status,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (indicator_name, observation_date, strategy_version) DO UPDATE SET
                score = excluded.score,
                raw_value = excluded.raw_value,
                normalization_method = excluded.normalization_method,
                normalization_window = excluded.normalization_window,
                quality_status = excluded.quality_status,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        return len(rows)

    def get_scores(self, observation_date: date, *, strategy_version: str) -> list[IndicatorScore]:
        rows = self._connection.execute(
            "SELECT * FROM indicator_scores WHERE observation_date = ? AND strategy_version = ? "
            "ORDER BY indicator_name",
            (to_db_date(observation_date), strategy_version),
        ).fetchall()
        return [_score_from_row(row) for row in rows]


def _score_from_row(row: sqlite3.Row) -> IndicatorScore:
    return IndicatorScore(
        indicator_name=row["indicator_name"],
        observation_date=from_db_date(row["observation_date"]),
        score=row["score"],
        normalization_method=row["normalization_method"],
        normalization_window=row["normalization_window"],
        raw_value=row["raw_value"],
        quality_status=DataQualityStatus(row["quality_status"]),
    )

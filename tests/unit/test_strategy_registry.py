"""The registry of strategy versions the system has actually run.

The table existed and stayed empty: ``save_version`` had no caller, so the
dashboard fell back to guessing the active version from whichever state row was
newest. Harmless with one strategy, ambiguous the moment a second is frozen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fear_ladder.data.models import StrategyVersionRecord
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork

FROZEN_AT = datetime(2026, 9, 13, tzinfo=UTC)


def _record(version: str, *, active: bool = True) -> StrategyVersionRecord:
    return StrategyVersionRecord(
        strategy_version=version,
        parameter_status="FROZEN",
        frozen_at=FROZEN_AT,
        is_active=active,
    )


def test_a_second_active_version_stands_the_first_one_down(db_path: Path) -> None:
    """A partial unique index allows one active row.

    ``save_version`` used to insert the new row with ``is_active = 1`` and only
    then deactivate the old one, so the insert hit the index first and the whole
    switch failed. Registering a v2 has to work on the day there is a v2.
    """
    with SQLiteUnitOfWork(db_path) as uow:
        uow.strategies.save_version(_record("v1.0-frozen"))
        uow.strategies.save_version(_record("v2.0-frozen"))

        active = uow.strategies.get_active_version()
        assert active is not None
        assert active.strategy_version == "v2.0-frozen"

        rows = uow.connection.execute(
            "SELECT strategy_version, is_active FROM strategy_versions ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("v1.0-frozen", 0), ("v2.0-frozen", 1)]


def test_re_registering_the_same_version_is_idempotent(db_path: Path) -> None:
    with SQLiteUnitOfWork(db_path) as uow:
        for _ in range(3):
            uow.strategies.save_version(_record("v1.0-frozen"))
        count = uow.connection.execute(
            "SELECT COUNT(*) FROM strategy_versions"
        ).fetchone()[0]
        assert count == 1
        active = uow.strategies.get_active_version()
        assert active is not None and active.strategy_version == "v1.0-frozen"


def test_an_inactive_registration_leaves_the_active_one_alone(db_path: Path) -> None:
    with SQLiteUnitOfWork(db_path) as uow:
        uow.strategies.save_version(_record("v1.0-frozen"))
        uow.strategies.save_version(_record("v0.9-frozen", active=False))
        active = uow.strategies.get_active_version()
        assert active is not None
        assert active.strategy_version == "v1.0-frozen"


def test_activating_an_unknown_version_is_refused(db_path: Path) -> None:
    with SQLiteUnitOfWork(db_path) as uow, pytest.raises(LookupError, match="unknown"):
        uow.strategies.activate("never-frozen")

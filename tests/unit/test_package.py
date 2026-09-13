"""TASK-001: the project is importable and the test runner is wired up."""

from __future__ import annotations

from pathlib import Path

import fear_ladder
from fear_ladder import paths


def test_package_exposes_version() -> None:
    assert fear_ladder.__version__


def test_project_root_contains_the_core_documents() -> None:
    for document in ("PRD.md", "ARCHITECTURE.md", "BACKTEST_SPEC.md", "TASKS.md"):
        assert (paths.PROJECT_ROOT / document).is_file(), document


def test_default_db_path_honours_the_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FEAR_LADDER_DB", str(tmp_path / "x.db"))
    assert paths.default_db_path() == (tmp_path / "x.db").resolve()

    monkeypatch.delenv("FEAR_LADDER_DB")
    assert paths.default_db_path().name == paths.DEFAULT_DB_FILENAME

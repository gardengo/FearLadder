"""TASK-001: the project is importable and the test runner is wired up."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import fear_ladder
from fear_ladder import paths


def test_package_exposes_version() -> None:
    assert fear_ladder.__version__


def test_project_root_contains_the_core_documents() -> None:
    for document in (
        "PRD.md",
        "ARCHITECTURE.md",
        "BACKTEST_SPEC.md",
        "TASKS.md",
        "CONTRIBUTING.md",
    ):
        assert (paths.PROJECT_ROOT / document).is_file(), document


def test_every_declared_entry_point_resolves() -> None:
    """A console script naming a module that does not exist installs fine.

    It only fails when someone runs it — which is the wrong moment to find out,
    and is how `fearladder-daily = fear_ladder.pipeline.cli:main` survived past
    the module being removed.
    """
    import importlib

    scripts = _pyproject().get("project", {}).get("scripts", {})
    for name, target in scripts.items():
        module_name, _, attribute = target.partition(":")
        module = importlib.import_module(module_name)
        assert hasattr(module, attribute), f"{name} -> {target}"


def test_requirements_txt_still_mirrors_pyproject() -> None:
    """Streamlit Cloud reads requirements.txt and knows nothing about extras.

    So it is a hand-maintained flattening of the base dependencies plus the
    `dashboard` extra, and hand-maintained copies drift: `xlrd` was added to
    pyproject and not here, which would have taken the AAII fetch down on any
    host installed from this file.
    """
    project = _pyproject()["project"]
    declared = [
        *project["dependencies"],
        *project["optional-dependencies"]["dashboard"],
    ]
    expected = {_requirement_name(item) for item in declared}

    text = (paths.PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    mirrored = {
        _requirement_name(line)
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert mirrored == expected, f"drifted: {expected ^ mirrored}"


def _pyproject() -> dict:
    with (paths.PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _requirement_name(requirement: str) -> str:
    """``"tzdata>=2024.1; sys_platform == 'win32'"`` -> ``"tzdata"``."""
    return re.split(r"[<>=!;\[ ]", requirement.strip(), maxsplit=1)[0].lower()


def test_default_db_path_honours_the_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FEAR_LADDER_DB", str(tmp_path / "x.db"))
    assert paths.default_db_path() == (tmp_path / "x.db").resolve()

    monkeypatch.delenv("FEAR_LADDER_DB")
    assert paths.default_db_path().name == paths.DEFAULT_DB_FILENAME

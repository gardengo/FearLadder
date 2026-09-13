"""Filesystem locations for the project.

Resolved relative to the installed package so that the daily runner, the test
suite and the Streamlit app all agree on where the SQLite state lives.
"""

from __future__ import annotations

import os
from pathlib import Path

#: ``src/fear_ladder`` -> ``src`` -> repository root
PACKAGE_ROOT: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = PACKAGE_ROOT.parent.parent

CONFIG_DIR: Path = PROJECT_ROOT / "config"
DATA_DIR: Path = PROJECT_ROOT / "data"
RAW_DIR: Path = DATA_DIR / "raw"
PROCESSED_DIR: Path = DATA_DIR / "processed"
SNAPSHOT_DIR: Path = DATA_DIR / "snapshots"
REPORTS_DIR: Path = PROJECT_ROOT / "reports"
BACKTEST_REPORT_DIR: Path = REPORTS_DIR / "backtest"

DEFAULT_DB_FILENAME = "fear_ladder.db"


def default_db_path() -> Path:
    """Path of the operational SQLite database.

    ``FEAR_LADDER_DB`` overrides it, which keeps tests and ad-hoc research
    runs off the committed operational database.
    """
    override = os.environ.get("FEAR_LADDER_DB")
    if override:
        return Path(override).expanduser().resolve()
    return DATA_DIR / DEFAULT_DB_FILENAME

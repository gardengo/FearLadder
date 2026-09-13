"""Shared plumbing for the dashboard views.

Everything here is read-only by construction. The dashboard shows what the daily
worker already computed and what the freeze already measured; it never runs an
engine (``ARCHITECTURE.md`` §4.2), which is why no compute module is imported
anywhere under ``app/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st

from fear_ladder import paths
from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.pipeline.queries import DashboardQueries

CACHE_SECONDS = 300

#: Fear (red) through neutral (grey) to greed (blue). Deliberately not
#: red=bad/green=good: a fearful regime is where this strategy *adds* leverage.
REGIME_COLOURS = [
    "#b2182b", "#d6604d", "#f4a582", "#d9d9d9",
    "#92c5de", "#4393c3", "#2166ac", "#1a4a7a", "#0d2d4d",
]
UNKNOWN_COLOUR = "#9e9e9e"

PERFORMANCE_REPORT = paths.REPORTS_DIR / "performance.json"
STRATEGY_LABEL = "전략"


@st.cache_resource
def queries() -> DashboardQueries:
    return DashboardQueries()


@st.cache_data(ttl=CACHE_SECONDS)
def load(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
    """One cached entry point, so every query shares the same TTL."""
    return getattr(queries(), name)(*args, **kwargs)


@st.cache_data(ttl=CACHE_SECONDS)
def performance_report(path: str | None = None) -> dict[str, Any] | None:
    """The frozen strategy's measured behaviour, or ``None`` if not generated.

    Written by ``scripts/make_performance_report.py``. Reading a file rather
    than recomputing keeps a 30-year backtest out of the page load, and keeps
    the dashboard honest: it can only show what a freeze actually measured.
    """
    target = Path(path) if path else PERFORMANCE_REPORT
    if not target.is_file():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


def regime_palette(labels: list[str]) -> dict[str, str]:
    known = [label for label in labels if label != UNKNOWN_REGIME]
    step = max(1, len(REGIME_COLOURS) // max(len(known), 1))
    palette = {
        label: REGIME_COLOURS[min(index * step, len(REGIME_COLOURS) - 1)]
        for index, label in enumerate(known)
    }
    palette[UNKNOWN_REGIME] = UNKNOWN_COLOUR
    return palette


def missing_report_notice() -> None:
    st.info(
        "성과 리포트가 없습니다.\n\n"
        "`python scripts/make_performance_report.py` 로 생성하세요. "
        "이 파일은 고정된 전략을 전체 보유 기간에 대해 한 번 계산한 결과이며, "
        "대시보드는 그 결과를 읽기만 합니다."
    )


def percent(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def ratio(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"

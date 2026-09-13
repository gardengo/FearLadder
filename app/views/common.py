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

#: Fear (red) through neutral (grey) to greed (green), following the convention
#: every price chart already uses: falling is red, rising is green. A fearful
#: regime is where this strategy *adds* leverage, so the colours describe the
#: market rather than whether the day is good news for the portfolio.
REGIME_COLOURS = [
    "#b2182b", "#d6604d", "#f4a582", "#d9d9d9", "#a6d96a", "#66bd63", "#1a9850",
]
#: Continuous version for the 0-100 composite score.
SCORE_SCALE = ["#b2182b", "#d9d9d9", "#1a9850"]
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


@st.cache_data(ttl=CACHE_SECONDS)
def regime_order() -> list[str]:
    """The regimes from fear to greed, as the strategy defines them.

    Reading the configuration matters: the labels arriving from a query are in
    whatever order the database returned them, and sorting those alphabetically
    put Panic at the greed end of the palette and Euphoria at the fear end —
    every crash on the history chart was shaded as if it were a rally.
    """
    from fear_ladder.config.loader import load_config

    return list(load_config().strategy.regime.labels or ())


def regime_palette(labels: list[str]) -> dict[str, str]:
    """Colour per regime, placed by where each sits on the fear-greed range."""
    order = regime_order()
    known = [label for label in labels if label != UNKNOWN_REGIME]
    ranked = [label for label in order if label in known]
    # Anything the configuration does not know about goes after what it does,
    # so an unexpected label cannot shift the colours of the real ones.
    ranked += sorted(label for label in known if label not in order)

    last = len(REGIME_COLOURS) - 1
    span = max(len(ranked) - 1, 1)
    palette = {
        label: REGIME_COLOURS[round(index / span * last)]
        for index, label in enumerate(ranked)
    }
    palette[UNKNOWN_REGIME] = UNKNOWN_COLOUR
    return palette


@st.cache_data(ttl=CACHE_SECONDS)
def weekly_indicators() -> set[str]:
    """Indicators fed by a source that publishes weekly rather than daily.

    They have no reading on four days out of five, which is not a failure and
    must not be reported as one — the backtest treats those days the same way.
    """
    from fear_ladder.config.loader import load_config
    from fear_ladder.data.retention import WEEKLY_SOURCES

    return {
        name
        for name, spec in load_config().indicators.enabled_indicators.items()
        if spec.source in WEEKLY_SOURCES
    }


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

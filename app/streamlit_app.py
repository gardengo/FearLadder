"""Streamlit dashboard (TASK-140 .. TASK-144).

Reads the state the daily worker already computed and visualises it. It does not
run the strategy: ``ARCHITECTURE.md`` §4.2 makes the worker the source of truth,
and the database is opened read-only so a dashboard session can never interfere
with a run.

    streamlit run app/streamlit_app.py

One tab per view module under ``app/views``; this file only wires them up.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR.parent / "src"))
sys.path.insert(0, str(APP_DIR))

import streamlit as st  # noqa: E402

from fear_ladder import paths  # noqa: E402
from fear_ladder.pipeline.queries import DashboardDataError  # noqa: E402
from views import (  # noqa: E402
    events,
    history,
    indicators,
    operations,
    performance,
    strategy,
    today,
)
from views.common import load  # noqa: E402

#: A drawn icon rather than an emoji. Streamlit renders an emoji page_icon as
#: text inside an SVG data URI, so the browser has to own a font covering that
#: codepoint — and U+1FA9C (the ladder) is Unicode 13.0, which many emoji fonts
#: still lack, leaving an empty box in the tab. Regenerate with
#: scripts/make_favicon.py.
FAVICON = APP_DIR / "assets" / "favicon.png"

st.set_page_config(
    page_title="FearLadder",
    page_icon=str(FAVICON) if FAVICON.is_file() else "📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

#: Label, module, and whether the tab needs a stored strategy version.
TABS = (
    ("오늘", today, True),
    ("전략 설명", strategy, False),
    ("성과", performance, False),
    ("지표", indicators, True),
    ("기록", history, True),
    ("이벤트", events, True),
    ("운영", operations, False),
)


def main() -> None:
    # The drawn icon again rather than the emoji: U+1FA9C renders as an empty
    # box wherever the font does not cover it, and a broken glyph in the title
    # is more visible than one in a tab.
    if FAVICON.is_file():
        st.logo(str(FAVICON))
        heading = st.columns([1, 14], vertical_alignment="center")
        heading[0].image(str(FAVICON), width=64)
        heading[1].title("FearLadder")
    else:
        st.title("FearLadder")
    st.caption(
        "공포가 깊을수록 한 칸 더 올라가는 레버리지 사다리 — "
        "분석과 알림만 제공합니다. "
        "**자동매매를 하지 않으며, 매매 결정은 사용자의 몫입니다.**"
    )

    try:
        version = load("active_strategy_version")
    except DashboardDataError as error:
        st.error(str(error))
        return

    with st.sidebar:
        st.header("FearLadder")
        st.write(f"strategy: `{version or '—'}`")
        st.write(f"db: `{paths.default_db_path().name}`")
        st.caption(f"오늘: {date.today()}")
        if st.button("새로고침", width="stretch"):
            st.cache_data.clear()
            st.rerun()
        st.divider()
        st.caption(
            "처음이라면 **전략 설명** 탭부터 보세요. 이 전략이 무엇을 보고 "
            "어떻게 판단하는지 설명합니다."
        )
        st.caption(
            "이 대시보드는 저장된 상태를 보여줄 뿐, 전략을 다시 계산하지 "
            "않습니다 (ARCHITECTURE.md §4.2)."
        )

    tabs = st.tabs([label for label, _, _ in TABS])
    for tab, (_, module, needs_version) in zip(tabs, TABS, strict=True):
        with tab:
            if needs_version and not version:
                st.info(
                    "아직 저장된 상태가 없습니다. `python scripts/daily_runner.py` 를 "
                    "실행하면 이 탭이 채워집니다. 그 전에도 **전략 설명**과 "
                    "**성과** 탭은 볼 수 있습니다."
                )
                continue
            module.render(version) if needs_version else module.render()


if __name__ == "__main__":
    main()

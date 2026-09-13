"""The record the daily worker accumulates: regime, score and leverage over time."""

from __future__ import annotations

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.pipeline.queries import regime_spans
from views.common import UNKNOWN_COLOUR, load, regime_palette


def render(version: str) -> None:
    st.header("기록")
    years = st.slider("기간 (년)", 1, 15, 3)
    days = years * 365

    history = load("state_history", version, days=days)
    if history.empty:
        st.info("저장된 이력이 없습니다.")
        return

    prices = load("price_history", ("QQQ",), days=days)
    palette = regime_palette(history["regime"].dropna().unique().tolist())

    st.subheader("QQQ 와 단계")
    figure = go.Figure()
    if not prices.empty:
        figure.add_scatter(
            x=prices.index, y=prices["QQQ"], name="QQQ", line={"color": "#222", "width": 1.4}
        )
        for start, end, regime in regime_spans(history):
            figure.add_vrect(
                x0=start,
                x1=end,
                fillcolor=palette.get(regime, UNKNOWN_COLOUR),
                opacity=0.18,
                line_width=0,
                layer="below",
            )
    figure.update_layout(height=420, showlegend=False)
    st.plotly_chart(figure, width="stretch")
    st.caption(
        "배경색 = 단계. 빨강 = 공포(레버리지를 올리는 구간), 초록 = 탐욕(내리는 구간), "
        "회색 = 신호 없음. 차트 관례대로 하락이 빨강, 상승이 초록이다."
    )

    left, right = st.columns(2)
    with left:
        st.subheader("종합점수")
        figure = px.line(history, y="composite_score", range_y=[0, 100])
        figure.add_hline(y=50, line_dash="dash", line_color="#999")
        figure.update_layout(height=300, yaxis_title="score")
        st.plotly_chart(figure, width="stretch")

    with right:
        st.subheader("목표 레버리지")
        figure = px.line(history, y="target_leverage")
        figure.update_layout(height=300, yaxis_title="leverage (x)")
        st.plotly_chart(figure, width="stretch")

    st.subheader("목표 비중")
    allocations = load("allocation_history", version, days=days)
    if allocations.empty:
        st.info("배분 이력이 없습니다.")
    else:
        figure = px.area(
            allocations,
            color_discrete_sequence=px.colors.qualitative.Set2,
            labels={"value": "weight", "observation_date": ""},
        )
        figure.update_layout(height=320, yaxis_range=[0, 1])
        st.plotly_chart(figure, width="stretch")

    unknown_days = int((history["regime"] == UNKNOWN_REGIME).sum())
    if unknown_days:
        st.warning(f"이 기간에 신호를 만들지 못한 날이 {unknown_days}일 있습니다.")

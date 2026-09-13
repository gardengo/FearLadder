"""Every indicator's score today, and one indicator's history."""

from __future__ import annotations

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from views.common import load


def render(version: str) -> None:
    st.header("지표")
    state = load("latest_state", version)
    if state is None:
        st.info("저장된 상태가 없습니다.")
        return

    scores = load("indicator_scores", str(state["observation_date"]), version)
    if scores.empty:
        st.info("해당 날짜의 지표 점수가 없습니다.")
        return

    ordered = scores.dropna(subset=["score"]).sort_values("score")
    figure = px.bar(
        ordered,
        x="score",
        y="indicator_name",
        orientation="h",
        range_x=[0, 100],
        color="score",
        color_continuous_scale=["#b2182b", "#d9d9d9", "#2166ac"],
        range_color=[0, 100],
        labels={"score": "Score (0=공포, 100=탐욕)", "indicator_name": ""},
    )
    figure.add_vline(x=50, line_dash="dash", line_color="#666")
    figure.update_layout(height=max(400, 22 * len(ordered)), coloraxis_showscale=False)
    st.plotly_chart(figure, width="stretch")

    missing = scores[scores["score"].isna()]
    if not missing.empty:
        st.warning(
            "점수를 계산하지 못한 지표: "
            + ", ".join(missing["indicator_name"].tolist())
            + " — 가중치는 나머지 지표로 재분배되었습니다."
        )

    st.subheader("지표 이력")
    names = load("available_indicators", version)
    chosen = st.selectbox("지표", names, index=names.index("rsi_14") if "rsi_14" in names else 0)
    history = load("indicator_history", chosen, version)
    if history.empty:
        st.info("이력이 없습니다.")
        return

    figure = go.Figure()
    figure.add_scatter(x=history.index, y=history["score"], name="score (0-100)")
    figure.add_scatter(
        x=history.index, y=history["raw_value"], name="raw", yaxis="y2", opacity=0.5
    )
    figure.update_layout(
        yaxis={"title": "score", "range": [0, 100]},
        yaxis2={"title": "raw", "overlaying": "y", "side": "right"},
        height=380,
        legend={"orientation": "h", "y": 1.1},
    )
    st.plotly_chart(figure, width="stretch")

    st.dataframe(scores, hide_index=True, width="stretch")

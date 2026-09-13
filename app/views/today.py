"""Today's signal: what the worker decided and why."""

from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import streamlit as st

from regime_monitor.constants import UNKNOWN_REGIME
from regime_monitor.pipeline.queries import is_signal_stale
from views.common import load


def render(version: str | None) -> None:
    st.header("Current")
    state = load("latest_state", version)
    if state is None:
        st.info(
            "아직 저장된 상태가 없습니다. `python scripts/daily_runner.py` 를 "
            "실행하거나 GitHub Actions 의 daily-monitor 워크플로를 수동 실행하세요."
        )
        return

    if state["regime"] == UNKNOWN_REGIME:
        st.error(
            f"**{state['observation_date']}: 신호 없음 (UNKNOWN)**\n\n"
            "필수 데이터를 신뢰할 수 없어 목표 배분과 레버리지를 계산하지 "
            "않았습니다. 아래 근거를 확인하세요."
        )
    elif is_signal_stale(state):
        st.warning(
            f"마지막 상태가 {state['observation_date']} 기준입니다. "
            "일일 워커가 최근에 실행되지 않았을 수 있습니다."
        )

    columns = st.columns(4)
    columns[0].metric("Regime", str(state["regime"]))
    score = state["composite_score"]
    columns[1].metric(
        "Market Score",
        f"{score:.1f}" if score is not None else "—",
        help="0 = 극단적 공포, 100 = 극단적 탐욕",
    )
    leverage = state["target_leverage"]
    columns[2].metric(
        "Target Leverage",
        f"{leverage:.2f}x" if leverage is not None else "—",
        help="1·QQQ + 2·QLD + 3·TQQQ. 실현 수익률의 배수를 뜻하지 않습니다.",
    )
    columns[3].metric("Last Update", str(state["observation_date"]))

    left, right = st.columns([1, 1])
    with left:
        st.subheader("Target Allocation")
        allocation = load("latest_allocation", version)
        if allocation.empty:
            st.write("배분 없음 (상태 불명)")
        else:
            held = allocation[allocation["weight"] > 0]
            st.plotly_chart(
                px.pie(
                    held, names="asset", values="weight", hole=0.45,
                    color_discrete_sequence=px.colors.qualitative.Set2,
                ).update_traces(textinfo="label+percent"),
                width="stretch",
            )
            st.dataframe(
                allocation.assign(weight=lambda f: (f["weight"] * 100).round(1)),
                hide_index=True,
                width="stretch",
            )

    with right:
        st.subheader("판단 근거")
        st.caption(f"이전 레짐: {state['previous_regime'] or '—'}")
        reasons = json.loads(str(state["reason_codes"] or "[]"))
        for reason in reasons:
            st.write(f"- `{reason}`")

        breakdown = json.loads(str(state["score_breakdown"] or "{}"))
        if breakdown:
            st.caption("지표별 점수 (0=공포, 100=탐욕)")
            frame = (
                pd.DataFrame(
                    {"indicator": list(breakdown), "score": list(breakdown.values())}
                )
                .assign(distance=lambda f: (f["score"] - 50).abs())
                .sort_values("distance", ascending=False)
                .drop(columns="distance")
            )
            st.dataframe(frame, hide_index=True, width="stretch", height=280)

    st.caption(
        f"strategy_version `{state['strategy_version']}` · "
        f"data_version `{state['data_version'] or '—'}` · "
        f"parameter_version `{state['parameter_version'] or '—'}` · "
        f"data quality `{state['data_quality_status']}`"
    )

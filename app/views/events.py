"""Regime changes and the alerts they produced."""

from __future__ import annotations

import streamlit as st

from views.common import load


def render(version: str) -> None:
    st.header("이벤트")

    st.subheader("단계 변경")
    events = load("regime_events", version)
    if events.empty:
        st.info("기록된 레짐 변경이 없습니다.")
    else:
        st.dataframe(events, hide_index=True, width="stretch")

    st.subheader("알림")
    alerts = load("alert_events")
    if alerts.empty:
        st.info("기록된 알림이 없습니다.")
    else:
        pending = alerts[alerts["delivery_status"] == "PENDING"]
        failed = alerts[alerts["delivery_status"] == "FAILED"]
        if not failed.empty:
            st.error(f"전송 실패한 알림 {len(failed)}건이 있습니다.")
        if not pending.empty:
            st.warning(f"아직 전송되지 않은 알림 {len(pending)}건이 있습니다.")
        st.dataframe(alerts, hide_index=True, width="stretch")

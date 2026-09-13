"""Run health, data coverage and open findings."""

from __future__ import annotations

import streamlit as st

from views.common import load


def render() -> None:
    st.header("운영")

    st.subheader("최근 실행")
    runs = load("recent_runs")
    if runs.empty:
        st.info("실행 기록이 없습니다.")
    else:
        failures = runs[runs["status"].isin(["FAILED", "DATA_FAILURE"])]
        if not failures.empty:
            st.error(f"최근 {len(runs)}회 중 {len(failures)}회가 실패했습니다.")
        st.dataframe(runs, hide_index=True, width="stretch")

    st.subheader("데이터 커버리지")
    coverage = load("data_coverage")
    st.dataframe(coverage, hide_index=True, width="stretch")

    findings = load("open_findings")
    st.subheader(f"검토가 필요한 데이터 품질 항목 ({len(findings)}건)")
    if findings.empty:
        st.success("검토가 필요한 항목이 없습니다.")
    else:
        st.warning(
            "교차검증 불일치는 자동으로 수정되지 않습니다. 직접 확인하세요 "
            "(BACKTEST_SPEC.md §5.5)."
        )
        st.dataframe(findings, hide_index=True, width="stretch")

    st.subheader("전략 버전")
    versions = load("strategy_versions")
    if versions.empty:
        st.info("등록된 전략 버전이 없습니다.")
    else:
        st.dataframe(versions, hide_index=True, width="stretch")

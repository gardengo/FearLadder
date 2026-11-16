"""Run health, data coverage and open findings."""

from __future__ import annotations

import streamlit as st

from views.common import (
    RECORDS,
    coverage_table,
    load,
    record_progress,
    run_table,
    updated_daily,
)


def render() -> None:
    st.header("운영")
    version = load("active_strategy_version")
    updated_daily(version)

    _progress(version)

    st.subheader("최근 실행")
    runs = load("recent_runs")
    if runs.empty:
        st.info("실행 기록이 없습니다.")
    else:
        failures = runs[runs["status"].isin(["FAILED", "DATA_FAILURE"])]
        if not failures.empty:
            st.error(f"최근 {len(runs)}회 중 {len(failures)}회가 실패했습니다.")
        st.dataframe(run_table(runs), hide_index=True, width="stretch")
        st.caption(
            "배치는 미국 거래일 다음 날 오전(한국 시간 10시 이후)에 여러 번 예약되어 있고, "
            "그날의 입력이 다 모인 것을 처음 확인한 실행 하나만 일을 합니다. 이 표에는 "
            "일을 한 실행만 남습니다 — 물러난 시도는 GitHub Actions 목록에 있습니다."
        )

    st.subheader("데이터 커버리지")
    coverage = load("data_coverage")
    st.dataframe(coverage_table(coverage), hide_index=True, width="stretch")
    st.caption(
        "«재구성 등급 행» 은 오류의 개수가 아닙니다. 발표된 값을 그대로 받은 것이 아니라 "
        "다시 만들었거나 이어 붙인 값이라는 표시입니다. `CNN_FEAR_GREED` 는 과거 이력"
        "(2차 데이터셋)에 실시간 값을 이어 붙인 시리즈라 **모든 행**이 이 등급이고, "
        "QLD · TQQQ 는 상장 이전 구간을 불러왔을 때만 해당합니다. 값이 서로 어긋나 "
        "사람이 봐야 하는 경우는 아래 항목에 따로 셉니다."
    )

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


def _progress(version: str | None) -> None:
    """How far the signal, the paper track record and the regime memory have got."""
    raw = load("record_progress", version)
    progress = record_progress(raw)
    if progress.signal is None:
        return

    st.subheader("기록이 어디까지 왔나")
    columns = st.columns(len(RECORDS))
    for column, (name, label) in zip(columns, RECORDS.items(), strict=True):
        reached = raw.get(name)
        column.metric(label, str(reached) if reached else "아직 없음")
    if progress.level:
        st.caption(
            f"세 기록이 모두 **{progress.signal}** 까지 와 있습니다. 신호는 그날의 판정, "
            "모의 성과는 그 신호대로 따랐을 때의 수익률, 단계 기억은 다음 날 판정이 "
            "이어받을 상태입니다."
        )
    else:
        late = ", ".join(
            f"{label} ({reached if reached else '기록 없음'})" for label, reached in progress.behind
        )
        st.warning(
            f"신호는 {progress.signal} 까지 왔는데 뒤처진 기록이 있습니다: {late}. "
            "다음 배치가 따라잡지 못하면 GitHub Actions 의 `Daily run` 로그를 확인하세요."
        )

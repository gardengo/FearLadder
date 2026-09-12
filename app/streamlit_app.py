"""Streamlit dashboard (TASK-140 .. TASK-144).

Reads the state the daily worker already computed and visualises it. It does not
run the strategy: ``ARCHITECTURE.md`` §4.2 makes the worker the source of truth,
and the database is opened read-only so a dashboard session can never interfere
with a run.

    streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from regime_monitor import paths
from regime_monitor.constants import UNKNOWN_REGIME
from regime_monitor.pipeline.queries import (
    DashboardDataError,
    DashboardQueries,
    is_signal_stale,
    regime_spans,
)

st.set_page_config(
    page_title="RegimePilot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

CACHE_SECONDS = 300

#: Fear (red) through neutral (grey) to greed (blue). Deliberately not
#: red=bad/green=good: a fearful regime is where this strategy *adds* leverage.
REGIME_COLOURS = [
    "#b2182b", "#d6604d", "#f4a582", "#d9d9d9",
    "#92c5de", "#4393c3", "#2166ac", "#1a4a7a", "#0d2d4d",
]
UNKNOWN_COLOUR = "#9e9e9e"


# --------------------------------------------------------------------- data


@st.cache_resource
def _queries() -> DashboardQueries:
    return DashboardQueries()


@st.cache_data(ttl=CACHE_SECONDS)
def _load(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
    """One cached entry point, so every query shares the same TTL."""
    return getattr(_queries(), name)(*args, **kwargs)


def regime_palette(labels: list[str]) -> dict[str, str]:
    known = [label for label in labels if label != UNKNOWN_REGIME]
    step = max(1, len(REGIME_COLOURS) // max(len(known), 1))
    palette = {
        label: REGIME_COLOURS[min(index * step, len(REGIME_COLOURS) - 1)]
        for index, label in enumerate(known)
    }
    palette[UNKNOWN_REGIME] = UNKNOWN_COLOUR
    return palette


# ------------------------------------------------------------------- TASK-140


def render_current(version: str | None) -> None:
    st.header("Current")
    state = _load("latest_state", version)
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
        allocation = _load("latest_allocation", version)
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


# ------------------------------------------------------------------- TASK-141


def render_indicators(version: str) -> None:
    st.header("Indicators")
    state = _load("latest_state", version)
    if state is None:
        st.info("저장된 상태가 없습니다.")
        return

    scores = _load("indicator_scores", str(state["observation_date"]), version)
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
    names = _load("available_indicators", version)
    chosen = st.selectbox("지표", names, index=names.index("rsi_14") if "rsi_14" in names else 0)
    history = _load("indicator_history", chosen, version)
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


# ------------------------------------------------------------------- TASK-142


def render_history(version: str) -> None:
    st.header("History")
    years = st.slider("기간 (년)", 1, 15, 3)
    days = years * 365

    history = _load("state_history", version, days=days)
    if history.empty:
        st.info("저장된 이력이 없습니다.")
        return

    prices = _load("price_history", ("QQQ",), days=days)
    labels = sorted(history["regime"].dropna().unique().tolist())
    palette = regime_palette(labels)

    st.subheader("QQQ + Regime")
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
        "배경색 = 레짐 (왼쪽 진한 빨강 = 공포 … 오른쪽 진한 파랑 = 탐욕, 회색 = UNKNOWN)"
    )

    left, right = st.columns(2)
    with left:
        st.subheader("Market Score")
        figure = px.line(history, y="composite_score", range_y=[0, 100])
        figure.add_hline(y=50, line_dash="dash", line_color="#999")
        figure.update_layout(height=300, yaxis_title="score")
        st.plotly_chart(figure, width="stretch")

    with right:
        st.subheader("Target Leverage")
        figure = px.line(history, y="target_leverage")
        figure.update_layout(height=300, yaxis_title="leverage (x)")
        st.plotly_chart(figure, width="stretch")

    st.subheader("Target Allocation")
    allocations = _load("allocation_history", version, days=days)
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


# ------------------------------------------------------------------- TASK-143


def render_events(version: str) -> None:
    st.header("Events")

    st.subheader("Regime Changes")
    events = _load("regime_events", version)
    if events.empty:
        st.info("기록된 레짐 변경이 없습니다.")
    else:
        st.dataframe(events, hide_index=True, width="stretch")

    st.subheader("Alerts")
    alerts = _load("alert_events")
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


# ------------------------------------------------------------------- TASK-144


def render_backtest() -> None:
    st.header("Backtest")
    directory = paths.BACKTEST_REPORT_DIR
    metrics_path = directory / "metrics.json"
    summary_path = directory / "summary.json"

    if not metrics_path.is_file():
        st.info(
            "백테스트 리포트가 없습니다.\n\n"
            "`python scripts/backtest.py --profile placeholder --report` 로 생성하세요."
        )
        return

    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("warning"):
            st.warning(summary["warning"])
        st.caption(
            f"strategy `{summary.get('strategy_version')}` "
            f"({summary.get('parameter_status')}) · "
            f"{summary.get('start_date')} … {summary.get('end_date')} · "
            f"실행 `{summary.get('execution_rule')}` · "
            f"비용 {summary.get('cost_model')} · "
            f"commit `{(summary.get('code_commit') or '')[:8]}`"
        )

    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    rows = [payload["strategy"], *payload.get("benchmarks", {}).values()]
    table = pd.DataFrame(rows).set_index("name")

    display = table[
        [
            "cagr", "total_return", "volatility", "max_drawdown", "sharpe",
            "sortino", "calmar", "worst_year", "time_under_water", "turnover",
            "trade_count", "regime_change_count",
        ]
    ]
    st.dataframe(
        display.style.format(
            {
                "cagr": "{:.2%}", "total_return": "{:.2%}", "volatility": "{:.2%}",
                "max_drawdown": "{:.2%}", "worst_year": "{:.2%}",
                "time_under_water": "{:.1%}", "sharpe": "{:.2f}",
                "sortino": "{:.2f}", "calmar": "{:.2f}", "turnover": "{:.1f}",
            }
        ),
        width="stretch",
    )

    portfolio_path = directory / "daily_portfolio.csv"
    if portfolio_path.is_file():
        frame = pd.read_csv(portfolio_path, index_col=0, parse_dates=[0])
        nav_columns = [column for column in frame.columns if column.startswith("nav")]
        if nav_columns:
            st.subheader("Equity Curve")
            st.plotly_chart(
                px.line(frame[nav_columns], log_y=True, labels={"value": "NAV (log)"}),
                width="stretch",
            )
            st.subheader("Drawdown")
            drawdowns = 1 - frame[nav_columns] / frame[nav_columns].cummax()
            st.plotly_chart(
                px.line(-drawdowns, labels={"value": "drawdown"}),
                width="stretch",
            )


# ---------------------------------------------------------------- operations


def render_operations() -> None:
    st.header("Operations")

    st.subheader("Recent Runs")
    runs = _load("recent_runs")
    if runs.empty:
        st.info("실행 기록이 없습니다.")
    else:
        failures = runs[runs["status"].isin(["FAILED", "DATA_FAILURE"])]
        if not failures.empty:
            st.error(f"최근 {len(runs)}회 중 {len(failures)}회가 실패했습니다.")
        st.dataframe(runs, hide_index=True, width="stretch")

    st.subheader("Data Coverage")
    coverage = _load("data_coverage")
    st.dataframe(coverage, hide_index=True, width="stretch")

    findings = _load("open_findings")
    st.subheader(f"Open Data-Quality Findings ({len(findings)})")
    if findings.empty:
        st.success("검토가 필요한 항목이 없습니다.")
    else:
        st.warning(
            "교차검증 불일치는 자동으로 수정되지 않습니다. 직접 확인하세요 "
            "(BACKTEST_SPEC.md §5.5)."
        )
        st.dataframe(findings, hide_index=True, width="stretch")

    st.subheader("Strategy Versions")
    versions = _load("strategy_versions")
    if versions.empty:
        st.info("등록된 전략 버전이 없습니다.")
    else:
        st.dataframe(versions, hide_index=True, width="stretch")


# ---------------------------------------------------------------------- main


def main() -> None:
    st.title("📈 RegimePilot")
    st.caption(
        "NASDAQ Leverage Regime Monitor — 분석과 알림만 제공합니다. "
        "**자동매매를 하지 않으며, 매매 결정은 사용자의 몫입니다.**"
    )

    try:
        version = _load("active_strategy_version")
    except DashboardDataError as error:
        st.error(str(error))
        return

    with st.sidebar:
        st.header("RegimePilot")
        st.write(f"strategy: `{version or '—'}`")
        st.write(f"db: `{paths.default_db_path().name}`")
        st.caption(f"오늘: {date.today()}")
        if st.button("새로고침", width="stretch"):
            st.cache_data.clear()
            st.rerun()
        st.divider()
        st.caption(
            "이 대시보드는 저장된 상태를 보여줄 뿐, 전략을 다시 계산하지 "
            "않습니다 (ARCHITECTURE.md §4.2)."
        )

    tabs = st.tabs(["Current", "Indicators", "History", "Events", "Backtest", "Operations"])
    with tabs[0]:
        render_current(version)
    with tabs[1]:
        if version:
            render_indicators(version)
        else:
            st.info("전략 버전이 없습니다.")
    with tabs[2]:
        if version:
            render_history(version)
        else:
            st.info("전략 버전이 없습니다.")
    with tabs[3]:
        if version:
            render_events(version)
        else:
            st.info("전략 버전이 없습니다.")
    with tabs[4]:
        render_backtest()
    with tabs[5]:
        render_operations()


if __name__ == "__main__":
    main()

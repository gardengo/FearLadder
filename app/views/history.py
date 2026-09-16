"""The record the daily worker accumulates.

Three sub-tabs, because the record answers three different questions and they
do not belong on one scroll: what happened (charts), how each stage went
(journal), and how often the score and the ladder disagreed (locks). The third
is the point of this page — on the stored history the two differ far more often
than they agree, and nothing on the old page showed it.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.pipeline.queries import regime_spans
from views.common import (
    LOCK_COLOUR,
    UNKNOWN_COLOUR,
    ladder,
    load,
    locks,
    performance_report,
    regime_palette,
)

#: Preset before slider: the record is read far more often for "what happened
#: lately" than for "what happened in 2024", and a slider defaulting to three
#: years buries the last month in 750 points.
PERIODS: tuple[tuple[str, int], ...] = (
    ("3개월", 91),
    ("6개월", 182),
    ("1년", 365),
    ("3년", 365 * 3),
    ("전체", 365 * 40),
)
DEFAULT_PERIOD = 2  # 1년

LOCK_LABELS = {
    "min_duration": "최소 유지 기간",
    "hysteresis": "히스테리시스",
    "confirmation": "확인 대기",
}


def render(version: str) -> None:
    st.header("기록")

    labels = [label for label, _ in PERIODS]
    chosen = st.radio(
        "기간", labels, index=DEFAULT_PERIOD, horizontal=True, label_visibility="collapsed"
    )
    days = dict(PERIODS)[chosen]

    history = load("state_history", version, days=days)
    if history.empty:
        st.info("저장된 이력이 없습니다.")
        return

    st.caption(
        f"{history.index[0]:%Y-%m-%d} … {history.index[-1]:%Y-%m-%d} · {len(history)}거래일"
    )

    charts, journal, lockdown = st.tabs(["차트", "단계별 일지", "잠금 통계"])
    with charts:
        _charts(version, history, days)
    with journal:
        _journal(version, history)
    with lockdown:
        _locks(history)

    unknown_days = int((history["regime"] == UNKNOWN_REGIME).sum())
    if unknown_days:
        st.warning(f"이 기간에 신호를 만들지 못한 날이 {unknown_days}일 있습니다.")


# -------------------------------------------------------------------- charts


def _charts(version: str, history: pd.DataFrame, days: int) -> None:
    palette = regime_palette(history["regime"].dropna().unique().tolist())

    st.subheader("QQQ 와 단계")
    prices = load("price_history", ("QQQ",), days=days)
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
    figure.update_layout(height=380, showlegend=False)
    st.plotly_chart(figure, width="stretch")
    st.caption(
        "배경색 = 단계. 빨강 = 공포(레버리지를 올리는 구간), 초록 = 탐욕(내리는 구간), "
        "회색 = 신호 없음. 차트 관례대로 하락이 빨강, 상승이 초록이다."
    )

    st.subheader("점수가 가리킨 칸과 실제로 선 칸")
    st.plotly_chart(_steps(history), width="stretch")
    st.caption(
        "실선 = 실제 단계, 점선 = 점수가 그날 가리킨 단계. **두 선이 벌어진 구간이 "
        "잠금 구간**입니다 — 점수는 옮겨갔는데 확인·히스테리시스·최소 유지 규칙이 "
        "아직 단계를 옮겨주지 않은 날들입니다."
    )

    left, right = st.columns(2)
    with left:
        st.subheader("종합점수")
        st.plotly_chart(_score(history), width="stretch")
    with right:
        st.subheader("목표 레버리지")
        figure = px.line(history, y="target_leverage", line_shape="hv")
        figure.update_layout(height=320, yaxis_title="leverage (x)", xaxis_title="")
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


def _rungs() -> list[str]:
    return [rung.label for rung in ladder(performance_report())]


def _steps(history: pd.DataFrame) -> go.Figure:
    """Actual rung and the rung the score pointed at, on one fear-to-greed axis."""
    order = _rungs() or sorted(set(history["regime"].dropna()) - {UNKNOWN_REGIME})
    rank = {label: index for index, label in enumerate(order)}

    actual = history["regime"].map(rank)
    raw = history["raw_regime"].map(rank)

    figure = go.Figure()
    for start, end in _divergences(history, rank):
        figure.add_vrect(
            x0=start, x1=end, fillcolor=LOCK_COLOUR, opacity=0.15, line_width=0, layer="below"
        )
    figure.add_scatter(
        x=history.index,
        y=raw,
        name="점수가 가리킨 단계",
        line={"color": LOCK_COLOUR, "width": 1.4, "dash": "dash"},
        line_shape="hv",
    )
    figure.add_scatter(
        x=history.index,
        y=actual,
        name="실제 단계",
        line={"color": "#222", "width": 2.2},
        line_shape="hv",
    )
    figure.update_yaxes(
        tickmode="array",
        tickvals=list(range(len(order))),
        ticktext=order,
        range=[-0.5, len(order) - 0.5],
    )
    figure.update_layout(
        height=360,
        legend={"orientation": "h", "y": 1.12},
        margin={"l": 8, "r": 8, "t": 30, "b": 8},
    )
    return figure


def _divergences(history: pd.DataFrame, rank: dict[str, int]) -> list[tuple[object, object]]:
    """Contiguous stretches where the score pointed somewhere else."""
    apart = [
        bool(raw in rank and actual in rank and raw != actual)
        for raw, actual in zip(history["raw_regime"], history["regime"], strict=True)
    ]
    spans: list[tuple[object, object]] = []
    start = None
    for moment, differs in zip(history.index, apart, strict=True):
        if differs and start is None:
            start = moment
        elif not differs and start is not None:
            spans.append((start, moment))
            start = None
    if start is not None:
        spans.append((start, history.index[-1]))
    return spans


def _score(history: pd.DataFrame) -> go.Figure:
    """The composite score against the bands it is being classified into."""
    rungs = ladder(performance_report())
    palette = regime_palette([rung.label for rung in rungs])

    figure = go.Figure()
    for rung in rungs:
        figure.add_hrect(
            y0=rung.low,
            y1=rung.high,
            fillcolor=palette.get(rung.label, UNKNOWN_COLOUR),
            opacity=0.16,
            line_width=0,
            layer="below",
            annotation_text=rung.label,
            annotation_position="right",
            annotation_font={"size": 9},
        )
    figure.add_scatter(
        x=history.index,
        y=history["composite_score"],
        name="score",
        line={"color": "#222", "width": 1.4},
    )
    figure.update_layout(
        height=320, yaxis_title="score", yaxis_range=[0, 100], xaxis_title="", showlegend=False
    )
    return figure


# ------------------------------------------------------------------- journal


def _journal(version: str, history: pd.DataFrame) -> None:
    st.subheader("단계별 일지")
    spans = regime_spans(history)
    if not spans:
        st.info("기록된 단계가 없습니다.")
        return

    prices = load("price_history", ("QQQ",), days=(history.index[-1] - history.index[0]).days + 30)
    rows = []
    for start, end, regime in spans:
        window = history.loc[start:end]
        rows.append(
            {
                "진입일": start.date(),
                "단계": regime,
                "거래일": len(window),
                "평균 점수": window["composite_score"].mean(),
                "레버리지": window["target_leverage"].mean(),
                "구간 QQQ": _return(prices, start, end),
                "점수가 다른 칸을 가리킨 날": int(
                    sum(
                        1
                        for raw, actual in zip(window["raw_regime"], window["regime"], strict=True)
                        if raw and raw != actual and raw != UNKNOWN_REGIME
                    )
                ),
            }
        )

    frame = pd.DataFrame(rows).iloc[::-1]
    st.dataframe(
        frame.style.format(
            {
                "평균 점수": "{:.1f}",
                "레버리지": "{:.2f}x",
                "구간 QQQ": lambda value: "—" if pd.isna(value) else f"{value * 100:+.1f}%",
            }
        ),
        hide_index=True,
        width="stretch",
        height=min(560, 38 * (len(frame) + 1)),
    )
    st.caption(
        "맨 윗줄이 진행 중인 단계입니다. «구간 QQQ» 는 그 기간 QQQ 종가의 수익률이며, "
        "전략의 수익률이 아닙니다 — 전략 성과는 **성과** 탭에 있습니다."
    )

    events = load("regime_events", version)
    if not events.empty:
        st.subheader("단계 변경 이벤트")
        st.dataframe(events, hide_index=True, width="stretch")


def _return(prices: pd.DataFrame, start: object, end: object) -> float | None:
    if prices.empty or "QQQ" not in prices:
        return None
    window = prices.loc[start:end, "QQQ"].dropna()  # type: ignore[misc]
    if len(window) < 2:
        return None
    return float(window.iloc[-1] / window.iloc[0] - 1.0)


# --------------------------------------------------------------------- locks


def _locks(history: pd.DataFrame) -> None:
    st.subheader("잠금 통계")
    st.caption(
        "이 전략이 실제로 얼마나 느린지를 보여줍니다. 반응이 느린 것은 버그가 "
        "아니라 거래를 줄이려고 건 장치이고, 그 대가가 여기에 나옵니다."
    )

    total = len(history)
    apart = [
        raw and raw != actual and raw != UNKNOWN_REGIME
        for raw, actual in zip(history["raw_regime"], history["regime"], strict=True)
    ]
    locked = int(sum(apart))

    counts: dict[str, int] = dict.fromkeys(LOCK_LABELS, 0)
    for codes in history["reason_codes"]:
        for brake in locks(codes):
            counts[brake.kind] = counts.get(brake.kind, 0) + 1

    changes = int((history["regime"] != history["regime"].shift()).sum()) - 1

    columns = st.columns(4)
    columns[0].metric(
        "점수와 단계가 어긋난 날",
        f"{locked / total * 100:.1f}%" if total else "—",
        delta=f"{locked} / {total}일",
        delta_color="off",
    )
    columns[1].metric("최소 유지 기간에 막힌 날", counts.get("min_duration", 0))
    columns[2].metric("히스테리시스에 막힌 날", counts.get("hysteresis", 0))
    columns[3].metric(
        "실제 단계 변경",
        max(changes, 0),
        delta=f"평균 {total // changes}거래일에 1회" if changes > 0 else None,
        delta_color="off",
    )

    reported = {LOCK_LABELS[kind]: value for kind, value in counts.items() if value}
    if reported:
        figure = px.bar(
            x=list(reported.values()),
            y=list(reported),
            orientation="h",
            labels={"x": "일수", "y": ""},
        )
        figure.update_traces(marker_color=LOCK_COLOUR)
        figure.update_layout(height=180, margin={"l": 8, "r": 8, "t": 10, "b": 30})
        st.plotly_chart(figure, width="stretch")
    else:
        st.info("이 기간에는 제동이 걸린 날이 없습니다.")

    st.caption(
        "한 날에 두 규칙이 동시에 기록될 수 있어 원인별 합은 어긋난 날 수와 다를 수 "
        "있습니다. 규칙 자체의 설명은 **전략 설명** 탭 «2·3. 점수를 단계로 나눈다» 에 "
        "있습니다."
    )

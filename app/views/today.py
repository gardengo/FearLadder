"""Today's signal: which rung the ladder is on, and why it is not another one.

The page answers three questions in order — where the score stands, which rung
is actually held, and what is holding it there. The third is the one the stored
state answers best and the old page threw away: the transition engine writes its
reasoning into ``reason_codes`` every day, including the brake that stopped a
move and how far it has counted.
"""

from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.pipeline.queries import is_signal_stale
from views.common import (
    LOCK_COLOUR,
    REGIME_COLOURS,
    Rung,
    gates,
    ladder,
    load,
    locks,
    performance_report,
    reasons_of,
    regime_palette,
    rung_of,
    streak,
    weekly_indicators,
)

#: Enough history to measure the current run of days without pulling years the
#: page never draws. The longest brake is 75 trading days.
STREAK_DAYS = 400

HERE = "● 지금 여기"


def _hysteresis_points(report: dict | None) -> float:
    transition = (report or {}).get("parameters", {}).get("transition", {})
    return float(transition.get("hysteresis") or 0.0)


def render(version: str | None) -> None:
    st.header("오늘의 신호")
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

    report = performance_report()
    rungs = ladder(report)
    history = load("state_history", version, days=STREAK_DAYS)
    run = streak(history)

    _headline(state, run)
    _ladder(state, rungs, report)
    _lock(state, rungs, report, run)

    left, right = st.columns([1, 1])
    with left:
        _allocation(version, state, rungs)
    with right:
        _grounds(state)

    _indicators(state, report)

    st.caption(
        f"strategy_version `{state['strategy_version']}` · "
        f"data_version `{state['data_version'] or '—'}` · "
        f"parameter_version `{state['parameter_version'] or '—'}` · "
        f"data quality `{state['data_quality_status']}`"
    )


# ------------------------------------------------------------------ headline


def _headline(state: dict, run) -> None:  # type: ignore[no-untyped-def]
    columns = st.columns(4)

    held = f"{run.days}일째" if run and run.regime == state["regime"] else None
    columns[0].metric("Regime", str(state["regime"]), delta=held, delta_color="off")

    score = state["composite_score"]
    previous = state["previous_score"]
    change = None
    if score is not None and previous is not None:
        change = f"{score - float(previous):+.1f}"
    columns[1].metric(
        "Market Score",
        f"{score:.1f}" if score is not None else "—",
        delta=change,
        delta_color="off",
        help="0 = 극단적 공포, 100 = 극단적 탐욕. delta 는 직전 거래일 대비.",
    )

    leverage = state["target_leverage"]
    columns[2].metric(
        "Target Leverage",
        f"{leverage:.2f}x" if leverage is not None else "—",
        help="1·QQQ + 2·QLD + 3·TQQQ. 실현 수익률의 배수를 뜻하지 않습니다.",
    )
    columns[3].metric("Last Update", str(state["observation_date"]))


# -------------------------------------------------------------------- ladder


def _ladder(state: dict, rungs: list[Rung], report: dict | None) -> None:
    if not rungs:
        return
    st.subheader("사다리의 어느 칸인가")

    regime = str(state["regime"])
    raw = str(state["raw_regime"] or "")
    score = state["composite_score"]
    hysteresis = _hysteresis_points(report)
    lines = gates(rungs, regime, hysteresis)

    st.plotly_chart(
        _scale(rungs, score, lines), width="stretch", config={"displayModeBar": False}
    )

    marks = {regime: HERE}
    if raw and raw != regime and raw != UNKNOWN_REGIME:
        marks[raw] = "◌ 점수가 가리킴"
    frame = pd.DataFrame(
        {
            "": [marks.get(rung.label, "") for rung in rungs],
            "단계": [rung.label for rung in rungs],
            "점수 구간": [rung.band for rung in rungs],
            "레버리지": [rung.leverage for rung in rungs],
            "구성": [rung.composition() for rung in rungs],
        }
    )
    st.dataframe(
        frame.style.format({"레버리지": "{:.2f}x"}).apply(
            lambda _: [
                "font-weight: 600" if marks.get(rung.label) == HERE else ""
                for rung in rungs
            ],
            axis=0,
        ),
        hide_index=True,
        width="stretch",
        height=38 * (len(rungs) + 1),
    )
    st.caption(
        "**구성은 사다리가 제안하는 비중입니다.** 실제 목표 비중은 추세 필터와 "
        "바닥 확인 게이트를 통과한 뒤의 결과이며, 아래 «목표 비중» 에 있습니다."
    )


def _scale(rungs: list[Rung], score: float | None, lines) -> go.Figure:  # type: ignore[no-untyped-def]
    """The 0-100 score line, its bands, and the two lines it has to clear."""
    palette = regime_palette([rung.label for rung in rungs])
    figure = go.Figure()
    for index, rung in enumerate(rungs):
        figure.add_shape(
            type="rect",
            x0=rung.low,
            x1=rung.high,
            y0=0,
            y1=1,
            fillcolor=palette.get(rung.label, REGIME_COLOURS[index % len(REGIME_COLOURS)]),
            opacity=0.75,
            line_width=0,
            layer="below",
        )
        figure.add_annotation(
            x=(rung.low + rung.high) / 2,
            y=0.5,
            text=rung.label,
            showarrow=False,
            font={"size": 10, "color": "#222"},
        )

    for value, label in ((lines.down, "이 아래로"), (lines.up, "이 위로")):
        if value is None:
            continue
        figure.add_vline(
            x=value,
            line={"color": LOCK_COLOUR, "width": 1.5, "dash": "dash"},
            annotation_text=f"{label} {value:.0f}",
            annotation_position="bottom",
            annotation_font={"size": 10, "color": LOCK_COLOUR},
        )

    if score is not None:
        figure.add_vline(x=score, line={"color": "#111", "width": 2})
        figure.add_annotation(
            x=score,
            y=1.0,
            yanchor="bottom",
            text=f"<b>{score:.1f}</b>",
            showarrow=False,
            font={"size": 13},
        )

    figure.update_xaxes(range=[0, 100], tickvals=[0, *(_edges(rungs)), 100])
    figure.update_yaxes(range=[0, 1], visible=False)
    figure.update_layout(
        height=140,
        margin={"l": 8, "r": 8, "t": 22, "b": 44},
        showlegend=False,
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return figure


def _edges(rungs: list[Rung]) -> list[float]:
    return [rung.high for rung in rungs[:-1]]


# ---------------------------------------------------------------------- lock


def _lock(state: dict, rungs: list[Rung], report: dict | None, run) -> None:  # type: ignore[no-untyped-def]
    """Why the score is pointing at one rung while another is held."""
    regime = str(state["regime"])
    raw = str(state["raw_regime"] or "")
    if not raw or raw in (regime, UNKNOWN_REGIME):
        return

    brakes = locks(state["reason_codes"])
    leverage = state["target_leverage"]
    held = rung_of(rungs, regime)

    st.warning(
        f"**점수는 `{raw}` 를 가리키지만 단계는 `{regime}` 입니다.** "
        + (
            f"목표 레버리지는 {regime} 의 {leverage:.2f}x 를 유지합니다."
            if leverage is not None
            else ""
        )
    )

    for brake in brakes:
        if brake.kind == "hysteresis":
            _hysteresis(brake, state, rungs, regime, report)
        else:
            _countdown(brake)

    if not brakes:
        st.caption(
            "오늘 기록된 제동 사유가 없습니다 — 저장된 `reason_codes` 를 확인하세요."
        )
    elif run and held is not None:
        # Entry date and the rung before it, but no second day count. The brake
        # above already shows the engine's own counter, and that counter can sit
        # a day ahead of the stored rows — it steps on every scored day, while a
        # market_states row is only written for a day the worker completed. Two
        # nearly-equal numbers side by side read as a bug in the page.
        st.caption(
            f"`{regime}` 진입 {run.since:%Y-%m-%d}"
            + (f" · 직전 `{run.previous}`" if run.previous else "")
        )


def _hysteresis(  # type: ignore[no-untyped-def]
    brake, state: dict, rungs: list[Rung], regime: str, report: dict | None
) -> None:
    hysteresis = _hysteresis_points(report)
    lines = gates(rungs, regime, hysteresis)
    score = state["composite_score"]
    raw = str(state["raw_regime"] or "")

    current = rung_of(rungs, regime)
    target = rung_of(rungs, raw)
    downward = bool(current and target and target.index < current.index)
    line = lines.down if downward else lines.up

    st.markdown(f"- {brake.text}")
    if line is None or score is None:
        return
    remaining = abs(score - line)
    direction = "이하로" if downward else "이상으로"
    st.markdown(
        f"  - 넘어야 할 선 **{line:.1f}** {direction} — 오늘 **{score:.1f}**, "
        f"**{remaining:.1f}점 남았습니다.**"
    )


def _countdown(brake) -> None:  # type: ignore[no-untyped-def]
    st.markdown(f"- {brake.text}")
    if brake.progress is None:
        return
    elapsed, required = brake.progress
    st.progress(min(elapsed / required, 1.0))
    st.caption(
        f"{elapsed} / {required}"
        + (" — 다음 거래일부터 해제 가능" if brake.is_ready else f" — {required - elapsed}일 더")
    )


# ---------------------------------------------------------------- allocation


def _allocation(version: str | None, state: dict, rungs: list[Rung]) -> None:
    st.subheader("목표 비중")
    allocation = load("latest_allocation", version)
    if allocation.empty:
        st.write("배분 없음 (상태 불명)")
        return

    proposed = rung_of(rungs, str(state["regime"]))
    actual = {str(row.asset): float(row.weight) for row in allocation.itertuples()}
    if proposed and proposed.weights and not _same(proposed.weights, actual):
        st.caption(
            f"사다리는 **{proposed.composition()}** 을 제안했지만, 필터를 거친 뒤 "
            "아래와 같이 바뀌었습니다."
        )

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


def _same(proposed: dict[str, float], actual: dict[str, float], tolerance: float = 5e-3) -> bool:
    keys = set(proposed) | set(actual)
    return all(abs(proposed.get(key, 0.0) - actual.get(key, 0.0)) <= tolerance for key in keys)


# ------------------------------------------------------------------ grounds


def _grounds(state: dict) -> None:
    st.subheader("판단 근거")
    st.caption(f"이전 레짐: {state['previous_regime'] or '—'}")

    items = reasons_of(state["reason_codes"])
    # ``lock`` codes are deliberately absent: the panel above already explains
    # the brake in full, and repeating it here read as two different findings.
    for kind in ("change", "trend", "gate", "info"):
        for item in items:
            if item.kind == kind:
                st.markdown(f"- {item.text}")

    with st.expander("원본 reason codes"):
        st.code("\n".join(item.code for item in items) or "—", language="text")


# --------------------------------------------------------------- indicators


def _indicators(state: dict, report: dict | None) -> None:
    breakdown = json.loads(str(state["score_breakdown"] or "{}"))
    if not breakdown:
        return

    configured = set((report or {}).get("parameters", {}).get("indicator_weights") or {})
    missing = configured - set(breakdown)
    if missing:
        weekly = missing & weekly_indicators()
        if weekly:
            st.caption(
                f"오늘 점수에 반영되지 않은 지표 {len(weekly)}개 (주간 지표라 "
                f"발표일에만 값이 있습니다): {', '.join(sorted(weekly))}"
            )
        if unexpected := missing - weekly:
            st.warning(
                f"점수를 계산하지 못한 지표 {len(unexpected)}개: "
                f"{', '.join(sorted(unexpected))} — 가중치는 나머지 지표로 "
                "재분배되었습니다. 데이터를 확인하세요."
            )

    with st.expander("점수를 밀어낸 지표"):
        frame = (
            pd.DataFrame({"지표": list(breakdown), "점수": list(breakdown.values())})
            .assign(거리=lambda f: (f["점수"] - 50).abs())
            .sort_values("거리", ascending=False)
            .drop(columns="거리")
        )
        fear, greed = frame[frame["점수"] < 50].head(3), frame[frame["점수"] >= 50].head(3)
        columns = st.columns(2)
        columns[0].caption("공포 쪽으로 가장 멀리")
        columns[0].dataframe(
            fear.style.format({"점수": "{:.1f}"}), hide_index=True, width="stretch"
        )
        columns[1].caption("탐욕 쪽으로 가장 멀리")
        columns[1].dataframe(
            greed.style.format({"점수": "{:.1f}"}), hide_index=True, width="stretch"
        )
        st.caption(
            "지표 하나하나의 정의와 가중치는 **전략 설명** 탭에 있습니다. "
            "0 = 공포, 100 = 탐욕."
        )

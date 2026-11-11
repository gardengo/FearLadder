"""Today's signal: which rung the ladder is on, and why it is not another one.

The page answers four questions in order — where the score stands, which rung
is actually held, what is holding it there, and how far the market is from the
handrail. The third is the one the stored state answers best and the old page
threw away: the transition engine writes its reasoning into ``reason_codes``
every day, including the brake that stopped a move and how far it has counted.

The fourth was missing altogether. The trend filter is the rule that takes the
leverage away, and the only trace of it was one sentence among the reason
codes. How close the market was to setting it off could not be read anywhere.
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
    CORE,
    RAILING_COLOUR,
    REGIME_COLOURS,
    Railing,
    Rung,
    band_opacity,
    gates,
    halo,
    ladder,
    load,
    lock_colour,
    locks,
    performance_report,
    railing,
    readable_on,
    reasons_of,
    regime_palette,
    rung_of,
    streak,
    updated_daily,
    weekly_indicators,
)

#: Enough history to measure the current run of days without pulling years the
#: page never draws. The longest brake is 75 trading days.
STREAK_DAYS = 400

HERE = "● 지금 여기"

#: Handrail status -> (the words under the headline number, their colour).
RAIL_BADGES = {
    "engaged": ("난간 작동 · {cap:.2f}x 제한", "red"),
    "shallow": ("작동선 아래 · 미작동", "orange"),
    "clear": ("난간 해제", "off"),
    "unknown": ("추세 확인 불가", "off"),
}


def _hysteresis_points(report: dict | None) -> float:
    transition = (report or {}).get("parameters", {}).get("transition", {})
    return float(transition.get("hysteresis") or 0.0)


def render(version: str | None) -> None:
    st.header("오늘의 신호")
    updated_daily(version)
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
    readings = _trend_readings(report)
    rail = _railing(state, report, readings)

    _headline(state, run, rail)
    _ladder(state, rungs, report)
    _lock(state, rungs, report, run)
    _handrail(rail, readings, history, report)

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


def _headline(state: dict, run, rail: Railing | None) -> None:  # type: ignore[no-untyped-def]
    columns = st.columns(5 if rail else 4)

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
    if rail:
        # Next to the leverage it can take away, not further down the page:
        # this is the number that says how safe the one beside it is.
        columns[3].metric(
            "200일선 이격",
            _signed(rail.distance),
            delta=RAIL_BADGES[rail.status][0].format(cap=rail.cap),
            delta_color=RAIL_BADGES[rail.status][1],
            delta_arrow="off",
            help=(
                "QQQ 종가가 200일 이동평균보다 얼마나 위/아래인가. "
                f"{_signed(rail.engage_below, 0)} 아래로 내려가고 52주 고점 대비 낙폭 조건까지 "
                f"충족되면 난간이 레버리지를 {rail.cap:.2f}x 로 묶습니다. "
                "자세한 거리는 아래 «난간» 에 있습니다."
            ),
        )
    columns[-1].metric("Last Update", str(state["observation_date"]))


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
        height=35 * (len(rungs) + 1) + 3,
    )
    st.caption(
        "**구성은 사다리가 제안하는 비중입니다.** 실제 목표 비중은 추세 필터와 "
        "바닥 확인 게이트를 통과한 뒤의 결과이며, 아래 «목표 비중» 에 있습니다."
    )


def _scale(rungs: list[Rung], score: float | None, lines) -> go.Figure:  # type: ignore[no-untyped-def]
    """The 0-100 score line, its bands, and the two lines it has to clear."""
    palette = regime_palette([rung.label for rung in rungs])
    amber = lock_colour()
    figure = go.Figure()
    for index, rung in enumerate(rungs):
        fill = palette.get(rung.label, REGIME_COLOURS[index % len(REGIME_COLOURS)])
        figure.add_shape(
            type="rect",
            x0=rung.low,
            x1=rung.high,
            y0=0,
            y1=1,
            fillcolor=fill,
            opacity=0.9,
            line_width=0,
            layer="below",
        )
        figure.add_annotation(
            x=(rung.low + rung.high) / 2,
            y=0.5,
            text=rung.label,
            showarrow=False,
            font={"size": 10, "color": readable_on(fill)},
        )

    for value, label in ((lines.down, "이 아래로"), (lines.up, "이 위로")):
        if value is None:
            continue
        figure.add_vline(
            x=value,
            line={"color": amber, "width": 1.5, "dash": "dash"},
            annotation_text=f"{label} {value:.0f}",
            annotation_position="bottom",
            annotation_font={"size": 10, "color": amber},
        )

    if score is not None:
        # Two lines, not one: today's score is the single mark on this strip a
        # reader must never lose, so it does not depend on getting the theme
        # right. The light layer shows on a dark ground, the dark one on light.
        for layer in halo():
            figure.add_vline(x=score, line=layer)
        figure.add_annotation(
            x=score,
            y=1.0,
            yanchor="bottom",
            text=f"<b>{score:.1f}</b>",
            showarrow=False,
            font={"size": 13, "color": "#ffffff"},
            bgcolor=CORE,
            borderpad=3,
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

    # The engine stops at the first brake that fires, so a day blocked by
    # hysteresis never records how the minimum-duration count is doing — and a
    # reader is left thinking the score line is the only thing in the way. It
    # usually is not: clearing the line only makes the move a *candidate*.
    if brakes and not any(brake.kind == "min_duration" for brake in brakes):
        _minimum_duration_outlook(regime, report, run)

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


def _minimum_duration_outlook(regime: str, report: dict | None, run) -> None:  # type: ignore[no-untyped-def]
    """What still stands between "the line is cleared" and "the rung changes"."""
    required = (report or {}).get("parameters", {}).get("transition", {}).get(
        "minimum_duration_days"
    )
    if not required or run is None or run.regime != regime:
        return

    held = run.days
    if held > required:
        st.markdown(
            f"- 최소 유지 {required}거래일은 이미 채웠습니다 "
            f"(현재 {held}거래일째) — **선만 넘으면 단계가 바뀝니다.**"
        )
        return

    st.markdown(
        f"- **선을 넘어도 그날 바로 바뀌지는 않습니다.** 최소 유지 "
        f"{required}거래일을 채워야 하는데, `{regime}` 은 {run.since:%Y-%m-%d} "
        f"진입 후 {held}거래일째입니다."
    )
    st.progress(min(held / required, 1.0))
    st.caption(
        f"{held} / {required} — 약 {required - held}거래일 더. "
        "그때까지는 점수가 더 내려가도 단계는 그대로이고, 사유만 "
        "`BLOCKED_BY_MIN_DURATION` 으로 바뀝니다. "
        "급락에 먼저 반응하는 것은 사다리가 아니라 추세 필터(난간)입니다."
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


# ------------------------------------------------------------------ handrail


def _trend_spec(report: dict | None) -> dict:
    return (report or {}).get("parameters", {}).get("trend_filter") or {}


def _trend_readings(report: dict | None) -> pd.DataFrame:
    """The stored readings the trend filter compared, day by day."""
    spec = _trend_spec(report)
    names = tuple(name for name in (spec.get("indicator"), spec.get("depth_indicator")) if name)
    return load("indicator_history", names, days=STREAK_DAYS) if names else pd.DataFrame()


def _railing(state: dict, report: dict | None, readings: pd.DataFrame) -> Railing | None:
    if state["regime"] == UNKNOWN_REGIME:
        return None
    day = pd.Timestamp(str(state["observation_date"]))
    row = readings.loc[day].to_dict() if day in readings.index else {}
    prices = load("price_history", ("QQQ",), days=STREAK_DAYS)
    close = prices["QQQ"].get(day) if "QQQ" in prices else None
    rail = railing(report, state["reason_codes"], row, close)
    if rail is None or (rail.engaged is None and rail.distance is None):
        return None
    return rail


def _handrail(
    rail: Railing | None, readings: pd.DataFrame, history: pd.DataFrame, report: dict | None
) -> None:
    """How far the market is from the rule that takes the leverage away."""
    if rail is None:
        return
    st.subheader("난간 — 200일선과의 거리")
    _rail_verdict(rail)

    name = _trend_spec(report).get("indicator")
    series = readings[name].dropna() if name in readings else pd.Series(dtype=float)
    _rail_numbers(rail, series)

    if len(series) > 1:
        engaged = _engaged_days(history).reindex(series.index, fill_value=False).astype(bool)
        st.plotly_chart(
            _rail_chart(series, rail, engaged),
            width="stretch",
            config={"displayModeBar": False},
        )
        st.caption(
            f"선이 **작동선({_signed(rail.engage_below, 0)})** 아래로 내려가고 낙폭 조건까지 "
            f"충족되면 난간이 작동하고, 그 뒤로는 **해제선({_signed(rail.release_above, 0)})** "
            "위로 올라와야 해제됩니다. 두 선 사이에서는 직전 상태가 그대로 갑니다. "
            + (
                "굵은 붉은 선이 실제로 난간이 작동하던 날입니다."
                if engaged.any()
                else "이 기간에는 난간이 작동한 날이 없었습니다 — 선 아래로 내려간 날이 "
                "있어도 낙폭 조건이 맞지 않으면 작동하지 않습니다."
            )
        )


def _rail_verdict(rail: Railing) -> None:
    """One sentence: is the handrail on, and what would change that."""
    cap = f"{rail.cap:.2f}x"
    if rail.status == "engaged":
        if rail.distance is None:
            st.error(
                f"**난간 작동 중 — 레버리지를 {cap} 로 묶고 있습니다.** 추세 지표를 "
                "읽지 못해 방어적으로 묶은 상태입니다."
            )
            return
        left = rail.to_release or 0.0
        st.error(
            f"**난간 작동 중 — 레버리지를 {cap} 로 묶고 있습니다.** QQQ 는 200일선 대비 "
            f"{_signed(rail.distance)}. 해제선 {_signed(rail.release_above, 0)} 위로 올라와야 "
            "풀립니다"
            + (
                f" — **{left * 100:.1f}%p 남았습니다** "
                f"(QQQ 종가 약 {_dollars(rail.price_at(rail.release_above), markdown=True)})."
                if left > 0
                else " — 선은 이미 넘었고, 다음 배치에서 해제 여부가 확정됩니다."
            )
        )
        return

    depth_rule = ""
    if rail.min_depth is not None and rail.depth is not None:
        depth_rule = (
            f"52주 고점 대비 낙폭이 {rail.min_depth * 100:.0f}% 에 닿아야 하는데 "
            f"지금은 {rail.depth * 100:.1f}% 입니다"
        )

    if rail.status == "shallow":
        st.warning(
            f"**작동선 아래({_signed(rail.distance)})지만 난간은 아직 작동하지 않습니다.** "
            f"작동선 {_signed(rail.engage_below, 0)} 은 넘었는데, "
            f"{depth_rule or '깊이 조건이 남았습니다'}. "
            + (
                f"낙폭이 **{(rail.depth_shortfall or 0) * 100:.1f}%p 더** 깊어지면 "
                f"(QQQ 종가 약 {_dollars(rail.depth_price, markdown=True)}) 레버리지가 "
                f"{cap} 로 묶입니다."
                if rail.depth_shortfall
                else f"조건이 맞으면 레버리지가 {cap} 로 묶입니다."
            )
        )
        return

    side = "위" if (rail.distance or 0.0) >= 0 else "아래"
    message = (
        f"**난간 해제 — QQQ 는 200일선보다 {abs(rail.distance or 0.0) * 100:.1f}% {side}.** "
        f"작동선 {_signed(rail.engage_below, 0)} 까지 **{(rail.to_engage or 0) * 100:.1f}%p** "
        "남았습니다. "
    )
    if depth_rule and not rail.deep_enough:
        message += f"선을 넘더라도 {depth_rule} — 두 조건이 다 맞아야 {cap} 로 묶입니다."
    elif rail.min_depth is not None:
        message += f"낙폭 조건은 이미 충족돼 있어, 선만 넘으면 레버리지가 {cap} 로 묶입니다."
    else:
        message += f"선을 넘으면 레버리지가 {cap} 로 묶입니다."
    # Green only while price is actually above its average. Below it and still
    # "clear" is true, but it is not the day to be told everything is fine.
    (st.success if (rail.distance or 0.0) >= 0 else st.info)(message)


def _rail_numbers(rail: Railing, series: pd.Series) -> None:
    columns = st.columns(4)

    columns[0].metric("QQQ 종가", _dollars(rail.close))
    columns[0].caption(f"200일선 {_dollars(rail.average, markdown=True)}")

    change = None
    if rail.distance is not None and len(series) > 1:
        change = f"{(rail.distance - float(series.iloc[-2])) * 100:+.1f}%p"
    columns[1].metric(
        "200일선 이격", _signed(rail.distance), delta=change,
        help="종가 ÷ 200일 이동평균 − 1. 변화는 직전 거래일 대비.",
    )

    if rail.status == "engaged":
        columns[2].metric(
            "해제선까지",
            "—" if rail.to_release is None else f"{max(rail.to_release, 0) * 100:.1f}%p",
        )
        columns[2].caption(
            f"해제선 {_signed(rail.release_above, 0)} = "
            f"{_dollars(rail.price_at(rail.release_above), markdown=True)}"
        )
    else:
        room = rail.to_engage
        columns[2].metric(
            "작동선까지",
            "—" if room is None else (f"{room * 100:.1f}%p" if room >= 0 else "이미 넘음"),
        )
        columns[2].caption(
            f"작동선 {_signed(rail.engage_below, 0)} = "
            f"{_dollars(rail.price_at(rail.engage_below), markdown=True)}"
        )

    columns[3].metric(
        "52주 고점 대비 낙폭",
        "—" if rail.depth is None else f"{rail.depth * 100:.1f}%",
        help="난간이 작동하려면 200일선 조건과 함께 이 낙폭 조건도 맞아야 합니다.",
    )
    if rail.min_depth and rail.depth is not None:
        columns[3].progress(min(rail.depth / rail.min_depth, 1.0))
        columns[3].caption(
            f"조건 {rail.min_depth * 100:.0f}% 이상 = "
            f"{_dollars(rail.depth_price, markdown=True)} 이하"
        )

    if rail.status != "engaged" and rail.trigger_price and rail.close:
        st.caption(
            "두 조건이 함께 맞는 QQQ 종가는 약 "
            f"**{_dollars(rail.trigger_price, markdown=True)}** "
            f"(오늘 종가 대비 {_signed(rail.trigger_price / rail.close - 1)}) 입니다. "
            "오늘의 200일선과 52주 고점을 그대로 둔 값이라 매일 조금씩 움직입니다."
        )


def _engaged_days(history: pd.DataFrame) -> pd.Series:
    """Which stored days the handrail was on, read off the reason codes."""
    codes = history.get("reason_codes") if history is not None else None
    if codes is None:
        return pd.Series(dtype=bool)
    return codes.astype(str).str.contains("TREND_BROKEN|LEVERAGE_CAPPED_BY_DEFAULT")


def _rail_chart(distance: pd.Series, rail: Railing, on: pd.Series) -> go.Figure:
    """The distance from the 200-day average over time, against its two lines.

    ``on`` marks the days the handrail was engaged, on ``distance``'s index.
    """
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=distance.index,
            y=distance,
            name="200일선 이격",
            line={"width": 2},
            hovertemplate="%{y:+.1%}<extra>200일선 이격</extra>",
        )
    )
    if on.any():
        # Drawn on the line rather than shaded behind it: the handrail stays on
        # between the two lines, which a band keyed to the engage line cannot
        # show and a recoloured stretch of the line shows without a word.
        figure.add_trace(
            go.Scatter(
                x=distance.index,
                y=distance.where(on),
                name="난간이 작동하던 날",
                mode="lines+markers",
                line={"color": RAILING_COLOUR, "width": 4},
                marker={"size": 4, "color": RAILING_COLOUR},
                connectgaps=False,
                hoverinfo="skip",
            )
        )

    low = min(float(distance.min()), rail.engage_below)
    high = max(float(distance.max()), rail.release_above)
    pad = (high - low) * 0.12 or 0.01
    low, high = low - pad, high + pad

    alpha = band_opacity() * 0.6
    figure.add_hrect(
        y0=low, y1=rail.engage_below, fillcolor=RAILING_COLOUR,
        opacity=alpha, line_width=0, layer="below",
    )
    if rail.release_above > rail.engage_below:
        figure.add_hrect(
            y0=rail.engage_below, y1=rail.release_above, fillcolor="#9e9e9e",
            opacity=alpha, line_width=0, layer="below",
        )

    # Each label on the side of its line that it describes: the engage line's
    # under it, in the zone it opens; the release line's over it. Three labels
    # stacked on the same side sat on top of one another.
    rules = [(0.0, "200일선", "bottom", {"color": "#8c8f96", "width": 1})]
    rules.append(
        (rail.engage_below, f"작동선 {_signed(rail.engage_below, 0)} — 이 아래가 작동 구간",
         "top", {"color": RAILING_COLOUR, "width": 1.5, "dash": "dash"})
    )
    if rail.release_above != rail.engage_below:
        rules.append(
            (rail.release_above, f"해제선 {_signed(rail.release_above, 0)}",
             "bottom", {"color": "#8c8f96", "width": 1.5, "dash": "dash"})
        )
    for level, label, anchor, line in rules:
        figure.add_hline(y=level, line=line)
        figure.add_annotation(
            x=0.0, xref="x domain", xanchor="left", y=level, yanchor=anchor,
            text=label, showarrow=False, font={"size": 10},
        )

    # Today, marked so it survives either theme: a dark point with a light rim.
    last_day, last_value = distance.index[-1], float(distance.iloc[-1])
    figure.add_trace(
        go.Scatter(
            x=[last_day], y=[last_value], mode="markers", showlegend=False,
            marker={"size": 9, "color": CORE, "line": {"color": "#f5f5f5", "width": 2}},
            hoverinfo="skip",
        )
    )
    figure.add_annotation(
        x=last_day, y=last_value, xanchor="left", xshift=10,
        text=f"<b>{_signed(last_value)}</b>", showarrow=False,
        font={"size": 13, "color": "#ffffff"}, bgcolor=CORE, borderpad=3,
    )

    figure.update_yaxes(range=[low, high], tickformat="+.0%")
    figure.update_layout(
        height=300,
        margin={"l": 8, "r": 72, "t": 10, "b": 8},
        hovermode="x unified",
        showlegend=bool(on.any()),
        legend={"orientation": "h", "y": 1.12, "x": 0},
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return figure


def _signed(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:+.{digits}f}%"


def _dollars(value: float | None, *, markdown: bool = False) -> str:
    """A price. Escaped for markdown, where a pair of ``$`` starts a formula."""
    if value is None:
        return "—"
    prefix = "\\$" if markdown else "$"
    return f"{prefix}{value:,.2f}"


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

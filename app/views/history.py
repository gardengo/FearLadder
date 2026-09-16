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
from plotly.subplots import make_subplots

from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.pipeline.queries import regime_spans
from views.common import (
    UNKNOWN_COLOUR,
    band_opacity,
    halo,
    ladder,
    load,
    lock_colour,
    locks,
    performance_report,
    readable_on,
    regime_event_table,
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

SECTIONS = ("차트", "단계별 일지", "잠금 통계")

#: Sleeves from the most aggressive down, so the stack reads the way the
#: ladder table does. Shaded by leverage rather than by an arbitrary palette:
#: denser means more exposure.
ASSET_ROWS = {"TQQQ": "TQQQ 3x", "QLD": "QLD 2x", "QQQ": "QQQ 1x", "CASH": "현금"}
SLEEVE_COLOURS = {
    "TQQQ": "#7b3294",
    "QLD": "#c2a5cf",
    "QQQ": "#a6dba0",
    "CASH": "#cfcfcf",
}

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

    # Deliberately not st.tabs: a dataframe first painted inside a *nested*
    # tab is laid out while its panel is hidden and comes back collapsed to a
    # single column until something forces a re-layout. A segmented control
    # reads the same and renders one section at a time for real.
    section = st.segmented_control(
        "구역", SECTIONS, default=SECTIONS[0], label_visibility="collapsed"
    )
    if section == SECTIONS[1]:
        _journal(version, history)
    elif section == SECTIONS[2]:
        _locks(history)
    else:
        _charts(version, history, days)

    unknown_days = int((history["regime"] == UNKNOWN_REGIME).sum())
    if unknown_days:
        st.warning(f"이 기간에 신호를 만들지 못한 날이 {unknown_days}일 있습니다.")


# -------------------------------------------------------------------- charts


def _charts(version: str, history: pd.DataFrame, days: int) -> None:
    """One figure, one timeline.

    These four series only mean anything against each other — the score falls,
    the ladder eventually follows, the sleeves change, the leverage steps down.
    As four charts with four independent x-axes that story had to be assembled
    by eye. Stacked rows on a shared axis tell it directly: a vertical line
    through the figure is one day, everywhere.
    """
    palette = regime_palette(history["regime"].dropna().unique().tolist())
    prices = load("price_history", ("QQQ",), days=days)
    allocations = load("allocation_history", version, days=days)
    spans = regime_spans(history)

    figure = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.40, 0.28, 0.32],
        specs=[[{"secondary_y": True}], [{}], [{"secondary_y": True}]],
        subplot_titles=(
            "QQQ 와 종합점수",
            "점수가 가리킨 칸과 실제로 선 칸",
            "목표 비중과 레버리지",
        ),
    )

    _price_and_score(figure, history, prices, palette, spans)
    _step_rows(figure, history)
    _sleeves(figure, history, allocations)

    figure.update_layout(
        height=920,
        hovermode="x unified",
        legend={"orientation": "h", "y": -0.06, "x": 0},
        margin={"l": 8, "r": 8, "t": 40, "b": 8},
        # The regime bands are drawn on the "below" layer, which sits under the
        # panel fill as well as under the traces — on an opaque panel they are
        # painted and then covered up.
        plot_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(figure, width="stretch")
    st.caption(
        "세 칸 모두 같은 날짜 축입니다 — 세로로 읽으면 그날의 점수·단계·보유가 "
        "한 줄로 보입니다. 위 칸의 칩이 그 구간의 단계이고, 가운데 칸에서 두 선이 "
        "벌어진 구간이 **잠금 구간**입니다. 아래 칸의 계단선이 그 결과로 실제 든 "
        "레버리지입니다."
    )


def _price_and_score(
    figure: go.Figure,
    history: pd.DataFrame,
    prices: pd.DataFrame,
    palette: dict,
    spans: list,
) -> None:
    """Price against the score that is judging it, on one pair of axes."""
    if not prices.empty:
        figure.add_trace(
            go.Scatter(x=prices.index, y=prices["QQQ"], name="QQQ", line={"width": 1.6}),
            row=1,
            col=1,
        )
    figure.add_trace(
        go.Scatter(
            x=history.index,
            y=history["composite_score"],
            name="종합점수",
            line={"width": 1.3, "dash": "dot"},
            opacity=0.85,
        ),
        row=1,
        col=1,
        secondary_y=True,
    )
    # After the traces, never before: a shape resolves ``row``/``col`` against
    # the axes the subplot already has, and on an empty panel it matches
    # nothing and is silently dropped.
    bounds = _price_bounds(prices)
    _shade(figure, spans, palette, bounds, row=1)
    if spans:
        _name_the_bands(
            figure, spans, palette, total=history.index[-1] - history.index[0], row=1
        )
    # Pinned, so the bands drawn in these coordinates fill the panel exactly.
    figure.update_yaxes(title_text="QQQ", range=list(bounds), row=1, col=1, secondary_y=False)
    figure.update_yaxes(
        title_text="점수", range=[0, 100], row=1, col=1, secondary_y=True, showgrid=False
    )


def _step_rows(figure: go.Figure, history: pd.DataFrame) -> None:
    """The ladder the score asked for, against the one actually held."""
    order = _rungs() or sorted(set(history["regime"].dropna()) - {UNKNOWN_REGIME})
    rank = {label: index for index, label in enumerate(order)}
    amber = lock_colour()

    # Filled between the two lines rather than shaded behind them. The score
    # points elsewhere on most days, so a band per divergence covered almost
    # the whole chart and said nothing; the gap's *height* is the information —
    # how many rungs apart the score and the ladder are.
    figure.add_trace(
        go.Scatter(
            x=history.index,
            y=history["raw_regime"].map(rank),
            name="점수가 가리킨 단계",
            line={"color": amber, "width": 1.4, "dash": "dash"},
            line_shape="hv",
        ),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=history.index,
            y=history["regime"].map(rank),
            name="실제 단계",
            line={"width": 2.4},
            line_shape="hv",
            fill="tonexty",
            fillcolor=_translucent(amber, 0.22),
        ),
        row=2,
        col=1,
    )
    figure.update_yaxes(
        tickmode="array",
        tickvals=list(range(len(order))),
        ticktext=order,
        range=[-0.5, len(order) - 0.5],
        row=2,
        col=1,
    )


def _sleeves(figure: go.Figure, history: pd.DataFrame, allocations: pd.DataFrame) -> None:
    """What is held, and the leverage that comes out of holding it.

    The same fact twice — leverage is 1·QQQ + 2·QLD + 3·TQQQ of these weights —
    so they belong on one pair of axes rather than side by side, where the eye
    has to carry a shape across the gap to see they agree.
    """
    if not allocations.empty:
        for asset in [name for name in ASSET_ROWS if name in allocations.columns]:
            figure.add_trace(
                go.Scatter(
                    x=allocations.index,
                    y=allocations[asset],
                    name=ASSET_ROWS[asset],
                    stackgroup="sleeves",
                    line={"width": 0},
                    fillcolor=SLEEVE_COLOURS[asset],
                    hovertemplate="%{y:.0%}<extra>" + ASSET_ROWS[asset] + "</extra>",
                ),
                row=3,
                col=1,
            )
    # Two layers again: this line has to stay legible over the sleeve fills in
    # either theme, and the fills are the same colours in both.
    for index, layer in enumerate(halo()):
        figure.add_trace(
            go.Scatter(
                x=history.index,
                y=history["target_leverage"],
                name="목표 레버리지",
                line={**layer, "shape": "hv"},
                # Neither layer takes a legend slot: the dark core's swatch is
                # invisible on a dark legend and the light one on a light legend,
                # and the right-hand axis already names this line.
                showlegend=False,
                hoverinfo="skip" if index == 0 else None,
            ),
            row=3,
            col=1,
            secondary_y=True,
        )
    figure.update_yaxes(
        title_text="비중", range=[0, 1], tickformat=".0%", row=3, col=1, secondary_y=False
    )
    figure.update_yaxes(
        title_text="레버리지 (x)", row=3, col=1, secondary_y=True, showgrid=False
    )


def _shade(
    figure: go.Figure, spans: list, palette: dict, bounds: tuple[float, float], *, row: int = 1
) -> None:
    """Paint one band per regime run, behind the traces.

    Drawn in the panel's own data coordinates rather than against its domain,
    and on the "below" layer. Both matter: Streamlit ships its own plotly.js,
    which is older than the ``layer="between"`` this wants and silently drops a
    shape that asks for it — the bands were built every run and never appeared.
    """
    alpha = band_opacity()
    low, high = bounds
    for start, end, regime in spans:
        figure.add_shape(
            type="rect",
            x0=start,
            x1=end,
            y0=low,
            y1=high,
            fillcolor=palette.get(regime, UNKNOWN_COLOUR),
            opacity=alpha,
            line_width=0,
            layer="below",
            row=row,
            col=1,
            secondary_y=False,
        )


def _name_the_bands(  # type: ignore[no-untyped-def]
    figure: go.Figure, spans: list, palette: dict, total, *, row: int = 1
) -> None:
    """Write each band's stage on the band, the way the ladder strip does.

    A legend of colours under the chart makes the reader hold seven hues in
    their head and look down every time. The name belongs on the band.
    """
    for start, end, regime in spans:
        # A chip needs room; below roughly a twentieth of the window the text
        # would overlap its neighbours and say less than the colour already does.
        if total and (end - start) / total < 0.05:
            continue
        fill = palette.get(regime, UNKNOWN_COLOUR)
        # Inside the plot, not above it: above the top edge the chips collide
        # with plotly's own toolbar and the last one is clipped by the frame.
        figure.add_annotation(
            x=start + (end - start) / 2,
            y=0.97,
            # "y domain", not "paper": inside a subplot, paper coordinates are
            # the whole figure, and a plain number is read as a *data* value —
            # which dragged the price axis down to include 1.
            yref="y domain",
            yanchor="top",
            text=regime,
            showarrow=False,
            font={"size": 10, "color": readable_on(fill)},
            bgcolor=fill,
            borderpad=3,
            opacity=0.95,
            row=row,
            col=1,
        )


def _price_bounds(prices: pd.DataFrame) -> tuple[float, float]:
    """The price panel's y range, with a little air above and below."""
    if prices.empty or "QQQ" not in prices:
        return (0.0, 1.0)
    series = prices["QQQ"].dropna()
    if series.empty:
        return (0.0, 1.0)
    low, high = float(series.min()), float(series.max())
    margin = (high - low) * 0.06 or 1.0
    return (low - margin, high + margin)


def _translucent(colour: str, alpha: float) -> str:
    """``#rrggbb`` as an ``rgba()`` string, so a fill can sit over gridlines."""
    red, green, blue = (int(colour[index : index + 2], 16) for index in (1, 3, 5))
    return f"rgba({red},{green},{blue},{alpha})"


def _rungs() -> list[str]:
    return [rung.label for rung in ladder(performance_report())]


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
        st.dataframe(
            regime_event_table(events).style.format(
                {"이전 점수": "{:.1f}", "새 점수": "{:.1f}"}
            ),
            hide_index=True,
            width="stretch",
        )


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
    # ``st.cache_data`` outlives a hot reload too, so for up to its TTL after a
    # deploy this frame can still be the previous release's column set. The
    # headline number is derivable without the codes; the breakdown is not.
    stored_codes = history.get("reason_codes")
    for codes in [] if stored_codes is None else stored_codes:
        for brake in locks(codes):
            counts[brake.kind] = counts.get(brake.kind, 0) + 1

    changes = int((history["regime"] != history["regime"].shift()).sum()) - 1

    columns = st.columns(4)
    columns[0].metric("점수와 단계가 어긋난 날", f"{locked / total * 100:.1f}%" if total else "—")
    columns[0].caption(f"{locked} / {total}거래일")
    columns[1].metric("최소 유지 기간에 막힌 날", counts.get("min_duration", 0))
    columns[1].caption("한 단계를 최소 기간만큼 지키느라")
    columns[2].metric("히스테리시스에 막힌 날", counts.get("hysteresis", 0))
    columns[2].caption("경계를 충분히 넘지 못해서")
    columns[3].metric("실제 단계 변경", max(changes, 0))
    columns[3].caption(
        f"평균 {total // changes}거래일에 1회" if changes > 0 else "이 기간에는 없음"
    )

    reported = {LOCK_LABELS[kind]: value for kind, value in counts.items() if value}
    if reported:
        figure = px.bar(
            x=list(reported.values()),
            y=list(reported),
            orientation="h",
            labels={"x": "일수", "y": ""},
        )
        figure.update_traces(marker_color=lock_colour())
        figure.update_layout(height=180, margin={"l": 8, "r": 8, "t": 10, "b": 30})
        st.plotly_chart(figure, width="stretch")
    elif stored_codes is None:
        st.info(
            "원인별 내역은 방금 배포된 코드가 아직 이전 응답을 캐시하고 있어 "
            "비어 있습니다. 사이드바의 **새로고침** 을 누르면 채워집니다."
        )
    else:
        st.info("이 기간에는 제동이 걸린 날이 없습니다.")

    st.caption(
        "한 날에 두 규칙이 동시에 기록될 수 있어 원인별 합은 어긋난 날 수와 다를 수 "
        "있습니다. 규칙 자체의 설명은 **전략 설명** 탭 «2·3. 점수를 단계로 나눈다» 에 "
        "있습니다."
    )

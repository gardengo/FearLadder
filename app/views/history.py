"""The record the daily worker accumulates.

Three sub-tabs, because the record answers three different questions and they
do not belong on one scroll: what happened (charts), how each stage went
(journal), and how often the score and the ladder disagreed (locks). The third
is the point of this page — on the stored history the two differ far more often
than they agree, and nothing on the old page showed it.

The charts open with the question the page used to leave unanswered: what the
signal has been worth. The score was drawn against QQQ, but the portfolio the
score produces was not, so the record could not say whether following it had
beaten simply holding the index.
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
    BENCHMARK_COLOUR,
    BENCHMARK_LABEL,
    STRATEGY_COLOUR,
    STRATEGY_LABEL,
    UNKNOWN_COLOUR,
    Rung,
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
    updated_daily,
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
    updated_daily(version)

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

    These series only mean anything against each other — the score falls, the
    ladder eventually follows, the sleeves change, the leverage steps down, and
    the result of all that either beats QQQ or does not. As separate charts
    with independent x-axes that story had to be assembled by eye. Stacked rows
    on a shared axis tell it directly: a vertical line through the figure is
    one day, everywhere.
    """
    # Coloured by the whole ladder, not by the regimes that happen to fall in
    # the window. Spread over only the ones present, a year of Neutral, Optimism
    # and Greed painted Neutral dark red — the colour of Capitulation on the
    # today tab — and the score row below, which shows every band, disagreed
    # with the row above it about what each colour meant.
    present = history["regime"].dropna().unique().tolist()
    palette = regime_palette([*_rungs(), *present])
    prices = load("price_history", ("QQQ",), days=days)
    track = load("track_record", version, days=days)
    allocations = load("allocation_history", version, days=days)
    spans = regime_spans(history)
    tracked = len(track) > 1

    _scorecard(track, history)

    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.045,
        row_heights=[0.33, 0.17, 0.21, 0.29],
        specs=[[{}], [{}], [{}], [{"secondary_y": True}]],
        subplot_titles=(
            # The two lines are named where they are drawn. The figure's one
            # legend sits under the last row, a full screen below this one.
            f"{_swatch(STRATEGY_COLOUR)} {STRATEGY_LABEL} · "
            f"{_swatch(BENCHMARK_COLOUR)} {BENCHMARK_LABEL} — 기간 첫날을 100 으로"
            if tracked
            else BENCHMARK_LABEL,
            "종합점수",
            "점수가 가리킨 칸과 실제로 선 칸",
            "목표 비중과 레버리지",
        ),
    )

    if tracked:
        _performance(figure, history, track, palette, spans)
    else:
        _price_alone(figure, history, prices, palette, spans)
    _score(figure, history, palette)
    _step_rows(figure, history)
    _sleeves(figure, history, allocations)

    figure.update_layout(
        height=1080,
        hovermode="x unified",
        # ``normal``: the stacked sleeves would otherwise reverse the order and
        # put the page's headline pair last.
        legend={"orientation": "h", "y": -0.06, "x": 0, "traceorder": "normal"},
        margin={"l": 8, "r": 8, "t": 40, "b": 8},
        # The regime bands are drawn on the "below" layer, which sits under the
        # panel fill as well as under the traces — on an opaque panel they are
        # painted and then covered up.
        plot_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(figure, width="stretch")
    st.caption(
        "네 칸 모두 같은 날짜 축입니다 — 세로로 읽으면 그날의 성과·점수·단계·보유가 "
        "한 줄로 보입니다. 맨 위 칸의 칩이 그 구간의 단계이고, 셋째 칸에서 두 선이 "
        "벌어진 구간이 **잠금 구간**입니다. 맨 아래 칸의 계단선이 그 결과로 실제 든 "
        "레버리지입니다."
    )
    if tracked:
        st.caption(
            "**전략 선은 모의 계좌입니다.** 매일 저장된 목표 비중을 다음 거래일 시가에 "
            "체결하고, 단계가 바뀔 때만 매매하며, 거래마다 비용을 뺐습니다 — 백테스트와 "
            "같은 계산입니다. QQQ 는 같은 날 같은 방식으로 사서 그대로 든 것이고, 배당은 "
            "양쪽 모두 넣지 않았습니다. 일일 배치가 하루씩 이어 붙이며, 한 번 적힌 날은 "
            "다시 계산하지 않습니다."
        )


# ------------------------------------------------------------- performance


def _frozen_on() -> pd.Timestamp | None:
    """The day the strategy was frozen: before it, replay; after it, live."""
    stamp = str((performance_report() or {}).get("frozen_at") or "")[:10]
    try:
        return pd.Timestamp(stamp) if stamp else None
    except ValueError:
        return None


def _drawdown(series: pd.Series) -> float:
    return float((series / series.cummax() - 1.0).min())


def _scorecard(track: pd.DataFrame, history: pd.DataFrame) -> None:
    """The four numbers the chart below is about, for the chosen window."""
    if len(track) < 2:
        st.info(
            "전략을 그대로 따랐을 때의 성과 기록이 아직 없습니다. 일일 배치가 한 번 "
            "돌면, 이미 저장돼 있는 목표 비중과 가격으로 첫날부터 채워집니다."
        )
        return

    followed, held = track["strategy_nav"], track["benchmark_nav"]
    strategy = float(followed.iloc[-1] / followed.iloc[0] - 1.0)
    benchmark = float(held.iloc[-1] / held.iloc[0] - 1.0)

    columns = st.columns(4)
    columns[0].metric(
        STRATEGY_LABEL, f"{strategy * 100:+.1f}%",
        help="저장된 목표 비중을 그대로 따른 모의 계좌의 이 기간 수익률.",
    )
    columns[1].metric(
        f"{BENCHMARK_LABEL} 보유", f"{benchmark * 100:+.1f}%",
        help="같은 기간 QQQ 를 사서 그대로 들고 있었을 때.",
    )
    columns[2].metric(
        f"{STRATEGY_LABEL} − {BENCHMARK_LABEL}", f"{(strategy - benchmark) * 100:+.1f}%p"
    )
    columns[3].metric(
        f"최대낙폭 ({STRATEGY_LABEL} / {BENCHMARK_LABEL})",
        f"{_drawdown(followed) * 100:.1f}% / {_drawdown(held) * 100:.1f}%",
        help="이 기간 안에서 고점 대비 가장 깊이 내려간 폭.",
    )

    notes = [
        f"{track.index[0]:%Y-%m-%d} 종가 → {track.index[-1]:%Y-%m-%d} 종가 · "
        f"{len(track) - 1}거래일"
    ]
    frozen = _frozen_on()
    if frozen is not None:
        before = track[track.index <= frozen]
        after = track[track.index > frozen]
        if before.empty:
            notes.append(f"전부 전략 고정({frozen:%Y-%m-%d}) 이후의 **실시간 기록**입니다")
        elif not after.empty:
            base = before.iloc[-1]
            live_strategy = float(after["strategy_nav"].iloc[-1] / base["strategy_nav"] - 1.0)
            live_benchmark = float(after["benchmark_nav"].iloc[-1] / base["benchmark_nav"] - 1.0)
            notes.append(
                f"이 중 전략 고정({frozen:%Y-%m-%d}) 이후 **실시간 {len(after)}거래일**: "
                f"{STRATEGY_LABEL} {live_strategy * 100:+.1f}% · "
                f"{BENCHMARK_LABEL} {live_benchmark * 100:+.1f}%. "
                "그 이전은 고정된 규칙을 과거 날짜에 재생한 기록입니다"
            )
    st.caption(" · ".join(notes) + ".")

    if track.index[-1] < history.index[-1]:
        st.warning(
            f"성과 기록이 {track.index[-1]:%Y-%m-%d} 까지입니다 — 신호 기록"
            f"({history.index[-1]:%Y-%m-%d})보다 뒤처져 있습니다. 신호에는 영향이 없고, "
            "다음 배치가 마지막으로 적힌 날부터 이어 붙입니다."
        )


def _performance(
    figure: go.Figure,
    history: pd.DataFrame,
    track: pd.DataFrame,
    palette: dict,
    spans: list,
) -> None:
    """The strategy against QQQ, both starting the window at 100.

    Indexed rather than drawn on two axes: the only question here is which line
    is higher, and two y-scales would let the answer depend on how each was
    stretched.
    """
    values = track[["strategy_nav", "benchmark_nav"]]
    indexed = values / values.iloc[0] * 100.0
    # The benchmark first, so the strategy is drawn over it where they cross.
    for column, name, colour, width in (
        ("benchmark_nav", BENCHMARK_LABEL, BENCHMARK_COLOUR, 1.8),
        ("strategy_nav", STRATEGY_LABEL, STRATEGY_COLOUR, 2.4),
    ):
        figure.add_trace(
            go.Scatter(
                x=indexed.index,
                y=indexed[column],
                name=name,
                line={"color": colour, "width": width},
                hovertemplate="%{y:.1f}<extra>" + name + "</extra>",
            ),
            row=1,
            col=1,
        )

    low, high = float(indexed.min().min()), float(indexed.max().max())
    margin = (high - low) * 0.08 or 1.0
    bounds = (low - margin, high + margin)
    # After the traces, never before: a shape resolves ``row``/``col`` against
    # the axes the subplot already has, and on an empty panel it matches
    # nothing and is silently dropped.
    _shade(figure, spans, palette, bounds, row=1)
    if spans:
        _name_the_bands(
            figure, spans, palette, total=history.index[-1] - history.index[0], row=1
        )
    _mark_the_freeze(figure, indexed.index)
    # Pinned, so the bands drawn in these coordinates fill the panel exactly.
    figure.update_yaxes(title_text="첫날 = 100", range=list(bounds), row=1, col=1)


def _mark_the_freeze(figure: go.Figure, days: pd.Index) -> None:
    """Draw the line between the replayed record and the live one."""
    frozen = _frozen_on()
    if frozen is None or not (days[0] < frozen < days[-1]):
        return
    figure.add_shape(
        type="line", x0=frozen, x1=frozen, y0=0, y1=1, yref="y domain",
        line={"color": BENCHMARK_COLOUR, "width": 1.2, "dash": "dot"}, row=1, col=1,
    )
    figure.add_annotation(
        x=frozen, y=0.03, yref="y domain", yanchor="bottom", xanchor="left", xshift=5,
        text="전략 고정 · 여기부터 실시간", showarrow=False, font={"size": 10},
        row=1, col=1,
    )


def _price_alone(
    figure: go.Figure,
    history: pd.DataFrame,
    prices: pd.DataFrame,
    palette: dict,
    spans: list,
) -> None:
    """QQQ on its own, for a database the track record has not reached yet."""
    if not prices.empty:
        figure.add_trace(
            go.Scatter(
                x=prices.index, y=prices["QQQ"], name=BENCHMARK_LABEL,
                line={"color": BENCHMARK_COLOUR, "width": 1.8},
            ),
            row=1,
            col=1,
        )
    bounds = _price_bounds(prices)
    _shade(figure, spans, palette, bounds, row=1)
    if spans:
        _name_the_bands(
            figure, spans, palette, total=history.index[-1] - history.index[0], row=1
        )
    figure.update_yaxes(title_text="QQQ", range=list(bounds), row=1, col=1)


def _score(figure: go.Figure, history: pd.DataFrame, palette: dict) -> None:
    """The composite score on its own 0-100 axis, over the bands it is cut into.

    It used to share the price panel on a second y-axis, where how far apart
    the two lines sat was an accident of two unrelated scales. Here the bands
    are the regime boundaries themselves, so the row reads without a legend:
    the line is in the band it is in.
    """
    for index, layer in enumerate(halo()):
        figure.add_trace(
            go.Scatter(
                x=history.index,
                y=history["composite_score"],
                name="종합점수",
                line={**layer, "width": 4 if index == 0 else 1.6},
                showlegend=False,
                hoverinfo="skip" if index == 0 else None,
                hovertemplate=None if index == 0 else "%{y:.1f}<extra>종합점수</extra>",
            ),
            row=2,
            col=1,
        )
    alpha = band_opacity()
    for rung in _ladder_rungs():
        figure.add_shape(
            type="rect", x0=history.index[0], x1=history.index[-1],
            y0=rung.low, y1=rung.high,
            fillcolor=palette.get(rung.label, UNKNOWN_COLOUR),
            opacity=alpha, line_width=0, layer="below", row=2, col=1,
        )
    figure.update_yaxes(
        title_text="점수", range=[0, 100], tickvals=[0, 50, 100], showgrid=False, row=2, col=1
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
        row=3,
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
        row=3,
        col=1,
    )
    figure.update_yaxes(
        tickmode="array",
        tickvals=list(range(len(order))),
        ticktext=order,
        range=[-0.5, len(order) - 0.5],
        row=3,
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
                row=4,
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
            row=4,
            col=1,
            secondary_y=True,
        )
    figure.update_yaxes(
        title_text="비중", range=[0, 1], tickformat=".0%", row=4, col=1, secondary_y=False
    )
    figure.update_yaxes(
        title_text="레버리지 (x)", row=4, col=1, secondary_y=True, showgrid=False
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


def _swatch(colour: str) -> str:
    """A short stroke in a series' colour, for naming a line in a title."""
    return f"<span style='color:{colour}'>━</span>"


def _translucent(colour: str, alpha: float) -> str:
    """``#rrggbb`` as an ``rgba()`` string, so a fill can sit over gridlines."""
    red, green, blue = (int(colour[index : index + 2], 16) for index in (1, 3, 5))
    return f"rgba({red},{green},{blue},{alpha})"


def _ladder_rungs() -> list[Rung]:
    return ladder(performance_report())


def _rungs() -> list[str]:
    return [rung.label for rung in _ladder_rungs()]


def _journal(version: str, history: pd.DataFrame) -> None:
    st.subheader("단계별 일지")
    spans = regime_spans(history)
    if not spans:
        st.info("기록된 단계가 없습니다.")
        return

    span_days = (history.index[-1] - history.index[0]).days + 30
    prices = load("price_history", ("QQQ",), days=span_days)
    track = load("track_record", version, days=span_days)
    held = prices["QQQ"] if "QQQ" in prices else pd.Series(dtype=float)
    followed = track["strategy_nav"] if "strategy_nav" in track else pd.Series(dtype=float)
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
                "구간 전략": _return(followed, start, end),
                "구간 QQQ": _return(held, start, end),
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
                "구간 전략": _percent_or_dash,
                "구간 QQQ": _percent_or_dash,
            }
        ),
        hide_index=True,
        width="stretch",
        height=min(560, 38 * (len(frame) + 1)),
    )
    st.caption(
        "맨 윗줄이 진행 중인 단계입니다. «구간 전략» 은 그 단계에 머문 동안 모의 계좌가 "
        "낸 수익률이고 «구간 QQQ» 는 같은 날들의 QQQ 종가 수익률입니다 — 둘 다 구간 "
        "첫날 종가에서 마지막 날 종가까지입니다. 1996년부터의 장기 성적은 **백테스트** "
        "탭에 있습니다."
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


def _return(series: pd.Series, start: object, end: object) -> float | None:
    """First close to last close inside one span, or ``None`` if it has no two."""
    if series.empty:
        return None
    window = series.loc[start:end].dropna()  # type: ignore[misc]
    if len(window) < 2:
        return None
    return float(window.iloc[-1] / window.iloc[0] - 1.0)


def _percent_or_dash(value: object) -> str:
    return "—" if pd.isna(value) else f"{float(value) * 100:+.1f}%"  # type: ignore[arg-type]


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

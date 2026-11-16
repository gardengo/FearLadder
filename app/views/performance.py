"""What the frozen rules did over thirty years, against the alternatives.

Every number here comes from ``reports/performance.json``, written once by
``scripts/make_performance_report.py`` against the frozen profile. The page
computes nothing, and nothing on it moves from one day to the next: it is the
evidence the strategy was frozen on, not a running score. What has happened
since is the track record on the history tab — hence the tab's name, which
used to be plain "성과" and read as if it were the live result.

The comparison is deliberately unkind to the strategy: the benchmarks are
buy-and-hold mixes of one ETF and cash, which anyone can run without a monitor,
a score or a filter. A strategy that cannot beat that grid is not worth its
complexity.
"""

from __future__ import annotations

import math

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from views.common import (
    BENCHMARK_COLOUR,
    BENCHMARK_LABEL,
    STRATEGY_COLOUR,
    STRATEGY_LABEL,
    equity_curve,
    fixed_at_freeze,
    missing_report_notice,
    performance_report,
)

#: The benchmarks worth showing by default. The report holds every 10% step;
#: these are the ones that make the shape of the answer visible.
HIGHLIGHT = (
    STRATEGY_LABEL,
    "QQQ",
    "QQQ 60%/현금 40%",
    "QLD",
    "QLD 60%/현금 40%",
    "TQQQ",
    "TQQQ 60%/현금 40%",
    "현금",
)


def render() -> None:
    st.header("백테스트 성과")
    report = performance_report()
    if report is None:
        missing_report_notice()
        return
    fixed_at_freeze(report)

    window = report["window"]
    st.info(
        f"**{window['start']} … {window['end']} 을 한 번 계산한 결과입니다.** "
        "전략을 고정하기 전까지의 데이터이고, 그 뒤의 날은 들어 있지 않습니다. "
        "고정 이후 실제로 쌓이고 있는 성과는 **기록** 탭 맨 위에 QQQ 와 나란히 있습니다."
    )
    st.caption(
        f"`{report['strategy_version']}` · {window['start']} … {window['end']} · "
        "벤치마크는 분기 리밸런싱, Sharpe 는 현금 대비 초과수익 기준. "
        "TQQQ 는 2010년, QLD 는 2006년 이전이 재구성 데이터입니다."
    )

    overall = report["overall"]
    strategy = overall[STRATEGY_LABEL]

    # Each headline number with the same number for simply holding QQQ under
    # it. On their own they had no scale: 18.88% a year is impressive or
    # ordinary depending entirely on what the index did over the same years.
    held = overall.get(BENCHMARK_LABEL)

    def beside(text: str) -> dict[str, object]:
        if held is None:
            return {}
        return {
            "delta": f"{BENCHMARK_LABEL} 보유 {text}",
            "delta_color": "off",
            "delta_arrow": "off",
        }

    columns = st.columns(4)
    columns[0].metric(
        "연 수익률 (CAGR)",
        f"{strategy['cagr'] * 100:.2f}%",
        **beside(f"{held['cagr'] * 100:.2f}%" if held else ""),
    )
    columns[1].metric(
        "최대낙폭", f"{strategy['max_drawdown'] * 100:.1f}%",
        help="고점 대비 가장 깊이 내려간 폭. 실제로 견뎌야 하는 숫자입니다.",
        **beside(f"{held['max_drawdown'] * 100:.1f}%" if held else ""),
    )
    columns[2].metric(
        "Sharpe", f"{strategy['sharpe']:.2f}", help="현금 대비 초과수익 / 변동성",
        **beside(f"{held['sharpe']:.2f}" if held else ""),
    )
    columns[3].metric(
        "최종 배수",
        f"{strategy['multiple']:.0f}x",
        **beside(f"{held['multiple']:.0f}x" if held else ""),
    )

    _curves(report)

    st.divider()
    _risk_matched(report)
    st.divider()
    _rolling(report)
    st.divider()
    _episodes(report)
    st.divider()
    _full_grid(overall)


def _curves(report: dict) -> None:
    """The path behind the four numbers above: growth, and the falls on the way.

    Two rows on one time axis rather than one chart with two scales. The upper
    row is logarithmic — over thirty years a linear axis shows the last five
    and flattens everything before 2015 into the floor, including the two
    crashes this strategy was calibrated on.
    """
    curve = equity_curve()
    if curve is None or curve.get("strategy_version") != report.get("strategy_version"):
        return
    dates = pd.to_datetime(curve["dates"])
    series = (
        (BENCHMARK_LABEL, BENCHMARK_COLOUR, 1.6),
        (STRATEGY_LABEL, STRATEGY_COLOUR, 2.2),
    )

    # The deepest point of each line is named in the row's title rather than
    # beside the trough: both bottomed in the same month, and two labels there
    # sat on top of each other and of the lines.
    troughs = []
    for name, colour, _ in reversed(series):
        falls = curve["drawdown"].get(name)
        if falls:
            worst = min(range(len(falls)), key=falls.__getitem__)
            troughs.append(
                f"<span style='color:{colour}'>●</span> {name} {falls[worst] * 100:.1f}% "
                f"({dates[worst]:%Y-%m})"
            )

    figure = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08, row_heights=[0.62, 0.38],
        subplot_titles=(
            "1 을 넣었다면 (로그 축)",
            "고점 대비 낙폭 — 가장 깊었던 곳: " + " · ".join(troughs),
        ),
    )
    for name, colour, width in series:
        equity = curve["equity"].get(name)
        falls = curve["drawdown"].get(name)
        if not equity or not falls:
            continue
        figure.add_trace(
            go.Scatter(
                x=dates, y=equity, name=name, line={"color": colour, "width": width},
                hovertemplate="%{y:.1f}x<extra>" + name + "</extra>",
            ),
            row=1, col=1,
        )
        # Named at the end of the line it belongs to, with where it ended up.
        figure.add_annotation(
            # On a log axis an annotation's y is the exponent, not the value:
            # given 202 it is placed at 10^202 and the axis stretches to reach it.
            x=dates[-1], y=math.log10(equity[-1]), yref="y", xanchor="left", xshift=6,
            text=f"{name} {equity[-1]:.0f}x", showarrow=False,
            font={"size": 12}, row=1, col=1,
        )
        figure.add_trace(
            go.Scatter(
                x=dates, y=falls, name=name, showlegend=False,
                line={"color": colour, "width": width},
                hovertemplate="%{y:.1%}<extra>" + name + "</extra>",
            ),
            row=2, col=1,
        )
        worst = min(range(len(falls)), key=falls.__getitem__)
        figure.add_trace(
            go.Scatter(
                x=[dates[worst]], y=[falls[worst]], mode="markers", showlegend=False,
                marker={"size": 9, "color": colour, "line": {"color": "#f5f5f5", "width": 2}},
                hoverinfo="skip",
            ),
            row=2, col=1,
        )

    figure.update_yaxes(type="log", tickvals=[1, 3, 10, 30, 100, 300], ticksuffix="x",
                        row=1, col=1)
    figure.update_yaxes(tickformat=".0%", range=[-1.0, 0.02], row=2, col=1)
    figure.update_layout(
        height=560, hovermode="x unified", showlegend=False,
        # Room on the right for the two end-of-line labels.
        margin={"l": 8, "r": 84, "t": 40, "b": 8},
        plot_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(figure, width="stretch")
    st.caption(
        "주 단위로 그렸습니다 — 위 칸은 그 주의 마지막 값, 아래 칸은 그 주의 **가장 깊은** "
        "낙폭이라 바닥의 깊이는 일별 계산과 같습니다. 낙폭은 각자의 직전 고점에서 잰 "
        "것이므로, 아래 칸에서 전략 선이 QQQ 보다 위에 있는 구간이 덜 빠진 구간이고 "
        "아래에 있는 구간이 레버리지의 대가를 치른 구간입니다."
    )


def _risk_matched(report: dict) -> None:
    st.subheader("같은 위험을 질 때 무엇이 더 버는가")
    st.caption(
        "수익률만 비교하면 아무것도 결정할 수 없습니다. 더 깊은 구덩이를 파고 얻은 "
        "수익은 개선이 아니기 때문입니다. 아래는 전략과 **최대낙폭이 가장 비슷한** "
        "정적 조합들입니다."
    )
    strategy = report["overall"][STRATEGY_LABEL]
    rows = [
        {
            "구성": STRATEGY_LABEL,
            "CAGR": strategy["cagr"],
            "최대낙폭": strategy["max_drawdown"],
            "최종 배수": strategy["multiple"],
            "전략 대비": 0.0,
        }
    ]
    rows += [
        {
            "구성": item["name"],
            "CAGR": item["cagr"],
            "최대낙폭": item["max_drawdown"],
            "최종 배수": item["multiple"],
            "전략 대비": item["cagr_gap"],
        }
        for item in report["risk_matched"]
    ]
    st.dataframe(
        pd.DataFrame(rows).style.format(
            {
                "CAGR": "{:.2%}",
                "최대낙폭": "{:.1%}",
                "최종 배수": "{:.1f}x",
                "전략 대비": "{:+.2%}",
            }
        ),
        hide_index=True,
        width="stretch",
    )


def _rolling(report: dict) -> None:
    st.subheader("보유 기간별 성적 분포")
    st.markdown(
        "전체 기간의 단일 숫자는 **언제 시작했는지**를 감춥니다. 아래는 한 달 간격의 "
        "모든 시작 시점에 대해 3·5·10·20년을 보유했을 때의 결과입니다. "
        "레버리지 전략에서 봐야 할 것은 중앙값이 아니라 **가장 나빴던 경우**입니다."
    )

    rolling = report["rolling"]
    available = sorted(
        {row["years"] for rows in rolling.values() for row in rows}, reverse=True
    )
    years = st.radio(
        "보유 기간", available, horizontal=True,
        format_func=lambda value: f"{value}년",
        index=available.index(10) if 10 in available else 0,
    )

    rows = []
    for name, summaries in rolling.items():
        summary = next((item for item in summaries if item["years"] == years), None)
        if summary is None:
            continue
        rows.append(
            {
                "구성": name,
                "창 개수": summary["windows"],
                "CAGR 중앙": summary["cagr_median"],
                "CAGR 최악": summary["cagr_worst"],
                "하위 10%": summary["cagr_p10"],
                "낙폭 중앙": summary["mdd_median"],
                "손실로 끝난 창": summary["loss_rate"],
                "전략을 이긴 비율": summary["beats_reference"],
            }
        )
    frame = pd.DataFrame(rows)

    only_highlight = st.checkbox("주요 벤치마크만 보기", value=True)
    if only_highlight:
        frame = frame[frame["구성"].isin(HIGHLIGHT)]
    # The strategy is the reference every other row is scored against, so it
    # stays on top no matter how it ranks; the benchmarks sort by the typical
    # outcome first and the worst one as the tie-break.
    frame = frame.assign(_reference=frame["구성"].eq(STRATEGY_LABEL)).sort_values(
        ["_reference", "CAGR 중앙", "CAGR 최악"], ascending=[False, False, False]
    ).drop(columns="_reference")

    st.dataframe(
        frame.style.format(
            {
                "CAGR 중앙": "{:.1%}",
                "CAGR 최악": "{:.1%}",
                "하위 10%": "{:.1%}",
                "낙폭 중앙": "{:.1%}",
                "손실로 끝난 창": "{:.0%}",
                "전략을 이긴 비율": lambda value: "—" if pd.isna(value) else f"{value:.0%}",
            }
        ),
        hide_index=True,
        width="stretch",
        height=min(560, 38 * (len(frame) + 1)),
    )

    plot = frame[frame["구성"] != STRATEGY_LABEL].copy()
    if not plot.empty:
        figure = px.scatter(
            frame,
            x="CAGR 최악",
            y="CAGR 중앙",
            text="구성",
            color=frame["구성"].eq(STRATEGY_LABEL).map({True: "전략", False: "벤치마크"}),
            color_discrete_map={"전략": "#b2182b", "벤치마크": "#8fa8bd"},
            labels={"color": ""},
        )
        figure.update_traces(textposition="top center", marker={"size": 11})
        figure.update_layout(height=460, legend={"orientation": "h", "y": 1.1})
        st.plotly_chart(figure, width="stretch")
        st.caption(
            "오른쪽으로 갈수록 최악의 경우가 낫고, 위로 갈수록 보통의 경우가 좋습니다. "
            "오른쪽 위가 유리합니다."
        )


def _episodes(report: dict) -> None:
    st.subheader("시장 국면별로 나눠 보면")
    st.markdown(
        "이 전략의 강점과 약점은 **하락의 모양**으로 갈립니다. "
        "빠른 폭락에서는 크게 이기고, 천천히 갈리는 하락에서는 그냥 QQQ 를 "
        "들고 있는 것보다 못합니다. 숨길 수 없는 성질이라 그대로 보여줍니다."
    )
    episodes = report["episodes"]
    if not episodes:
        st.info("국면 데이터가 없습니다.")
        return

    rows = []
    for episode in episodes:
        row = {
            "국면": episode["name"],
            "성격": episode["shape"],
            "기간": f"{episode['start']} … {episode['end']}",
        }
        for name in (STRATEGY_LABEL, "QQQ", "QLD", "TQQQ"):
            if name in episode["returns"]:
                row[name] = episode["returns"][name]
        rows.append(row)

    frame = pd.DataFrame(rows)
    value_columns = [c for c in frame.columns if c not in {"국면", "성격", "기간"}]
    st.dataframe(
        frame.style.format(dict.fromkeys(value_columns, "{:+.1%}")),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "느린 폭락(2008)과 느린 하락(2022)에서 전략이 QQQ 에 진다는 점을 확인하세요. "
        "이는 추세 필터의 깊이 조건이 만든 알려진 대가입니다 (docs/strategy.md §2.7)."
    )


def _full_grid(overall: dict) -> None:
    with st.expander("전체 벤치마크 격자 (10% 단위)"):
        rows = [
            {
                "구성": name,
                "CAGR": stats["cagr"],
                "최대낙폭": stats["max_drawdown"],
                "Sharpe": stats["sharpe"],
                "Calmar": stats["calmar"],
                "최종 배수": stats["multiple"],
            }
            for name, stats in overall.items()
        ]
        frame = pd.DataFrame(rows).sort_values("CAGR", ascending=False)
        st.dataframe(
            frame.style.format(
                {
                    "CAGR": "{:.2%}",
                    "최대낙폭": "{:.1%}",
                    "Sharpe": "{:.2f}",
                    "Calmar": "{:.3f}",
                    "최종 배수": "{:.1f}x",
                }
            ),
            hide_index=True,
            width="stretch",
            height=560,
        )

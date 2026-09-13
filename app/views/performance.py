"""What the strategy has actually done, against the alternatives to it.

Every number here comes from ``reports/performance.json``, written once by
``scripts/make_performance_report.py`` against the frozen profile. The page
computes nothing.

The comparison is deliberately unkind to the strategy: the benchmarks are
buy-and-hold mixes of one ETF and cash, which anyone can run without a monitor,
a score or a filter. A strategy that cannot beat that grid is not worth its
complexity.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from views.common import (
    STRATEGY_LABEL,
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
    st.header("성과")
    report = performance_report()
    if report is None:
        missing_report_notice()
        return

    window = report["window"]
    st.caption(
        f"`{report['strategy_version']}` · {window['start']} … {window['end']} · "
        "벤치마크는 분기 리밸런싱, Sharpe 는 현금 대비 초과수익 기준. "
        "TQQQ 는 2010년, QLD 는 2006년 이전이 재구성 데이터입니다."
    )

    overall = report["overall"]
    strategy = overall[STRATEGY_LABEL]

    columns = st.columns(4)
    columns[0].metric("연 수익률 (CAGR)", f"{strategy['cagr'] * 100:.2f}%")
    columns[1].metric(
        "최대낙폭", f"{strategy['max_drawdown'] * 100:.1f}%",
        help="고점 대비 가장 깊이 내려간 폭. 실제로 견뎌야 하는 숫자입니다.",
    )
    columns[2].metric("Sharpe", f"{strategy['sharpe']:.2f}", help="현금 대비 초과수익 / 변동성")
    columns[3].metric("최종 배수", f"{strategy['multiple']:.0f}x")

    st.divider()
    _risk_matched(report)
    st.divider()
    _rolling(report)
    st.divider()
    _episodes(report)
    st.divider()
    _full_grid(overall)


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
    frame = frame.sort_values("CAGR 최악", ascending=False)

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

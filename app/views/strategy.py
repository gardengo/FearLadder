"""How the strategy works, for someone who has never seen it before.

This page is the explanation, not the evidence. It walks the four steps a
signal passes through, names every indicator that feeds them, and states the
one rule that does most of the work — and what that rule costs.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from views.common import missing_report_notice, percent, performance_report

#: Plain-language description of each indicator family, keyed by the prefix the
#: indicator names share. The dashboard cannot read indicators.yaml's prose
#: (that file is loaded by the engines, not here), so the explanation lives with
#: the explanation.
FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("rsi", "RSI", "최근 상승폭과 하락폭의 비율. 낮으면 과매도(공포), 높으면 과매수(탐욕)."),
    ("price_vs", "이동평균 대비 위치", "현재가가 20·50·200일 평균선보다 위인지 아래인지."),
    ("ma_", "이동평균의 모양", "50일선과 200일선의 간격, 200일선이 오르는지 내리는지."),
    ("momentum", "모멘텀", "1·3·6·12개월 수익률. 최근에 올랐는가."),
    ("drawdown", "낙폭", "최근 고점 대비 몇 % 내려와 있는가."),
    ("vix", "변동성(VIX)", "시장이 예상하는 향후 30일 변동성. 공포 지수라 불린다."),
    ("cnn", "CNN 공포·탐욕 지수", "여러 시장 지표를 묶어 만든 심리 지수."),
    ("aaii", "AAII 개인투자자 설문", "개인투자자 강세·약세 응답 차이. 주간 발표."),
    ("breadth", "시장 폭(breadth)", "구성종목 중 몇 %가 200일선 위인가."),
)


def _family_of(name: str) -> tuple[str, str]:
    for prefix, label, description in FAMILIES:
        if name.startswith(prefix):
            return label, description
    return name, ""


def render() -> None:
    st.header("이 전략은 어떻게 동작하는가")
    report = performance_report()
    if report is None:
        missing_report_notice()
        return

    parameters = report["parameters"]
    st.caption(
        f"아래 숫자는 전부 고정된 `{report['strategy_version']}` 의 실제 설정값입니다. "
        f"고정 시각 {report.get('frozen_at') or '—'}."
    )

    st.markdown(
        """
### 한 문장으로

**시장이 공포에 질려 있을수록 레버리지를 늘리고, 탐욕에 차 있을수록 줄인다.
단, 장기 추세가 무너진 동안에는 그 규칙을 정지시킨다.**

앞부분만 있으면 하락장에서 파산합니다. 뒷부분이 이 전략의 핵심입니다.
"""
    )

    st.divider()
    st.subheader("매일 네 단계를 거칩니다")

    steps = st.columns(4)
    steps[0].markdown(
        "#### 1. 지표 수집\n"
        f"{len(parameters['indicator_weights'])}개 지표를 계산합니다. "
        "가격·추세·변동성·투자심리를 함께 봅니다."
    )
    steps[1].markdown(
        "#### 2. 종합점수\n"
        "각 지표를 0~100으로 환산해 가중평균합니다.\n\n"
        "**0 = 극단적 공포, 100 = 극단적 탐욕**"
    )
    steps[2].markdown(
        "#### 3. 단계 판정\n"
        f"점수를 {len(parameters['regime_labels'])}단계로 나눕니다. "
        "자주 흔들리지 않도록 확인·유지 규칙이 붙습니다."
    )
    steps[3].markdown(
        "#### 4. 목표 비중\n"
        "단계마다 정해진 레버리지가 있고, 추세 필터가 그 위에 상한을 겁니다."
    )

    st.divider()
    st.subheader("1. 어떤 지표를 보는가")

    weights = parameters["indicator_weights"]
    rows = []
    for name, weight in weights.items():
        label, description = _family_of(name)
        rows.append(
            {
                "지표": name,
                "분류": label,
                "가중치": weight,
                "무엇을 재는가": description,
            }
        )
    table = pd.DataFrame(rows)
    st.dataframe(
        table.style.format({"가중치": "{:.1%}"}),
        hide_index=True,
        width="stretch",
        height=min(560, 38 * (len(table) + 1)),
    )
    st.caption(
        "가중치는 탐색으로 정해졌습니다. 추세 계열이 가장 무겁고 공포 계열이 가장 "
        "가벼운데, 이는 아래 추세 필터의 결과와 같은 방향입니다."
    )

    st.divider()
    st.subheader("2·3. 점수를 단계로 나눈다")

    labels = parameters["regime_labels"]
    boundaries = parameters["regime_boundaries"]
    ladder = parameters["ladder"]
    edges = [0.0, *boundaries, 100.0]
    stage_rows = [
        {
            "단계": label,
            "점수 구간": f"{edges[index]:.0f} – {edges[index + 1]:.0f}",
            "사다리 레버리지": ladder.get(label),
            "뜻": _mood(index, len(labels)),
        }
        for index, label in enumerate(labels)
    ]
    st.dataframe(
        pd.DataFrame(stage_rows).style.format({"사다리 레버리지": "{:.2f}x"}),
        hide_index=True,
        width="stretch",
    )

    transition = parameters["transition"]
    st.markdown(
        f"""
단계가 하루 만에 뒤집히지 않도록 세 가지 장치가 있습니다.

- **확인 {transition['confirmation_days']}일** — 새 단계가 이 기간 유지돼야 인정합니다.
- **히스테리시스 {transition['hysteresis']:g}점** — 경계선을 스치기만 해서는 바뀌지 않습니다.
- **최소 유지 {transition['minimum_duration_days']}일** — 한 번 들어간 단계는
  최소 이만큼 유지합니다.

마지막 항목이 가장 큽니다. 한 번 자리를 잡으면 약 3개월은 그 단계로 지냅니다.
거래를 줄이려는 장치이며, 그만큼 반응이 느려지는 것을 감수한 것입니다.
"""
    )

    st.divider()
    st.subheader("4. 추세 필터 — 실제로 일을 하는 장치")

    trend = parameters["trend_filter"]
    st.markdown(
        f"""
사다리만 쓰면 하락장에서 파산합니다. 공포 지표는 **바닥과 하락 중간을 구별하지
못하기** 때문입니다. 둘 다 똑같이 무섭게 보입니다. 실제로 측정하면 필터 없이는
연 −2.7%에 최대낙폭 98%가 나옵니다.

그래서 조건을 하나 더 겁니다.

> **`{trend['indicator']}` 가 {percent(trend['threshold'], 0)} 아래이고,
> 동시에 52주 고점 대비 이미 {percent(trend['min_depth_to_engage'], 0)} 이상
> 빠져 있으면 — 레버리지를 {trend['max_leverage_below']:.2f}x 로 제한한다.**

세 부분이 각각 이유가 있습니다.
"""
    )
    reasons = st.columns(3)
    reasons[0].markdown(
        f"**200일선 {percent(trend['threshold'], 0)} 아래**\n\n"
        "장기 추세가 꺾였다는 신호입니다. 이 조건 하나가 전략 성과의 대부분을 "
        "만듭니다."
    )
    reasons[1].markdown(
        f"**재진입은 {percent(trend['reentry_threshold'], 0)} 위에서**\n\n"
        "내려갈 때와 올라올 때의 선을 다르게 둡니다. 같은 선이면 경계에서 "
        "오르내리며 수수료만 나갑니다."
    )
    reasons[2].markdown(
        f"**이미 {percent(trend['min_depth_to_engage'], 0)} 빠진 뒤에만**\n\n"
        "−10%짜리 흔들림은 위기가 아닙니다. 이 조건이 없으면 평범한 조정마다 "
        "레버리지를 내렸다가 뒤늦게 복귀합니다."
    )

    st.warning(
        f"**대가가 있습니다.** 마지막 조건 때문에 하락의 **첫 "
        f"{percent(trend['min_depth_to_engage'], 0)} 구간을 레버리지를 든 채 통과**합니다. "
        "천천히 갈리는 하락(2008년, 2022년)에서는 이것이 손해입니다. "
        "빠른 폭락(2000년, 2020년)에서는 이득입니다. 성과 탭에서 두 경우를 "
        "나눠서 볼 수 있습니다."
    )

    st.divider()
    st.subheader("실제로 주문은 어떻게 나가는가")

    execution = parameters["execution"]
    st.markdown(
        f"""
- **{execution['timing']}** — 오늘 종가로 판단하고 **다음 거래일**에 실행합니다.
  같은 날 종가에 사는 것은 불가능하므로 그렇게 계산하지 않습니다.
- **{execution['rebalance_on']}** — 단계가 바뀔 때만 갈아탑니다. 매일 맞추지 않습니다.
- **거래비용 {execution['commission_bps']:.0f}bp** 를 매 거래에 부과한 결과입니다.
- 자동매매를 하지 않습니다. 시스템은 목표 비중을 알려줄 뿐이고,
  주문은 사람이 직접 냅니다.
"""
    )

    st.divider()
    st.subheader("이 숫자들은 어떻게 정해졌는가")

    splits = parameters["splits"]
    st.markdown(
        f"""
데이터를 세 구간으로 나누고, 순서대로만 사용했습니다.

| 구간 | 기간 | 용도 |
| --- | --- | --- |
| 탐색 | {splits['research'][0]} … {splits['research'][1]} | 파라미터를 고르는 데 사용 |
| 검증 | {splits['validation'][0]} … {splits['validation'][1]} | 고른 값이 통하는지 확인 |
| 최종 | {splits['oos'][0]} … {splits['oos'][1]} | **고정한 뒤 단 한 번** 열람 |

최종 구간은 전략을 고정한 **다음에** 열었습니다. 순서가 중요합니다 — 결과를
보고 나서 숫자를 고칠 수 있으면 그 구간은 시험이 아니게 됩니다.

자세한 경위는 `docs/strategy.md` 에 있습니다. 실패한 시도와 기각한 이유까지
남겨두었습니다.
"""
    )


def _mood(index: int, total: int) -> str:
    if index == 0:
        return "극단적 공포 — 가장 공격적"
    if index == total - 1:
        return "극단적 탐욕 — 가장 방어적"
    if index < total / 2:
        return "공포 쪽"
    if index > total / 2:
        return "탐욕 쪽"
    return "중립"

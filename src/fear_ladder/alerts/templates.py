"""Alert message templates (TASK-121).

``CLAUDE_CODE_INITIAL_PROMPT.md`` §12 is explicit that an alert saying only
``Regime = Fear`` is not acceptable. Every message therefore carries the regime,
the score and its change, the target leverage, the target allocation, the loudest
indicators and the reason codes — enough for the user to judge without opening
the dashboard.

Every template also ends with the same reminder: this system does not trade
(``PRD.md`` §1.2). The user decides.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass

from fear_ladder.constants import Asset, EventType
from fear_ladder.data.models import MarketState, TargetAllocation

DISCLAIMER = "이 시스템은 자동매매를 하지 않습니다. 매매 여부는 직접 결정하세요."


@dataclass(frozen=True, slots=True)
class RenderedAlert:
    """A message ready to hand to a notification provider."""

    title: str
    body: str
    payload: dict[str, object]

    def as_text(self) -> str:
        return f"{self.title}\n\n{self.body}"


def _escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def _allocation_lines(allocation: TargetAllocation | None) -> str:
    if allocation is None:
        return "목표 배분: 없음 (상태 불명)"
    rows = [
        f"  {asset.value}: {weight:.0%}"
        for asset, weight in sorted(
            allocation.weights.items(), key=lambda item: -item[1]
        )
        if weight > 0
    ]
    return "목표 배분:\n" + "\n".join(rows)


def _score_line(state: MarketState) -> str:
    if state.composite_score is None:
        return "Market Score: 없음"
    change = state.score_change
    if change is None:
        return f"Market Score: {state.composite_score:.1f}"
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "—")
    return f"Market Score: {state.composite_score:.1f} ({arrow} {change:+.1f})"


def _indicator_lines(top_indicators: Mapping[str, float] | None) -> str:
    if not top_indicators:
        return ""
    rows = [f"  {name}: {score:.0f}" for name, score in top_indicators.items()]
    return "\n주요 지표:\n" + "\n".join(rows)


def _reason_lines(state: MarketState) -> str:
    if not state.reason_codes:
        return ""
    return "\n판단 근거: " + ", ".join(state.reason_codes)


def _common_body(
    state: MarketState,
    allocation: TargetAllocation | None,
    top_indicators: Mapping[str, float] | None = None,
) -> str:
    leverage = (
        f"{state.target_leverage:.2f}x" if state.target_leverage is not None else "없음"
    )
    return (
        f"{_score_line(state)}\n"
        f"Target Leverage: {leverage}\n"
        f"{_allocation_lines(allocation)}"
        f"{_indicator_lines(top_indicators)}"
        f"{_reason_lines(state)}\n\n"
        f"{DISCLAIMER}"
    )


# ------------------------------------------------------------------ templates


def regime_changed(
    state: MarketState,
    allocation: TargetAllocation | None,
    top_indicators: Mapping[str, float] | None = None,
) -> RenderedAlert:
    previous = state.previous_regime or "?"
    title = f"[레짐 변경] {previous} → {state.regime}"
    body = f"{state.observation_date} 기준\n\n" + _common_body(
        state, allocation, top_indicators
    )
    return RenderedAlert(
        title=title,
        body=body,
        payload={
            "previous_regime": previous,
            "new_regime": state.regime,
            "composite_score": state.composite_score,
            "previous_score": state.previous_score,
            "target_leverage": state.target_leverage,
        },
    )


def target_leverage_changed(
    state: MarketState,
    allocation: TargetAllocation | None,
    top_indicators: Mapping[str, float] | None = None,
    *,
    previous_leverage: float | None = None,
) -> RenderedAlert:
    current = state.target_leverage
    before = f"{previous_leverage:.2f}x" if previous_leverage is not None else "?"
    after = f"{current:.2f}x" if current is not None else "없음"
    title = f"[목표 레버리지 변경] {before} → {after}"
    body = f"{state.observation_date} 기준 (레짐: {state.regime})\n\n" + _common_body(
        state, allocation, top_indicators
    )
    return RenderedAlert(
        title=title,
        body=body,
        payload={
            "previous_leverage": previous_leverage,
            "target_leverage": current,
            "regime": state.regime,
        },
    )


def extreme_fear(
    state: MarketState,
    allocation: TargetAllocation | None,
    top_indicators: Mapping[str, float] | None = None,
) -> RenderedAlert:
    title = f"[극단적 공포] {state.regime}"
    body = (
        f"{state.observation_date} 기준, 시장이 가장 공포 구간에 있습니다.\n"
        "레버리지 확대 후보 구간이지만, TQQQ 편입은 별도의 바닥 확인 조건을 "
        "통과해야 합니다.\n\n" + _common_body(state, allocation, top_indicators)
    )
    return RenderedAlert(
        title=title,
        body=body,
        payload={"regime": state.regime, "composite_score": state.composite_score},
    )


def extreme_bubble(
    state: MarketState,
    allocation: TargetAllocation | None,
    top_indicators: Mapping[str, float] | None = None,
) -> RenderedAlert:
    title = f"[극단적 과열] {state.regime}"
    body = (
        f"{state.observation_date} 기준, 시장이 가장 과열 구간에 있습니다.\n"
        "레버리지 축소 구간입니다. 다만 전략상 시장을 완전히 이탈하지는 "
        "않습니다.\n\n" + _common_body(state, allocation, top_indicators)
    )
    return RenderedAlert(
        title=title,
        body=body,
        payload={"regime": state.regime, "composite_score": state.composite_score},
    )


def tqqq_candidate(
    state: MarketState,
    allocation: TargetAllocation | None,
    top_indicators: Mapping[str, float] | None = None,
    *,
    confirmations: tuple[str, ...] = (),
) -> RenderedAlert:
    weight = allocation.weight(Asset.TQQQ) if allocation else 0.0
    title = f"[TQQQ 후보] 바닥 확인 조건 통과 (목표 {weight:.0%})"
    confirmation_text = (
        "\n통과한 조건: " + ", ".join(confirmations) if confirmations else ""
    )
    body = (
        f"{state.observation_date} 기준, 공포 강도와 바닥 확인이 모두 "
        f"충족되었습니다.{confirmation_text}\n\n"
        + _common_body(state, allocation, top_indicators)
    )
    return RenderedAlert(
        title=title,
        body=body,
        payload={
            "regime": state.regime,
            "tqqq_weight": weight,
            "confirmations": list(confirmations),
        },
    )


def data_failure(
    state: MarketState,
    allocation: TargetAllocation | None = None,  # noqa: ARG001
    top_indicators: Mapping[str, float] | None = None,  # noqa: ARG001
    *,
    details: tuple[str, ...] = (),
) -> RenderedAlert:
    """The one alert that must never look like investment advice.

    It takes the same arguments as the other templates so ``render()`` can call
    any of them uniformly, and deliberately ignores the allocation and the
    indicators: a failed day has no advice to show.
    """
    title = "[데이터 실패] 오늘 신호를 생성하지 않았습니다"
    detail_text = "\n".join(f"  - {item}" for item in details) or "  - 상세 정보 없음"
    body = (
        f"{state.observation_date} 기준 필수 데이터를 신뢰할 수 없어 "
        f"레짐을 UNKNOWN 으로 처리했습니다.\n\n"
        f"원인:\n{detail_text}\n\n"
        "목표 배분과 레버리지는 계산하지 않았습니다. 이전 신호를 그대로 "
        "유지할지는 직접 판단하세요.\n\n"
        f"{DISCLAIMER}"
    )
    return RenderedAlert(
        title=title,
        body=body,
        payload={"reason_codes": list(state.reason_codes), "details": list(details)},
    )


#: ``alerts.yaml`` template name -> renderer.
TEMPLATES = {
    "regime_changed": regime_changed,
    "target_leverage_changed": target_leverage_changed,
    "extreme_fear": extreme_fear,
    "extreme_bubble": extreme_bubble,
    "tqqq_candidate": tqqq_candidate,
    "data_failure": data_failure,
}

#: Every event type ``PRD.md`` §16 lists must have a template.
EVENT_TEMPLATES = {
    EventType.REGIME_CHANGED: "regime_changed",
    EventType.TARGET_LEVERAGE_CHANGED: "target_leverage_changed",
    EventType.EXTREME_FEAR: "extreme_fear",
    EventType.EXTREME_BUBBLE: "extreme_bubble",
    EventType.TQQQ_CANDIDATE: "tqqq_candidate",
    EventType.DATA_FAILURE: "data_failure",
}


def render(
    template: str,
    state: MarketState,
    allocation: TargetAllocation | None = None,
    top_indicators: Mapping[str, float] | None = None,
    **extra: object,
) -> RenderedAlert:
    try:
        renderer = TEMPLATES[template]
    except KeyError as exc:
        raise KeyError(f"unknown alert template {template!r}; known: {sorted(TEMPLATES)}") from exc
    return renderer(state, allocation, top_indicators, **extra)  # type: ignore[operator]


def as_html(alert: RenderedAlert) -> str:
    """Telegram HTML rendering: bold title, plain body."""
    return f"<b>{_escape(alert.title)}</b>\n\n{_escape(alert.body)}"


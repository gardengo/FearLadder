"""TASK-120 .. TASK-122 — templates, the alert engine, deduplication, Telegram."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from fear_ladder.alerts.engine import (
    AlertContext,
    AlertEngine,
    NotificationError,
    NotificationProvider,
    NullNotifier,
)
from fear_ladder.alerts.telegram import (
    MAX_MESSAGE_CHARS,
    RecordingNotifier,
    TelegramNotConfiguredError,
    TelegramNotifier,
)
from fear_ladder.alerts.templates import DISCLAIMER, TEMPLATES, render
from fear_ladder.config.schema import TelegramSpec
from fear_ladder.constants import UNKNOWN_REGIME, Asset, EventType
from fear_ladder.data.models import MarketState, TargetAllocation

DAY = date(2024, 3, 16)
VERSION = "v0.0-placeholder"


def _state(**overrides: Any) -> MarketState:
    base: dict[str, Any] = {
        "observation_date": DAY,
        "composite_score": 18.0,
        "regime": "Capitulation",
        "previous_regime": "Fear",
        "previous_score": 33.0,
        "target_leverage": 2.3,
        "strategy_version": VERSION,
        "reason_codes": ("REGIME_CONFIRMED", "MOVED_TOWARD_FEAR"),
    }
    base.update(overrides)
    return MarketState(**base)


def _allocation(**weights: float) -> TargetAllocation:
    resolved = weights or {"QLD": 0.7, "TQQQ": 0.3}
    return TargetAllocation(
        observation_date=DAY,
        weights={Asset(name): value for name, value in resolved.items()},
        strategy_version=VERSION,
        regime="Capitulation",
    )


def _engine(alerts_config, notifier: NotificationProvider | None = None) -> AlertEngine:
    # RecordingNotifier, not NullNotifier: these tests are about what a
    # working channel does. NullNotifier means *no* channel, and now says so.
    return AlertEngine(alerts_config, notifier or RecordingNotifier())


@pytest.fixture
def alerts_config(placeholder_config):
    return placeholder_config.alerts


# ------------------------------------------------------------------ TASK-121


def test_every_event_type_has_a_template(placeholder_config) -> None:
    # PRD.md 16 lists six; none may be left without a message.
    for rule in placeholder_config.alerts.rules.values():
        assert rule.template in TEMPLATES, rule.template
    assert {member.value for member in EventType} == set(placeholder_config.alerts.rules)


def test_an_alert_explains_itself_rather_than_naming_a_regime() -> None:
    # CLAUDE_CODE_INITIAL_PROMPT.md 12 — "Regime = Fear" alone is not enough.
    rendered = render(
        "regime_changed",
        _state(),
        _allocation(),
        {"vix_level": 8.0, "drawdown_52w": 12.0},
    )
    body = rendered.body
    assert "Fear → Capitulation" in rendered.title
    assert "Market Score: 18.0" in body
    assert "-15.0" in body, "the score change must be visible"
    assert "Target Leverage: 2.30x" in body
    assert "QLD: 70%" in body and "TQQQ: 30%" in body
    assert "vix_level" in body
    assert "REGIME_CONFIRMED" in body


def test_every_template_carries_the_no_auto_trading_reminder() -> None:
    # PRD.md 1.2 — the user decides, every single time.
    for name in TEMPLATES:
        rendered = render(name, _state(), _allocation())
        assert DISCLAIMER in rendered.body, name


def test_the_data_failure_message_never_looks_like_advice() -> None:
    state = MarketState.unknown(
        DAY, strategy_version=VERSION, reason_codes=("STALE:VIX",)
    )
    rendered = render("data_failure", state, None, details=("VIX: latest 2024-03-01",))

    assert "신호를 생성하지 않았습니다" in rendered.title
    assert "VIX: latest 2024-03-01" in rendered.body
    assert "Target Leverage" not in rendered.body
    assert "목표 배분:\n" not in rendered.body


def test_an_unknown_state_shows_no_allocation() -> None:
    state = MarketState.unknown(DAY, strategy_version=VERSION, reason_codes=("x",))
    rendered = render("extreme_fear", state, None)
    assert "목표 배분: 없음" in rendered.body
    assert "Target Leverage: 없음" in rendered.body


def test_an_unknown_template_is_reported_with_alternatives() -> None:
    with pytest.raises(KeyError, match="unknown alert template"):
        render("teleport", _state(), _allocation())


# ------------------------------------------------------------------ TASK-122


def test_a_regime_change_produces_one_alert(alerts_config, uow) -> None:
    engine = _engine(alerts_config)
    context = AlertContext(
        state=_state(), allocation=_allocation(), regime_changed=True,
        most_fearful_regime="Capitulation", most_greedy_regime="Overheated",
    )
    report = engine.dispatch(context, uow.events)

    types = {event.event_type for event in report.created}
    assert EventType.REGIME_CHANGED in types


def test_re_running_the_same_day_sends_nothing(alerts_config, uow) -> None:
    """The guarantee ``ARCHITECTURE.md`` §11 asks for."""
    notifier = RecordingNotifier()
    engine = _engine(alerts_config, notifier)
    context = AlertContext(
        state=_state(), allocation=_allocation(), regime_changed=True,
        most_fearful_regime="Capitulation", most_greedy_regime="Overheated",
    )

    first = engine.dispatch(context, uow.events)
    assert first.sent
    sent_once = len(notifier.messages)

    second = engine.dispatch(context, uow.events)
    assert not second.created
    assert not second.sent
    assert len(notifier.messages) == sent_once
    assert all("already recorded" in reason for _, reason in second.suppressed)


def test_the_same_alert_on_a_different_day_is_sent(alerts_config, uow) -> None:
    engine = _engine(alerts_config)
    for day in (DAY, date(2024, 3, 17)):
        context = AlertContext(
            state=_state(observation_date=day), allocation=TargetAllocation(
                observation_date=day,
                weights={Asset.QLD: 0.7, Asset.TQQQ: 0.3},
                strategy_version=VERSION,
                regime="Capitulation",
            ),
            regime_changed=True,
            most_fearful_regime="Capitulation",
        )
        report = engine.dispatch(context, uow.events)
        assert report.created, day


def test_a_cooldown_suppresses_a_repeated_data_failure(alerts_config, uow) -> None:
    engine = _engine(alerts_config)
    first_day = MarketState.unknown(DAY, strategy_version=VERSION, reason_codes=("x",))
    engine.dispatch(AlertContext(state=first_day), uow.events)

    next_day = MarketState.unknown(
        date(2024, 3, 17), strategy_version=VERSION, reason_codes=("x",)
    )
    report = engine.dispatch(AlertContext(state=next_day), uow.events)
    assert not report.created
    assert any("cooldown" in reason for _, reason in report.suppressed)


def test_a_delivery_failure_is_recorded_not_lost(alerts_config, uow) -> None:
    class Broken:
        name = "broken"

        def send(self, alert: object) -> None:
            raise NotificationError("bot blocked")

    engine = _engine(alerts_config, Broken())
    context = AlertContext(
        state=_state(), allocation=_allocation(), regime_changed=True,
        most_fearful_regime="Capitulation",
    )
    report = engine.dispatch(context, uow.events)

    assert report.created
    assert report.failed
    stored = uow.events.get_alerts()
    assert any(event.delivery_status == "FAILED" for event in stored)


def test_a_failed_alert_can_be_retried_later(alerts_config, uow) -> None:
    engine = _engine(alerts_config)
    context = AlertContext(
        state=_state(), allocation=_allocation(), regime_changed=True,
        most_fearful_regime="Capitulation",
    )
    engine.dispatch(context, uow.events, send=False)
    assert uow.events.get_pending_alerts()

    report = engine.send_pending(uow.events)
    assert report.sent
    assert not uow.events.get_pending_alerts()


# ---------------------------------------------------------------- the rules


def test_a_data_failure_day_produces_only_the_failure_alert(alerts_config, uow) -> None:
    state = MarketState.unknown(DAY, strategy_version=VERSION, reason_codes=("STALE:VIX",))
    decisions = _engine(alerts_config).decide(
        AlertContext(state=state, data_failure_details=("VIX stale",))
    )
    assert [decision.event_type for decision in decisions] == [EventType.DATA_FAILURE]


def test_the_extreme_ends_of_the_scale_alert(alerts_config) -> None:
    engine = _engine(alerts_config)

    fearful = engine.decide(
        AlertContext(
            state=_state(regime="Capitulation"),
            allocation=_allocation(),
            most_fearful_regime="Capitulation",
            most_greedy_regime="Overheated",
        )
    )
    assert EventType.EXTREME_FEAR in {decision.event_type for decision in fearful}

    greedy = engine.decide(
        AlertContext(
            state=_state(regime="Overheated", composite_score=92.0, target_leverage=0.9),
            allocation=TargetAllocation(
                observation_date=DAY,
                weights={Asset.QQQ: 0.5, Asset.QLD: 0.2, Asset.CASH: 0.3},
                strategy_version=VERSION,
                regime="Overheated",
            ),
            most_fearful_regime="Capitulation",
            most_greedy_regime="Overheated",
        )
    )
    assert EventType.EXTREME_BUBBLE in {decision.event_type for decision in greedy}


def test_a_middle_regime_raises_no_extreme_alert(alerts_config) -> None:
    decisions = _engine(alerts_config).decide(
        AlertContext(
            state=_state(regime="Neutral", composite_score=50.0, target_leverage=1.4),
            allocation=TargetAllocation(
                observation_date=DAY,
                weights={Asset.QLD: 0.7, Asset.CASH: 0.3},
                strategy_version=VERSION,
                regime="Neutral",
            ),
            most_fearful_regime="Capitulation",
            most_greedy_regime="Overheated",
        )
    )
    types = {decision.event_type for decision in decisions}
    assert EventType.EXTREME_FEAR not in types
    assert EventType.EXTREME_BUBBLE not in types


def test_a_leverage_change_is_alerted(alerts_config) -> None:
    decisions = _engine(alerts_config).decide(
        AlertContext(
            state=_state(), allocation=_allocation(), previous_leverage=1.4,
            most_fearful_regime="Capitulation",
        )
    )
    assert EventType.TARGET_LEVERAGE_CHANGED in {d.event_type for d in decisions}


def test_an_unchanged_leverage_is_not_alerted(alerts_config) -> None:
    decisions = _engine(alerts_config).decide(
        AlertContext(
            state=_state(), allocation=_allocation(), previous_leverage=2.3,
            most_fearful_regime="Capitulation",
        )
    )
    assert EventType.TARGET_LEVERAGE_CHANGED not in {d.event_type for d in decisions}


def test_tqqq_is_only_alerted_when_the_gate_opened_and_it_is_held(
    alerts_config,
) -> None:
    engine = _engine(alerts_config)
    held = engine.decide(
        AlertContext(
            state=_state(),
            allocation=_allocation(),
            gate_passed=True,
            gate_confirmations=("deep_drawdown", "extreme_fear"),
            most_fearful_regime="Capitulation",
        )
    )
    assert EventType.TQQQ_CANDIDATE in {decision.event_type for decision in held}

    vetoed = engine.decide(
        AlertContext(
            state=_state(target_leverage=2.0),
            allocation=_allocation(QLD=1.0),
            gate_passed=False,
            most_fearful_regime="Capitulation",
        )
    )
    assert EventType.TQQQ_CANDIDATE not in {decision.event_type for decision in vetoed}


# ------------------------------------------------------------------ TASK-120


def _transport(captured: list[tuple[str, bytes]], response: dict[str, Any]):
    def send(url: str, body: bytes, timeout: float) -> dict[str, Any]:
        captured.append((url, body))
        return response

    return send


def test_telegram_reads_its_secrets_from_the_environment() -> None:
    spec = TelegramSpec()
    notifier = TelegramNotifier.from_spec(
        spec, env={"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"}
    )
    assert notifier.bot_token == "123:abc"
    assert notifier.chat_id == "42"


def test_missing_credentials_fail_loudly_and_name_the_variables() -> None:
    # Silence would be the worst failure mode for a monitoring system.
    with pytest.raises(TelegramNotConfiguredError, match="TELEGRAM_BOT_TOKEN"):
        TelegramNotifier.from_spec(TelegramSpec(), env={"TELEGRAM_CHAT_ID": "42"})
    with pytest.raises(TelegramNotConfiguredError, match="TELEGRAM_CHAT_ID"):
        TelegramNotifier.from_spec(TelegramSpec(), env={"TELEGRAM_BOT_TOKEN": "t"})


def test_a_message_is_posted_to_the_configured_chat() -> None:
    captured: list[tuple[str, bytes]] = []
    notifier = TelegramNotifier(
        bot_token="123:abc",
        chat_id="42",
        transport=_transport(captured, {"ok": True}),
        sleep=lambda _: None,
    )
    notifier.send_text("hello")

    url, body = captured[0]
    assert url.endswith("/bot123:abc/sendMessage")
    assert b"chat_id=42" in body
    assert b"hello" in body


def test_a_rate_limit_is_honoured_then_retried() -> None:
    import urllib.error

    slept: list[float] = []
    attempts = {"n": 0}

    def transport(url: str, body: bytes, timeout: float) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.HTTPError(
                url, 429, "Too Many Requests", {},  # type: ignore[arg-type]
                _BodyStub(b'{"parameters": {"retry_after": 7}}'),
            )
        return {"ok": True}

    notifier = TelegramNotifier(
        bot_token="t", chat_id="c", transport=transport, sleep=slept.append
    )
    notifier.send_text("hi")
    assert attempts["n"] == 2


def test_a_permanent_rejection_is_not_retried() -> None:
    import urllib.error

    attempts = {"n": 0}

    def transport(url: str, body: bytes, timeout: float) -> dict[str, Any]:
        attempts["n"] += 1
        raise urllib.error.HTTPError(
            url, 400, "Bad Request", {},  # type: ignore[arg-type]
            _BodyStub(b'{"description": "chat not found"}'),
        )

    notifier = TelegramNotifier(
        bot_token="t", chat_id="c", transport=transport, sleep=lambda _: None
    )
    with pytest.raises(NotificationError, match="rejected"):
        notifier.send_text("hi")
    assert attempts["n"] == 1, "a 400 cannot be fixed by trying again"


def test_a_long_message_is_truncated_with_a_pointer() -> None:
    captured: list[tuple[str, bytes]] = []
    notifier = TelegramNotifier(
        bot_token="t", chat_id="c", transport=_transport(captured, {"ok": True})
    )
    notifier.send_text("x" * (MAX_MESSAGE_CHARS + 500))

    from urllib.parse import parse_qs

    text = parse_qs(captured[0][1].decode())["text"][0]
    assert len(text) <= MAX_MESSAGE_CHARS
    assert "대시보드" in text


def test_an_alert_is_rendered_as_html_when_configured() -> None:
    captured: list[tuple[str, bytes]] = []
    notifier = TelegramNotifier(
        bot_token="t", chat_id="c", parse_mode="HTML",
        transport=_transport(captured, {"ok": True}),
    )
    from fear_ladder.data.models import AlertEvent

    notifier.send(
        AlertEvent(
            event_date=DAY,
            event_type=EventType.REGIME_CHANGED,
            severity="WARNING",
            title="Fear → Capitulation",
            body="body & more",
            strategy_version=VERSION,
        )
    )
    from urllib.parse import parse_qs

    payload = parse_qs(captured[0][1].decode())
    assert payload["parse_mode"][0] == "HTML"
    assert "<b>" in payload["text"][0]
    assert "&amp;" in payload["text"][0], "user text must be escaped"


def test_the_notifier_satisfies_the_port() -> None:
    assert isinstance(NullNotifier(), NotificationProvider)
    assert isinstance(TelegramNotifier(bot_token="t", chat_id="c"), NotificationProvider)


class _BodyStub:
    """Minimal stand-in for the file object an HTTPError carries."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        return None


def test_no_secret_is_ever_written_to_configuration() -> None:
    # ARCHITECTURE.md 9 — config holds variable names, never values.
    from fear_ladder import paths

    text = (paths.CONFIG_DIR / "alerts.yaml").read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN" in text
    assert ":AA" not in text, "that would look like a real bot token"
    assert "bot_token:" not in text


def test_utc_is_used_for_delivery_timestamps() -> None:
    from fear_ladder.data.models import AlertEvent

    alert = AlertEvent(
        event_date=DAY,
        event_type=EventType.DATA_FAILURE,
        severity="CRITICAL",
        title="t",
        body="b",
        strategy_version=VERSION,
    ).delivered("telegram", datetime(2024, 3, 16, 21, tzinfo=UTC))
    assert alert.sent_at == datetime(2024, 3, 16, 21, tzinfo=UTC)
    assert alert.delivery_status == "SENT"


def test_unknown_regime_never_triggers_an_extreme_alert(alerts_config) -> None:
    state = MarketState.unknown(DAY, strategy_version=VERSION, reason_codes=("x",))
    decisions = _engine(alerts_config).decide(
        AlertContext(state=state, most_fearful_regime=UNKNOWN_REGIME)
    )
    assert {d.event_type for d in decisions} == {EventType.DATA_FAILURE}


# ------------------------------- a channel that does not deliver (TASK-121)


def test_a_notifier_that_cannot_deliver_leaves_the_alert_pending(
    alerts_config, uow
) -> None:
    """The bug this replaces: a deployment with no Telegram token recorded its
    alerts as SENT by a provider named "null". Nobody received them, the
    database said otherwise, and they could never be retried."""
    engine = _engine(alerts_config, NullNotifier())
    context = AlertContext(
        state=_state(), allocation=_allocation(), regime_changed=True,
        most_fearful_regime="Capitulation",
    )
    report = engine.dispatch(context, uow.events)

    assert report.created
    assert not report.sent
    stored = uow.events.get_alerts()
    assert stored and all(event.delivery_status == "PENDING" for event in stored)
    assert uow.events.get_pending_alerts()


def test_pending_alerts_are_delivered_once_a_channel_appears(
    alerts_config, uow
) -> None:
    _engine(alerts_config, NullNotifier()).dispatch(
        AlertContext(
            state=_state(), allocation=_allocation(), regime_changed=True,
            most_fearful_regime="Capitulation",
        ),
        uow.events,
    )
    assert uow.events.get_pending_alerts()

    notifier = RecordingNotifier()
    report = _engine(alerts_config, notifier).send_pending(uow.events)

    assert report.sent
    assert notifier.messages
    assert not uow.events.get_pending_alerts()


def test_resending_without_a_channel_changes_nothing(alerts_config, uow) -> None:
    engine = _engine(alerts_config, NullNotifier())
    engine.dispatch(
        AlertContext(
            state=_state(), allocation=_allocation(), regime_changed=True,
            most_fearful_regime="Capitulation",
        ),
        uow.events,
    )
    before = len(uow.events.get_pending_alerts())
    report = engine.send_pending(uow.events)

    assert not report.sent and not report.failed
    assert len(uow.events.get_pending_alerts()) == before


def test_a_stale_pending_alert_is_stood_down_rather_than_sent(
    alerts_config, uow
) -> None:
    """An alert that waited months describes a market that has moved on."""
    _engine(alerts_config, NullNotifier()).dispatch(
        AlertContext(
            state=_state(), allocation=_allocation(), regime_changed=True,
            most_fearful_regime="Capitulation",
        ),
        uow.events,
    )
    notifier = RecordingNotifier()
    report = _engine(alerts_config, notifier).send_pending(uow.events, within_days=3)

    assert not report.sent
    assert report.suppressed
    assert not notifier.messages
    assert not uow.events.get_pending_alerts()

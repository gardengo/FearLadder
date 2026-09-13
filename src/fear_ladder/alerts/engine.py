"""Alert engine and deduplication (TASK-122).

``ARCHITECTURE.md`` §11 wants the daily worker to be safe to re-run::

    duplicate state 없음 / duplicate event 없음 / duplicate Telegram notification 없음

Deduplication is therefore layered, strongest first:

1. **The database.** ``alert_events.dedupe_key`` is UNIQUE, so a second insert
   for the same ``(date, type, strategy_version)`` cannot happen. This is the
   guarantee that survives crashes, concurrent runs and code bugs.
2. **Insert-then-send.** An alert is persisted *before* it is sent, and only a
   freshly-inserted row is sent. A re-run finds the row already there and sends
   nothing.
3. **Cooldown.** An optional quiet period after a rule fires, so a broken feed
   does not produce a DATA_FAILURE message every single day. ``cooldown_days: N``
   means "stay quiet for N days after sending", so ``N = 1`` suppresses an alert
   the day after one was sent and allows the day after that; ``N = 0`` never
   suppresses.

Delivery failures are recorded rather than retried forever: the row stays and
its status becomes FAILED, so the next run can see what happened.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Protocol, runtime_checkable

from fear_ladder.alerts.templates import RenderedAlert, render
from fear_ladder.config.schema import AlertRuleSpec, AlertsConfig
from fear_ladder.constants import UNKNOWN_REGIME, Asset, EventType
from fear_ladder.data.interfaces import EventRepository
from fear_ladder.data.models import AlertEvent, MarketState, TargetAllocation

logger = logging.getLogger(__name__)


class NotificationError(RuntimeError):
    """Raised when a provider cannot deliver a message."""


@runtime_checkable
class NotificationProvider(Protocol):
    """``ARCHITECTURE.md`` §10 — the port every channel implements."""

    name: str
    #: Whether ``send`` actually reaches a person. A provider that only records
    #: must say so, or the alert is marked delivered when nothing was delivered.
    delivers: bool

    def send(self, alert: AlertEvent) -> None:
        """Deliver the alert, or raise :class:`NotificationError`."""
        ...


@dataclass(slots=True)
class NullNotifier:
    """Accepts alerts and delivers none of them.

    Used where there is no channel: a dry run, a test, or — the case that
    matters — a deployment whose Telegram credentials are not set yet. It
    reports ``delivers = False`` so those alerts stay PENDING instead of being
    recorded as sent by a provider named "null", which is what the database
    used to claim.
    """

    name: str = "null"
    delivers: bool = False
    sent: list[AlertEvent] = field(default_factory=list)

    def send(self, alert: AlertEvent) -> None:
        self.sent.append(alert)
        logger.info("[not delivered] %s: %s", alert.event_type.value, alert.title)


@dataclass(frozen=True, slots=True)
class AlertContext:
    """Everything the rules need to decide what is worth saying."""

    state: MarketState
    allocation: TargetAllocation | None = None
    top_indicators: Mapping[str, float] = field(default_factory=dict)
    previous_leverage: float | None = None
    gate_confirmations: tuple[str, ...] = ()
    gate_passed: bool = False
    regime_changed: bool = False
    data_failure_details: tuple[str, ...] = ()
    most_fearful_regime: str | None = None
    most_greedy_regime: str | None = None


@dataclass(frozen=True, slots=True)
class AlertDecision:
    """One alert that the rules produced, before persistence."""

    event_type: EventType
    rule: AlertRuleSpec
    rendered: RenderedAlert

    def to_event(self, context: AlertContext) -> AlertEvent:
        return AlertEvent(
            event_date=context.state.observation_date,
            event_type=self.event_type,
            severity=self.rule.severity,
            title=self.rendered.title,
            body=self.rendered.body,
            strategy_version=context.state.strategy_version,
            payload=dict(self.rendered.payload),
        )


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """What one alert pass did."""

    created: tuple[AlertEvent, ...] = ()
    suppressed: tuple[tuple[EventType, str], ...] = ()
    sent: tuple[AlertEvent, ...] = ()
    failed: tuple[tuple[AlertEvent, str], ...] = ()

    def summary(self) -> str:
        parts = [f"created={len(self.created)}", f"sent={len(self.sent)}"]
        if self.suppressed:
            parts.append(f"suppressed={len(self.suppressed)}")
        if self.failed:
            parts.append(f"failed={len(self.failed)}")
        return "alerts: " + ", ".join(parts)


@dataclass(frozen=True, slots=True)
class AlertEngine:
    """Decides what to alert on, persists it, then sends only what is new."""

    config: AlertsConfig
    notifier: NotificationProvider

    # -- rules -------------------------------------------------------------
    def decide(self, context: AlertContext) -> list[AlertDecision]:
        rules = self.config.enabled_rules
        decisions: list[AlertDecision] = []
        state = context.state

        if state.is_unknown:
            # A failed day produces exactly one message, and it is never
            # investment advice (CLAUDE_CODE_INITIAL_PROMPT.md 14).
            rule = rules.get(EventType.DATA_FAILURE.value)
            if rule is not None:
                decisions.append(
                    AlertDecision(
                        EventType.DATA_FAILURE,
                        rule,
                        render(
                            rule.template,
                            state,
                            None,
                            None,
                            details=context.data_failure_details,
                        ),
                    )
                )
            return decisions

        if context.regime_changed and (rule := rules.get(EventType.REGIME_CHANGED.value)):
            decisions.append(
                AlertDecision(
                    EventType.REGIME_CHANGED,
                    rule,
                    render(rule.template, state, context.allocation, context.top_indicators),
                )
            )

        if self._leverage_changed(context) and (
            rule := rules.get(EventType.TARGET_LEVERAGE_CHANGED.value)
        ):
            decisions.append(
                AlertDecision(
                    EventType.TARGET_LEVERAGE_CHANGED,
                    rule,
                    render(
                        rule.template,
                        state,
                        context.allocation,
                        context.top_indicators,
                        previous_leverage=context.previous_leverage,
                    ),
                )
            )

        if (rule := rules.get(EventType.EXTREME_FEAR.value)) and self._is_extreme(
            context, rule, fearful=True
        ):
            decisions.append(
                AlertDecision(
                    EventType.EXTREME_FEAR,
                    rule,
                    render(rule.template, state, context.allocation, context.top_indicators),
                )
            )

        if (rule := rules.get(EventType.EXTREME_BUBBLE.value)) and self._is_extreme(
            context, rule, fearful=False
        ):
            decisions.append(
                AlertDecision(
                    EventType.EXTREME_BUBBLE,
                    rule,
                    render(rule.template, state, context.allocation, context.top_indicators),
                )
            )

        holds_tqqq = (
            context.allocation is not None and context.allocation.weight(Asset.TQQQ) > 0
        )
        if context.gate_passed and holds_tqqq and (
            rule := rules.get(EventType.TQQQ_CANDIDATE.value)
        ):
            decisions.append(
                AlertDecision(
                    EventType.TQQQ_CANDIDATE,
                    rule,
                    render(
                        rule.template,
                        state,
                        context.allocation,
                        context.top_indicators,
                        confirmations=context.gate_confirmations,
                    ),
                )
            )

        return decisions

    # -- dispatch ----------------------------------------------------------
    def dispatch(
        self,
        context: AlertContext,
        repository: EventRepository,
        *,
        send: bool = True,
    ) -> DispatchReport:
        created: list[AlertEvent] = []
        suppressed: list[tuple[EventType, str]] = []
        sent: list[AlertEvent] = []
        failed: list[tuple[AlertEvent, str]] = []

        for decision in self.decide(context):
            event = decision.to_event(context)

            if reason := self._cooldown_reason(decision, event, repository):
                suppressed.append((decision.event_type, reason))
                continue

            # Persist first: a crash between persisting and sending costs a
            # missed message, while sending first could cost a duplicate one.
            if not repository.save_alert(event):
                suppressed.append((decision.event_type, "already recorded for this day"))
                continue
            created.append(event)

            if not send:
                continue
            try:
                self.notifier.send(event)
            except NotificationError as exc:
                logger.warning("delivery failed for %s: %s", event.dedupe_key, exc)
                repository.mark_alert_delivered(event.failed(self.notifier.name, str(exc)))
                failed.append((event, str(exc)))
                continue
            if not self.notifier.delivers:
                # Left PENDING on purpose. Marking it delivered here would make
                # the record claim a message nobody received, and the alert
                # would never be retried once a real channel is configured.
                continue
            repository.mark_alert_delivered(event.delivered(self.notifier.name))
            sent.append(event)

        report = DispatchReport(
            created=tuple(created),
            suppressed=tuple(suppressed),
            sent=tuple(sent),
            failed=tuple(failed),
        )
        logger.info("%s", report.summary())
        return report

    def send_pending(
        self, repository: EventRepository, *, within_days: int | None = None
    ) -> DispatchReport:
        """Retry alerts that were recorded but never delivered.

        ``within_days`` bounds how far back to reach. An alert that has been
        waiting for months describes a market that has moved on, so delivering
        it is noise rather than news; those are stood down instead.
        """
        sent: list[AlertEvent] = []
        failed: list[tuple[AlertEvent, str]] = []
        suppressed: list[tuple[EventType, str]] = []
        if not self.notifier.delivers:
            logger.warning(
                "no delivering channel configured; %d alert(s) stay pending",
                len(repository.get_pending_alerts()),
            )
            return DispatchReport()

        cutoff = None if within_days is None else date.today() - timedelta(days=within_days)
        for event in repository.get_pending_alerts():
            if cutoff is not None and event.event_date < cutoff:
                repository.mark_alert_delivered(
                    replace(
                        event,
                        delivery_status="SUPPRESSED",
                        error=f"older than {within_days} days when a channel appeared",
                    )
                )
                suppressed.append((event.event_type, "too old to be news"))
                continue
            try:
                self.notifier.send(event)
            except NotificationError as exc:
                repository.mark_alert_delivered(event.failed(self.notifier.name, str(exc)))
                failed.append((event, str(exc)))
                continue
            repository.mark_alert_delivered(event.delivered(self.notifier.name))
            sent.append(event)
        return DispatchReport(
            sent=tuple(sent), failed=tuple(failed), suppressed=tuple(suppressed)
        )

    # -- internals ---------------------------------------------------------
    def _cooldown_reason(
        self,
        decision: AlertDecision,
        event: AlertEvent,
        repository: EventRepository,
    ) -> str | None:
        if decision.rule.cooldown_days <= 0:
            return None
        last = repository.last_alert_date(
            decision.event_type, strategy_version=event.strategy_version
        )
        if last is None:
            return None
        gap = (event.event_date - last).days
        if 0 <= gap <= decision.rule.cooldown_days:
            resumes = last + timedelta(days=decision.rule.cooldown_days + 1)
            return f"cooldown until {resumes} (last sent {last}, gap {gap}d)"
        return None

    @staticmethod
    def _leverage_changed(context: AlertContext) -> bool:
        current = context.state.target_leverage
        previous = context.previous_leverage
        if current is None or previous is None:
            return False
        return abs(current - previous) > 1e-9

    @staticmethod
    def _is_extreme(context: AlertContext, rule: AlertRuleSpec, *, fearful: bool) -> bool:
        """Whether today's regime is the extreme end of the scale.

        ``alerts.yaml`` may name the qualifying regimes explicitly. When it does
        not — which is the shipped state, because the labels are still a
        research parameter — the extreme end of the configured scale is used.
        Deriving it means adding a regime cannot silently orphan these alerts.
        """
        regime = context.state.regime
        if regime == UNKNOWN_REGIME:
            return False
        if rule.regimes is not None:
            return regime in rule.regimes
        extreme = context.most_fearful_regime if fearful else context.most_greedy_regime
        return extreme is not None and regime == extreme

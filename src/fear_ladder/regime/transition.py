"""Regime transition logic (TASK-051) and regime events (TASK-052).

The classifier answers "which band is today's score in?". This module answers
the harder question: "has the regime actually *changed*?" Three independent
brakes, all research parameters:

``confirmation_days``
    The new regime must persist for N consecutive days before it is accepted.
    Filters one-day spikes.
``hysteresis``
    The score must clear the boundary by an extra margin, in the direction of
    travel. Stops a score hovering on a cut point from oscillating.
``minimum_duration_days``
    A freshly confirmed regime must be held for at least N days. Bounds the
    turnover a regime-change rebalance can generate (``BACKTEST_SPEC.md`` 16).

The engine is a deterministic replay of one day at a time: given the same inputs
it produces the same outputs, which is what TASK-102's frozen regression and the
daily worker's idempotency both depend on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date
from typing import Self

import pandas as pd
from pandas import DataFrame, Series

from fear_ladder.config.schema import TransitionSpec
from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.data.models import RegimeEvent
from fear_ladder.regime.classifier import RegimeError, RegimeScale

logger = logging.getLogger(__name__)


class ReasonCode:
    """Stable strings that explain a decision to a human (and to a test)."""

    INITIAL = "INITIAL_REGIME"
    CONFIRMED = "REGIME_CONFIRMED"
    PENDING = "CONFIRMATION_PENDING"
    BLOCKED_HYSTERESIS = "BLOCKED_BY_HYSTERESIS"
    BLOCKED_MIN_DURATION = "BLOCKED_BY_MIN_DURATION"
    UNKNOWN_INPUT = "UNKNOWN_INPUT"
    HELD = "REGIME_HELD"


@dataclass(frozen=True, slots=True)
class TransitionState:
    """Everything the engine needs to carry from one day to the next."""

    regime: str
    since: date
    days_held: int = 1
    pending_regime: str | None = None
    pending_days: int = 0

    @property
    def has_pending(self) -> bool:
        return self.pending_regime is not None


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    """What the engine decided for one day, and why."""

    observation_date: date
    score: float | None
    raw_regime: str
    regime: str
    previous_regime: str | None
    changed: bool
    reason_codes: tuple[str, ...]
    state: TransitionState

    @property
    def is_unknown_input(self) -> bool:
        return self.raw_regime == UNKNOWN_REGIME


@dataclass(frozen=True, slots=True)
class TransitionEngine:
    """Applies confirmation, hysteresis and minimum duration to raw regimes."""

    scale: RegimeScale
    confirmation_days: int
    hysteresis: float
    minimum_duration_days: int

    def __post_init__(self) -> None:
        if self.confirmation_days < 1:
            raise RegimeError("confirmation_days must be >= 1")
        if self.hysteresis < 0:
            raise RegimeError("hysteresis cannot be negative")
        if self.minimum_duration_days < 1:
            raise RegimeError("minimum_duration_days must be >= 1")

    @classmethod
    def from_spec(cls, scale: RegimeScale, spec: TransitionSpec) -> Self:
        if not spec.is_resolved:
            raise RegimeError(
                "transition confirmation_days / hysteresis / minimum_duration_days "
                "are unresolved research parameters (TASK-085)"
            )
        assert spec.confirmation_days is not None
        assert spec.hysteresis is not None
        assert spec.minimum_duration_days is not None
        return cls(
            scale=scale,
            confirmation_days=spec.confirmation_days,
            hysteresis=spec.hysteresis,
            minimum_duration_days=spec.minimum_duration_days,
        )

    # -- one day -----------------------------------------------------------
    def step(
        self,
        state: TransitionState | None,
        day: date,
        score: float | None,
    ) -> TransitionDecision:
        raw_regime = self.scale.classify(score)

        if state is None:
            # Bootstrap: there is nothing to change *from*, so the first usable
            # score simply establishes the regime.
            if raw_regime == UNKNOWN_REGIME:
                return TransitionDecision(
                    observation_date=day,
                    score=score,
                    raw_regime=raw_regime,
                    regime=UNKNOWN_REGIME,
                    previous_regime=None,
                    changed=False,
                    reason_codes=(ReasonCode.UNKNOWN_INPUT,),
                    state=None,  # type: ignore[arg-type]
                )
            return TransitionDecision(
                observation_date=day,
                score=score,
                raw_regime=raw_regime,
                regime=raw_regime,
                previous_regime=None,
                changed=False,
                reason_codes=(ReasonCode.INITIAL,),
                state=TransitionState(regime=raw_regime, since=day),
            )

        if raw_regime == UNKNOWN_REGIME:
            # A day without a score neither confirms nor cancels anything: the
            # standing regime is held, and the caller records the *day* as
            # UNKNOWN (ARCHITECTURE.md 15).
            return TransitionDecision(
                observation_date=day,
                score=score,
                raw_regime=raw_regime,
                regime=state.regime,
                previous_regime=state.regime,
                changed=False,
                reason_codes=(ReasonCode.UNKNOWN_INPUT, ReasonCode.HELD),
                state=replace(state, days_held=state.days_held + 1),
            )

        held = replace(state, days_held=state.days_held + 1)

        if raw_regime == state.regime:
            cleared = replace(held, pending_regime=None, pending_days=0)
            return self._hold(day, score, raw_regime, cleared)

        reasons: list[str] = []

        if not self._clears_hysteresis(state.regime, raw_regime, float(score)):  # type: ignore[arg-type]
            reasons.append(
                f"{ReasonCode.BLOCKED_HYSTERESIS}:{self.hysteresis:g}"
            )
            # The candidate has not truly left the current band, so no
            # confirmation progress is made either.
            return self._hold(
                day, score, raw_regime, replace(held, pending_regime=None, pending_days=0), reasons
            )

        pending_days = held.pending_days + 1 if held.pending_regime == raw_regime else 1
        pending = replace(held, pending_regime=raw_regime, pending_days=pending_days)

        if pending_days < self.confirmation_days:
            reasons.append(f"{ReasonCode.PENDING}:{pending_days}/{self.confirmation_days}")
            return self._hold(day, score, raw_regime, pending, reasons)

        if held.days_held <= self.minimum_duration_days:
            reasons.append(
                f"{ReasonCode.BLOCKED_MIN_DURATION}:"
                f"{held.days_held}/{self.minimum_duration_days}"
            )
            return self._hold(day, score, raw_regime, pending, reasons)

        reasons.append(ReasonCode.CONFIRMED)
        reasons.append(self._direction_code(state.regime, raw_regime))
        return TransitionDecision(
            observation_date=day,
            score=score,
            raw_regime=raw_regime,
            regime=raw_regime,
            previous_regime=state.regime,
            changed=True,
            reason_codes=tuple(reasons),
            state=TransitionState(regime=raw_regime, since=day),
        )

    # -- whole history -----------------------------------------------------
    def run(self, scores: Series) -> list[TransitionDecision]:
        """Replay a score history day by day."""
        state: TransitionState | None = None
        decisions: list[TransitionDecision] = []
        for day, value in scores.sort_index().items():
            score = None if pd.isna(value) else float(value)
            decision = self.step(state, day, score)  # type: ignore[arg-type]
            decisions.append(decision)
            state = decision.state
        return decisions

    def to_frame(self, decisions: list[TransitionDecision]) -> DataFrame:
        return DataFrame(
            [
                {
                    "observation_date": decision.observation_date,
                    "score": decision.score,
                    "raw_regime": decision.raw_regime,
                    "regime": decision.regime,
                    "changed": decision.changed,
                    "reason_codes": ";".join(decision.reason_codes),
                }
                for decision in decisions
            ]
        ).set_index("observation_date")

    # -- internals ---------------------------------------------------------
    def _hold(
        self,
        day: date,
        score: float | None,
        raw_regime: str,
        state: TransitionState,
        reasons: list[str] | None = None,
    ) -> TransitionDecision:
        codes = tuple(reasons or []) or (ReasonCode.HELD,)
        return TransitionDecision(
            observation_date=day,
            score=score,
            raw_regime=raw_regime,
            regime=state.regime,
            previous_regime=state.regime,
            changed=False,
            reason_codes=codes,
            state=state,
        )

    def _clears_hysteresis(self, current: str, candidate: str, score: float) -> bool:
        """Has the score left the current band by the required margin?

        The relevant cut point is the one the score had to cross to leave the
        *current* regime, in the direction it is travelling — not the boundary of
        the destination, which may be several bands away.
        """
        if self.hysteresis == 0:
            return True
        current_index = self.scale.index_of(current)
        candidate_index = self.scale.index_of(candidate)
        low, high = self.scale.band(current)
        if candidate_index > current_index:
            return score >= high + self.hysteresis
        return score <= low - self.hysteresis

    def _direction_code(self, previous: str, new: str) -> str:
        return (
            "MOVED_TOWARD_FEAR"
            if self.scale.is_more_fearful(new, previous)
            else "MOVED_TOWARD_GREED"
        )


# ------------------------------------------------------------------ TASK-052


def build_regime_event(
    decision: TransitionDecision,
    *,
    strategy_version: str,
    previous_score: float | None = None,
) -> RegimeEvent | None:
    """Create the durable event for a confirmed transition.

    Returns ``None`` when nothing changed, so callers can write
    ``if event := build_regime_event(...)`` without re-checking.
    """
    if not decision.changed or decision.previous_regime is None:
        return None
    return RegimeEvent(
        event_date=decision.observation_date,
        previous_regime=decision.previous_regime,
        new_regime=decision.regime,
        previous_score=previous_score,
        new_score=decision.score,
        reason_codes=decision.reason_codes,
        strategy_version=strategy_version,
    )

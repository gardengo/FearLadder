"""The daily worker (TASK-110, TASK-111, TASK-112, TASK-133).

``ARCHITECTURE.md`` §4.1 fixes the stages::

    collect → validate → indicators → score → regime → allocation
    → persistence → event detection → alert → track record

Two design decisions are worth stating, because they are what make the operator
guarantees true rather than merely intended.

**The worker recomputes indicators and scores every run, and resumes the regime.**
Indicators and scores are rebuilt from the loaded history each day: they only
look back a bounded number of days, so the same date gets the same score
whatever the window, and a re-run has no accumulated state to double-count.

The regime rules are different, and until TASK-188 the worker treated them the
same way. A minimum holding period means the rules never forget their first
day, and the loaded window begins one day later every day — so replaying them
from the start of the window gave the same date a different regime depending on
when it was asked. The worker now resumes instead: from the row it stored for
the previous day (``regime_memory``), or, the first time, from the frozen
anchor — the state the backtest's own replay ended in
(:mod:`fear_ladder.regime.anchor`). The live regime is therefore the backtest's
replay continued, which is the only sense in which "a regime the backtest shows
for a past date is the regime the worker would have produced" can be true. A
re-run stays idempotent: it resumes from the same stored row and steps the same
days.

**Failure produces UNKNOWN, never a guess.**
If a mandatory input is missing, stale or dated in the future, the day is
recorded as UNKNOWN with no score and no allocation, a DATA_FAILURE alert is
raised, and any allocation previously stored for that date is removed. A wrong
"normal" state is the one output this system must never produce
(``CONTRIBUTING.md`` §5, §14).

**The track record is appended rather than replayed too, and is the one stage
allowed to fail.** See :mod:`fear_ladder.pipeline.track_record`
for the first half. For the second: by the time it runs the state is stored and
the alert is out, and an accounting error must not be able to take either back.

This module must not import :mod:`fear_ladder.research` — the production path
never calls the optimiser (``BACKTEST_SPEC.md`` §28), and a test enforces it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pandas as pd
from pandas import DataFrame, Series

from fear_ladder.alerts.engine import (
    AlertContext,
    AlertEngine,
    DispatchReport,
    NotificationProvider,
    NullNotifier,
)
from fear_ladder.allocation.engine import AllocationDecision, AllocationEngine
from fear_ladder.allocation.tqqq_gate import GateContext
from fear_ladder.config.schema import AppConfig
from fear_ladder.constants import UNKNOWN_REGIME, Asset, DataQualityStatus, PipelineStatus
from fear_ladder.data.collection import CollectionReport, CollectionService
from fear_ladder.data.models import (
    MarketState,
    PipelineRun,
    RegimeEvent,
    StrategyVersionRecord,
    TargetAllocation,
)
from fear_ladder.data.validators.freshness import FreshnessReport, FreshnessValidator
from fear_ladder.indicators.computations import IndicatorError
from fear_ladder.indicators.engine import IndicatorEngine, IndicatorResult
from fear_ladder.pipeline.track_record import TrackRecorder, TrackRecordUpdate
from fear_ladder.regime.anchor import RegimeAnchor, load_anchor
from fear_ladder.regime.classifier import RegimeError, RegimeScale
from fear_ladder.regime.transition import (
    TransitionDecision,
    TransitionEngine,
    TransitionState,
    build_regime_event,
)
from fear_ladder.scoring.composite import ScoreBreakdown, ScoreEngine

logger = logging.getLogger(__name__)

#: How much history the worker loads. Long enough to warm up the longest
#: configured window several times over, short enough to stay fast.
DEFAULT_LOOKBACK_DAYS = 2000

#: How many indicators to name in an alert.
TOP_INDICATOR_COUNT = 3


class RegimeMemoryError(RuntimeError):
    """The regime replay has a state to resume from but cannot reach it."""


@dataclass(frozen=True, slots=True)
class _ResumePoint:
    """Where a run's regime replay picks up: the state at the close of ``as_of``."""

    as_of: date
    state: TransitionState
    from_anchor: bool


@dataclass(frozen=True, slots=True)
class DailyResult:
    """Everything one run produced."""

    run: PipelineRun
    state: MarketState
    allocation: TargetAllocation | None = None
    regime_event: RegimeEvent | None = None
    alerts: DispatchReport = field(default_factory=DispatchReport)
    collection: CollectionReport | None = None
    freshness: FreshnessReport | None = None
    breakdown: ScoreBreakdown | None = None
    gate_passed: bool = False
    #: ``None`` when the day produced no signal, or the record could not be
    #: extended. Neither stops the run.
    track_record: TrackRecordUpdate | None = None

    @property
    def is_data_failure(self) -> bool:
        return self.state.is_unknown

    def summary(self) -> str:
        if self.is_data_failure:
            return (
                f"{self.state.observation_date}: DATA FAILURE — "
                f"{', '.join(self.state.reason_codes) or 'unknown cause'}"
            )
        score = self.state.composite_score
        leverage = self.state.target_leverage
        return (
            f"{self.state.observation_date}: {self.state.regime} "
            f"(score {score:.1f}, leverage {leverage:.2f}x)"
            + (" [regime changed]" if self.state.regime_changed else "")
        )


@dataclass(slots=True)
class DailyPipeline:
    """Runs one day of the operational pipeline."""

    config: AppConfig
    notifier: NotificationProvider = field(default_factory=NullNotifier)
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC)
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    collection_service: CollectionService | None = None
    #: Where the regime replay starts while the database holds no memory of its
    #: own. Left as ``None`` it is read from ``config/frozen`` for the configured
    #: strategy version; a version with no anchor file starts from the first
    #: scored day, as a research profile should.
    anchor: RegimeAnchor | None = None

    # Derived once at construction so an unresolved parameter fails here rather
    # than half way through a run that has already written rows.
    scale: RegimeScale = field(init=False)
    transitions: TransitionEngine = field(init=False)
    indicators: IndicatorEngine = field(init=False)
    scores: ScoreEngine = field(init=False)
    allocations: AllocationEngine = field(init=False)
    alerts: AlertEngine = field(init=False)
    freshness: FreshnessValidator = field(init=False)

    def __post_init__(self) -> None:
        self.scale = RegimeScale.from_spec(self.config.strategy.regime)
        self.transitions = TransitionEngine.from_spec(
            self.scale, self.config.strategy.transition
        )
        self.indicators = IndicatorEngine(self.config.indicators)
        self.scores = ScoreEngine.from_config(self.config)
        self.allocations = AllocationEngine.from_config(self.config)
        self.alerts = AlertEngine(self.config.alerts, self.notifier)
        self.freshness = FreshnessValidator(self.config.data_sources)

        version = self.config.strategy.strategy_version
        if self.anchor is None:
            self.anchor = load_anchor(version)
        if self.anchor is not None:
            if self.anchor.strategy_version != version:
                raise RegimeError(
                    f"regime anchor is for {self.anchor.strategy_version}, "
                    f"but the configured strategy is {version}"
                )
            self.scale.index_of(self.anchor.state.regime)  # raises on a foreign label

    # -- entry point -------------------------------------------------------
    def run(
        self,
        uow: object,
        *,
        as_of: date | None = None,
        collect: bool = True,
        send_alerts: bool = True,
    ) -> DailyResult:
        """Execute one day. ``uow`` is an active :class:`UnitOfWork`.

        Passing ``as_of`` in the past is a replay. The stored history then runs
        past the day being computed, which is normal for a replay and a clock
        error for today's run, so the freshness check is told which this is.
        """
        today = as_of or self.clock().date()
        replaying = as_of is not None and as_of < self.clock().date()
        run = PipelineRun(
            run_id=f"{today.isoformat()}-{uuid.uuid4().hex[:8]}",
            run_date=today,
            started_at=self.clock(),
            stage="start",
            strategy_version=self.config.strategy.strategy_version,
            data_version=self.config.strategy.data_version,
            parameter_version=self.config.strategy.parameter_version,
        )
        uow.strategies.save_run(run)  # type: ignore[attr-defined]

        try:
            return self._execute(
                uow,
                run,
                today,
                collect=collect,
                send_alerts=send_alerts,
                replaying=replaying,
            )
        except Exception as exc:
            # TASK-133: record the failure, never a plausible-looking state.
            logger.exception("daily pipeline failed on %s", today)
            uow.strategies.save_run(  # type: ignore[attr-defined]
                run.completed(PipelineStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            )
            raise

    # -- stages ------------------------------------------------------------
    def _execute(
        self,
        uow: object,
        run: PipelineRun,
        today: date,
        *,
        collect: bool,
        send_alerts: bool,
        replaying: bool = False,
    ) -> DailyResult:
        observations = uow.observations  # type: ignore[attr-defined]
        self._register_version(uow)

        collection: CollectionReport | None = None
        if collect:
            run = self._stage(uow, run, "collect")
            service = self.collection_service or CollectionService.from_config(
                self.config.data_sources
            )
            collection = service.collect(
                observations,
                start=today - pd.Timedelta(days=self.lookback_days).to_pytimedelta(),
                end=today,
            )

        run = self._stage(uow, run, "validate")
        freshness = self.freshness.validate(
            observations, as_of=today, replaying=replaying
        )
        if not freshness.usable:
            return self._data_failure(
                uow, run, today, freshness, collection, reasons=freshness.reason_codes()
            )

        run = self._stage(uow, run, "indicators")
        sources = self._load_sources(observations, today)
        try:
            indicator_results = self.indicators.compute_all(sources)
        except IndicatorError as exc:
            return self._data_failure(
                uow,
                run,
                today,
                freshness,
                collection,
                reasons=(f"INDICATOR_FAILURE:{exc}",),
            )

        run = self._stage(uow, run, "score")
        score_frame = self.scores.normalize(indicator_results)
        composite = self.scores.composite_series(score_frame)

        observation_date = self._latest_scored_date(composite, today)
        if observation_date is None:
            return self._data_failure(
                uow, run, today, freshness, collection, reasons=("NO_COMPUTABLE_SCORE",)
            )

        breakdown = self.scores.breakdown(score_frame, observation_date)

        run = self._stage(uow, run, "regime")
        try:
            decisions = self._replay_regime(uow, composite, observation_date)
        except RegimeMemoryError as exc:
            logger.error("%s", exc)
            return self._data_failure(
                uow, run, today, freshness, collection,
                reasons=("REGIME_MEMORY_OUT_OF_WINDOW",),
            )
        decision = self._decision_for(decisions, observation_date)
        if decision is None or decision.regime == UNKNOWN_REGIME:
            return self._data_failure(
                uow, run, today, freshness, collection, reasons=("NO_CONFIRMED_REGIME",)
            )

        run = self._stage(uow, run, "allocation")
        allocation_decision = self._allocate(decision, indicator_results, observation_date)

        run = self._stage(uow, run, "persistence")
        previous = uow.states.get_previous_state(  # type: ignore[attr-defined]
            observation_date, strategy_version=self.config.strategy.strategy_version
        )
        state = self._build_state(
            decision, allocation_decision, breakdown, previous, freshness
        )
        uow.states.save_state(state, allocation_decision.allocation)  # type: ignore[attr-defined]
        # In the same transaction as the state it explains: a stored regime
        # whose memory was lost would be resumed from an older row tomorrow.
        uow.regime_memory.save(  # type: ignore[attr-defined]
            [
                stepped.state.to_memory(
                    stepped.observation_date,
                    strategy_version=self.config.strategy.strategy_version,
                )
                for stepped in decisions
                if stepped.observation_date <= observation_date
            ]
        )
        uow.indicators.save_scores(  # type: ignore[attr-defined]
            self.scores.to_domain_scores(score_frame, observation_date),
            strategy_version=self.config.strategy.strategy_version,
        )
        uow.indicators.save_values(  # type: ignore[attr-defined]
            [result.to_domain(observation_date) for result in indicator_results.values()]
        )

        run = self._stage(uow, run, "events")
        regime_event = build_regime_event(
            decision,
            strategy_version=self.config.strategy.strategy_version,
            previous_score=previous.composite_score if previous else None,
        ) or self.transitions.missed_change_event(
            decision,
            stored_regime=previous.regime if previous else None,
            strategy_version=self.config.strategy.strategy_version,
            previous_score=previous.composite_score if previous else None,
        )
        if regime_event is not None:
            uow.events.save_regime_event(regime_event)  # type: ignore[attr-defined]

        run = self._stage(uow, run, "alert")
        dispatch = self.alerts.dispatch(
            self._alert_context(state, allocation_decision, breakdown, previous),
            uow.events,  # type: ignore[attr-defined]
            send=send_alerts,
        )

        run = self._stage(uow, run, "track_record")
        track_record = self._record_track(uow, observation_date)

        finished = run.completed(PipelineStatus.SUCCESS)
        uow.strategies.save_run(finished)  # type: ignore[attr-defined]

        result = DailyResult(
            run=finished,
            state=state,
            track_record=track_record,
            allocation=allocation_decision.allocation,
            regime_event=regime_event,
            alerts=dispatch,
            collection=collection,
            freshness=freshness,
            breakdown=breakdown,
            gate_passed=bool(allocation_decision.gate and allocation_decision.gate.allowed),
        )
        logger.info("%s", result.summary())
        return result

    # -- helpers -----------------------------------------------------------
    def _stage(self, uow: object, run: PipelineRun, stage: str) -> PipelineRun:
        from dataclasses import replace

        updated = replace(run, stage=stage)
        uow.strategies.save_run(updated)  # type: ignore[attr-defined]
        logger.debug("stage: %s", stage)
        return updated

    def _load_sources(self, observations: object, today: date) -> dict[str, Series]:
        names = sorted(
            {spec.source for spec in self.config.indicators.enabled_indicators.values()}
        )
        start = today - pd.Timedelta(days=self.lookback_days).to_pytimedelta()
        frame: DataFrame = observations.get_series_frame(  # type: ignore[attr-defined]
            names, start=start, end=today
        )
        return {str(column): frame[column].dropna() for column in frame.columns}

    def _replay_regime(
        self, uow: object, composite: Series, day: date
    ) -> list[TransitionDecision]:
        """The transition decisions this run adds, ending with the one for ``day``.

        Resumed runs return only the days stepped since the resume point —
        usually just ``day``, more when the worker missed some. A run with
        nothing to resume from replays the loaded history from its first scored
        day and returns the last decision alone: that is the only one worth
        remembering, and the next run resumes from it.
        """
        resume = self._resume_point(uow, day)
        if resume is None:
            if self.config.strategy.is_production_ready:
                logger.warning(
                    "no stored regime memory before %s and no frozen anchor for %s: "
                    "replaying from the first scored day in the loaded window. The "
                    "result depends on where that window starts; write an anchor "
                    "with scripts/make_regime_anchor.py.",
                    day,
                    self.config.strategy.strategy_version,
                )
            last = self._decision_for(self.transitions.run(composite), day)
            return [] if last is None or last.state is None else [last]

        if composite.empty or resume.as_of < composite.index.min():
            raise RegimeMemoryError(
                f"the regime replay would resume from {resume.as_of}, but the loaded "
                f"history starts at {None if composite.empty else composite.index.min()}: "
                "the days in between cannot be stepped. Rebuild the anchor from the "
                "full history (scripts/make_regime_anchor.py)."
            )
        if resume.from_anchor:
            logger.info(
                "regime replay resumes from the frozen anchor: %s since %s, as of %s",
                resume.state.regime,
                resume.state.since,
                resume.as_of,
            )
        ahead = composite[(composite.index > resume.as_of) & (composite.index <= day)]
        return self.transitions.run(ahead, state=resume.state)

    def _resume_point(self, uow: object, day: date) -> _ResumePoint | None:
        """The newest known replay state strictly before ``day``.

        The anchor wins a tie with a stored row of the same date, and any row
        older than it: the anchor is the backtest's own state, while a row from
        before it can only have come from a replay that started somewhere else.
        """
        stored = uow.regime_memory.latest_before(  # type: ignore[attr-defined]
            day, strategy_version=self.config.strategy.strategy_version
        )
        anchor = self.anchor if self.anchor is not None and self.anchor.as_of < day else None
        if anchor is not None and (stored is None or stored.observation_date <= anchor.as_of):
            return _ResumePoint(anchor.as_of, anchor.state, from_anchor=True)
        if stored is not None:
            return _ResumePoint(
                stored.observation_date, TransitionState.from_memory(stored), from_anchor=False
            )
        return None

    @staticmethod
    def _latest_scored_date(composite: Series, today: date) -> date | None:
        usable = composite.dropna()
        usable = usable[usable.index <= today]
        return None if usable.empty else usable.index[-1]

    @staticmethod
    def _decision_for(
        decisions: list[TransitionDecision], day: date
    ) -> TransitionDecision | None:
        for decision in reversed(decisions):
            if decision.observation_date == day:
                return decision
        return None

    def _allocate(
        self,
        decision: TransitionDecision,
        indicator_results: dict[str, IndicatorResult],
        day: date,
    ) -> AllocationDecision:
        context = GateContext(
            observation_date=day,
            indicator_values={
                name: result.value_on(day) for name, result in indicator_results.items()
            },
            composite_score=decision.score,
            regime=decision.regime,
            trend_broken=self._trend_broken(indicator_results, day),
        )
        return self.allocations.allocate(decision.regime, day, context=context)

    def _record_track(self, uow: object, day: date) -> TrackRecordUpdate | None:
        """Extend the paper track record through ``day``.

        The only stage whose failure is swallowed, and deliberately: the signal
        is already stored and announced, and what is left is bookkeeping about
        it. The record resumes from its last stored row, so a day lost here is
        picked up by the next run that succeeds — and the dashboard shows how
        far the record reaches, so falling behind is visible, not silent.
        """
        try:
            update = TrackRecorder(self.config).extend(uow, through=day)
        except Exception:
            logger.exception(
                "track record not extended through %s; the signal is unaffected "
                "and the next run resumes from the last stored day",
                day,
            )
            return None
        logger.info("%s", update.summary())
        return update

    def _register_version(self, uow: object) -> None:
        """Record which strategy produced the rows this run is about to write.

        The daily worker is the only thing that actually *runs* a version, so it
        is what registers one. Without this the table stayed empty and the
        dashboard had to guess the active version from whichever row happened to
        be newest — fine with one strategy, ambiguous the moment a second is
        frozen.

        Idempotent: re-running a day re-upserts the same row, and switching
        versions stands the previous one down rather than leaving two active.
        """
        strategy = self.config.strategy
        record = StrategyVersionRecord(
            strategy_version=strategy.strategy_version,
            parameter_status=strategy.parameter_status.value,
            parameter_version=strategy.parameter_version,
            data_version=strategy.data_version,
            frozen_at=strategy.frozen_at,
            manifest={
                "regime_labels": list(strategy.regime.labels or ()),
                "indicators": len(self.config.indicators.enabled_indicators),
            },
            is_active=True,
        )
        strategies = uow.strategies  # type: ignore[attr-defined]
        active = strategies.get_active_version()
        if active is not None and active.strategy_version != record.strategy_version:
            logger.warning(
                "active strategy changes from %s to %s",
                active.strategy_version,
                record.strategy_version,
            )
        strategies.save_version(record)

    def _trend_broken(
        self, indicator_results: dict[str, IndicatorResult], day: date
    ) -> bool | None:
        """Today's trend-filter state, replayed from the start of history.

        The filter has hysteresis, so today's state depends on yesterday's. It
        is recomputed from history rather than stored, which keeps a restarted
        run identical to one that never stopped.
        """
        filt = self.allocations.trend_filter
        if not filt.enabled:
            return None
        result = indicator_results.get(filt.indicator)
        if result is None:
            return None
        history = result.values[result.values.index <= day]
        if history.empty:
            return None
        depth = None
        if filt.gated_on_depth:
            measured = indicator_results.get(filt.depth_indicator or "")
            if measured is not None:
                depth = measured.values[measured.values.index <= day]
        return bool(filt.engaged_series(history, depth).iloc[-1])

    def _build_state(
        self,
        decision: TransitionDecision,
        allocation: AllocationDecision,
        breakdown: ScoreBreakdown,
        previous: MarketState | None,
        freshness: FreshnessReport,
    ) -> MarketState:
        quality = (
            DataQualityStatus.OK if not freshness.failures else DataQualityStatus.REVIEW
        )
        return MarketState(
            observation_date=decision.observation_date,
            composite_score=decision.score,
            regime=decision.regime,
            raw_regime=decision.raw_regime,
            previous_regime=previous.regime if previous else None,
            previous_score=previous.composite_score if previous else None,
            target_leverage=allocation.target_leverage,
            data_quality_status=quality,
            reason_codes=decision.reason_codes + allocation.reason_codes,
            score_breakdown=dict(breakdown.indicator_scores),
            strategy_version=self.config.strategy.strategy_version,
            data_version=self.config.strategy.data_version,
            parameter_version=self.config.strategy.parameter_version,
            created_at=self.clock(),
        )

    def _alert_context(
        self,
        state: MarketState,
        allocation: AllocationDecision,
        breakdown: ScoreBreakdown,
        previous: MarketState | None,
    ) -> AlertContext:
        return AlertContext(
            state=state,
            allocation=allocation.allocation,
            top_indicators=dict(breakdown.top_contributors(TOP_INDICATOR_COUNT)),
            previous_leverage=previous.target_leverage if previous else None,
            gate_confirmations=(
                allocation.gate.passed_rules if allocation.gate else ()
            ),
            gate_passed=bool(
                allocation.gate
                and allocation.gate.allowed
                and allocation.allocation
                and allocation.allocation.weight(Asset.TQQQ) > 0
            ),
            regime_changed=state.regime_changed,
            most_fearful_regime=self.scale.most_fearful,
            most_greedy_regime=self.scale.most_greedy,
        )

    def _data_failure(
        self,
        uow: object,
        run: PipelineRun,
        today: date,
        freshness: FreshnessReport | None,
        collection: CollectionReport | None,
        *,
        reasons: tuple[str, ...],
    ) -> DailyResult:
        """Record UNKNOWN and alert. No score, no allocation, no advice."""
        details = list(reasons)
        if freshness is not None:
            details.extend(source.describe() for source in freshness.failures)
        if collection is not None:
            details.extend(
                f"collection failed: {name}" for name in sorted(collection.failures)
            )

        previous = uow.states.get_previous_state(  # type: ignore[attr-defined]
            today, strategy_version=self.config.strategy.strategy_version
        )
        state = MarketState.unknown(
            today,
            strategy_version=self.config.strategy.strategy_version,
            reason_codes=tuple(reasons),
            previous_regime=previous.regime if previous else None,
            data_version=self.config.strategy.data_version,
            parameter_version=self.config.strategy.parameter_version,
            created_at=self.clock(),
        )
        # save_state clears any allocation previously stored for this date, so a
        # re-run that degrades does not leave yesterday's advice standing.
        uow.states.save_state(state, None)  # type: ignore[attr-defined]

        dispatch = self.alerts.dispatch(
            AlertContext(state=state, data_failure_details=tuple(details)),
            uow.events,  # type: ignore[attr-defined]
        )
        finished = run.completed(
            PipelineStatus.DATA_FAILURE, error="; ".join(reasons)
        )
        uow.strategies.save_run(finished)  # type: ignore[attr-defined]

        result = DailyResult(
            run=finished,
            state=state,
            alerts=dispatch,
            collection=collection,
            freshness=freshness,
        )
        logger.warning("%s", result.summary())
        return result

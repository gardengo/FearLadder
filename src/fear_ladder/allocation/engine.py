"""Allocation engine (TASK-060, TASK-061, TASK-062).

Maps a confirmed regime onto target sleeve weights, applies the TQQQ gate, and
checks the portfolio constraints the documents state:

* weights are non-negative and sum to 1 (``BACKTEST_SPEC.md`` 13)
* market exposure never reaches zero, however overheated (``PRD.md`` 2.1)
* ``L = 1·w_QQQ + 2·w_QLD + 3·w_TQQQ`` is reported as a research metric, not as
  a promise about realised returns (``BACKTEST_SPEC.md`` 14)

The regime→weights table itself is a research parameter and stays ``null`` in
``config/strategy.yaml``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Self

from fear_ladder.allocation.sleeves import leverage_of
from fear_ladder.allocation.tqqq_gate import GateContext, GateDecision, TqqqGate
from fear_ladder.allocation.trend_filter import TrendFilter, TrendVerdict
from fear_ladder.config.schema import AllocationConstraints, AllocationSpec, AppConfig
from fear_ladder.constants import UNKNOWN_REGIME, Asset
from fear_ladder.data.models import TargetAllocation

logger = logging.getLogger(__name__)

WEIGHT_TOLERANCE = 1e-6

#: Where a vetoed TQQQ weight goes. QLD is the next sleeve down the leverage
#: ladder, so the gate reduces exposure by one step rather than inventing a new
#: portfolio. Configurable because it is a structural choice, not a threshold.
DEFAULT_GATE_FALLBACK = Asset.QLD


class AllocationError(ValueError):
    """Raised when a target allocation cannot be produced."""


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    """The allocation for one day, plus why it looks like that."""

    allocation: TargetAllocation | None
    regime: str
    reason_codes: tuple[str, ...] = ()
    gate: GateDecision | None = None
    trend: TrendVerdict | None = None

    @property
    def target_leverage(self) -> float | None:
        return None if self.allocation is None else self.allocation.target_leverage


@dataclass(frozen=True, slots=True)
class AllocationEngine:
    """Turns a confirmed regime into target weights."""

    mappings: dict[str, dict[Asset, float]]
    constraints: AllocationConstraints
    gate: TqqqGate
    strategy_version: str
    trend_filter: TrendFilter = field(
        default_factory=lambda: TrendFilter(
            indicator="", threshold=0.0, max_leverage_below=0.0, enabled=False
        )
    )
    gate_fallback_asset: Asset = DEFAULT_GATE_FALLBACK

    @classmethod
    def from_config(cls, config: AppConfig) -> Self:
        spec: AllocationSpec = config.strategy.allocation
        if not spec.is_resolved:
            raise AllocationError(
                "allocation.mappings is an unresolved research parameter (TASK-084); "
                "no target allocation can be produced yet"
            )
        assert spec.mappings is not None
        engine = cls(
            mappings={
                label: dict(weights) for label, weights in spec.mappings.items()
            },
            constraints=spec.constraints,
            gate=TqqqGate.from_spec(config.strategy.tqqq_gate),
            strategy_version=config.strategy.strategy_version,
            trend_filter=TrendFilter.from_spec(config.strategy.trend_filter),
        )
        engine.validate_mappings()
        return engine

    # -- validation --------------------------------------------------------
    def validate_mappings(self) -> None:
        """Check every configured regime against the stated constraints."""
        for label, weights in self.mappings.items():
            self._check_portfolio(label, weights)

    def _check_portfolio(
        self, label: str, weights: dict[Asset, float], *, trend_capped: bool = False
    ) -> None:
        if label == UNKNOWN_REGIME:
            raise AllocationError(f"{UNKNOWN_REGIME} must not carry an allocation")
        if self.constraints.weights_non_negative and any(w < 0 for w in weights.values()):
            raise AllocationError(f"allocation for {label!r} has a negative weight")
        total = sum(weights.values())
        if self.constraints.weights_sum_to_one and abs(total - 1.0) > WEIGHT_TOLERANCE:
            raise AllocationError(f"allocation for {label!r} sums to {total!r}, not 1.0")

        exposure = 1.0 - weights.get(Asset.CASH, 0.0)
        floor = self.constraints.min_market_exposure
        # PRD.md 2.1 forbids a full exit *because the market is overheated*. A
        # defensive cap in a broken downtrend is a different decision, so the
        # floor is not applied to it — but a total exit is still refused.
        if trend_capped:
            floor = 0.0 if floor is None else min(floor, 0.0)
        if floor is not None and exposure < floor - WEIGHT_TOLERANCE:
            raise AllocationError(
                f"allocation for {label!r} leaves only {exposure:.2%} market exposure, "
                f"below the {floor:.2%} floor"
            )

        leverage = leverage_of(weights)
        ceiling = self.constraints.max_target_leverage
        if ceiling is not None and leverage > ceiling + WEIGHT_TOLERANCE:
            raise AllocationError(
                f"allocation for {label!r} implies {leverage:.2f}x leverage, "
                f"above the {ceiling:.2f}x ceiling"
            )

    # -- the decision ------------------------------------------------------
    def allocate(
        self,
        regime: str,
        observation_date: date,
        *,
        context: GateContext | None = None,
    ) -> AllocationDecision:
        """Target weights for a confirmed regime on a given day."""
        if regime == UNKNOWN_REGIME:
            # ARCHITECTURE.md 15 / CLAUDE_CODE_INITIAL_PROMPT.md 14: an unknown
            # state produces no advice at all.
            return AllocationDecision(
                allocation=None,
                regime=regime,
                reason_codes=("NO_ALLOCATION:UNKNOWN_REGIME",),
            )

        try:
            weights = dict(self.mappings[regime])
        except KeyError as exc:
            raise AllocationError(
                f"no allocation configured for regime {regime!r}; "
                f"known: {sorted(self.mappings)}"
            ) from exc

        reasons: list[str] = [f"REGIME_MAPPING:{regime}"]
        gate_decision: GateDecision | None = None
        trend_verdict: TrendVerdict | None = None

        # The trend filter runs first. Capping leverage can remove the TQQQ
        # sleeve outright, and there is no point asking the bottom-confirmation
        # gate about a sleeve the book no longer holds.
        if self.trend_filter.enabled:
            observed = context.value(self.trend_filter.indicator) if context else None
            depth_name = self.trend_filter.depth_indicator
            depth = context.value(depth_name) if context and depth_name else None
            weights, trend_verdict = self.trend_filter.apply(
                weights,
                observed,
                engaged=context.trend_broken if context else None,
                depth_observed=depth,
            )
            reasons.extend(trend_verdict.reason_codes())

        if weights.get(Asset.TQQQ, 0.0) > 0:
            gate_context = context or GateContext(
                observation_date=observation_date, regime=regime
            )
            gate_decision = self.gate.evaluate(gate_context)
            reasons.extend(gate_decision.reason_codes())
            if not gate_decision.allowed:
                weights = self._veto_tqqq(weights)
                reasons.append(f"TQQQ_REALLOCATED_TO:{self.gate_fallback_asset.value}")

        self._check_portfolio(
            regime, weights, trend_capped=bool(trend_verdict and trend_verdict.capped)
        )
        allocation = TargetAllocation(
            observation_date=observation_date,
            weights=weights,
            strategy_version=self.strategy_version,
            regime=regime,
        )
        reasons.append(f"TARGET_LEVERAGE:{allocation.target_leverage:.2f}")
        return AllocationDecision(
            allocation=allocation,
            regime=regime,
            reason_codes=tuple(reasons),
            gate=gate_decision,
            trend=trend_verdict,
        )

    def _veto_tqqq(self, weights: dict[Asset, float]) -> dict[Asset, float]:
        """Move the vetoed TQQQ weight down one rung of the leverage ladder."""
        vetoed = dict(weights)
        moved = vetoed.pop(Asset.TQQQ, 0.0)
        fallback = self.gate_fallback_asset
        vetoed[fallback] = vetoed.get(fallback, 0.0) + moved
        return vetoed

    # -- reporting ---------------------------------------------------------
    def target_leverage(self, regime: str) -> float:
        """Leverage the un-gated mapping implies for a regime."""
        try:
            return leverage_of(self.mappings[regime])
        except KeyError as exc:
            raise AllocationError(f"unknown regime {regime!r}") from exc

    def leverage_ladder(self, labels: tuple[str, ...]) -> dict[str, float]:
        """Target leverage per regime, in the scale's fear→greed order."""
        return {label: self.target_leverage(label) for label in labels}

"""TQQQ bottom-confirmation gate (TASK-063).

``PRD.md`` 10 draws the distinction this module exists to enforce::

    Fear Intensity
        vs
    Bottom Confirmation

A deeply fearful regime is *necessary* for the most aggressive sleeve but never
*sufficient*: markets can stay fearful for a long time while still falling. So
the regime mapping proposes TQQQ, and this gate has to agree before it is held.

TASK-063 asks for the rule **interface**, not the rules' thresholds. The rules
are therefore data-driven: each one names an indicator and a single
``min_value``/``max_value`` bound that lives in configuration and is ``null``
until the research fixes it (``BACKTEST_SPEC.md`` 15).

Failure is conservative by construction: a rule whose indicator is missing does
not pass. Buying the most leveraged sleeve on absent evidence is the single
worst outcome this system could produce.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, Self, runtime_checkable

from fear_ladder.config.schema import TqqqGateSpec

logger = logging.getLogger(__name__)


class GateConfigurationError(ValueError):
    """Raised when the gate cannot be built from configuration."""


@dataclass(frozen=True, slots=True)
class GateContext:
    """Everything a bottom-confirmation rule may look at for one day."""

    observation_date: date
    indicator_values: Mapping[str, float | None] = field(default_factory=dict)
    composite_score: float | None = None
    regime: str | None = None
    #: Trend-filter state for this day, already resolved with hysteresis by
    #: :meth:`~fear_ladder.allocation.trend_filter.TrendFilter.engaged_series`.
    #: ``None`` leaves the filter to compare today's reading to its threshold,
    #: which has no memory of yesterday.
    trend_broken: bool | None = None

    def value(self, indicator: str) -> float | None:
        return self.indicator_values.get(indicator)


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """One rule's verdict, with the reasoning kept for the alert and dashboard."""

    name: str
    passed: bool
    detail: str
    observed: float | None = None
    threshold: float | None = None

    @property
    def evaluable(self) -> bool:
        return self.observed is not None


@runtime_checkable
class BottomConfirmationRule(Protocol):
    """The interface TASK-063 asks for."""

    name: str

    def evaluate(self, context: GateContext) -> RuleOutcome: ...


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """A rule of the form "indicator is at least / at most X".

    Every bottom-confirmation candidate in ``PRD.md`` 10 reduces to this shape,
    so one implementation covers them all and the rule set stays configuration
    rather than code.
    """

    name: str
    indicator: str
    bound: float
    at_least: bool
    description: str = ""

    def evaluate(self, context: GateContext) -> RuleOutcome:
        observed = context.value(self.indicator)
        if observed is None:
            return RuleOutcome(
                name=self.name,
                passed=False,
                detail=f"{self.indicator} is unavailable, so {self.name} cannot confirm",
                threshold=self.bound,
            )
        passed = observed >= self.bound if self.at_least else observed <= self.bound
        comparison = ">=" if self.at_least else "<="
        return RuleOutcome(
            name=self.name,
            passed=passed,
            detail=(
                f"{self.indicator}={observed:.4g} {comparison} {self.bound:.4g}"
                if passed
                else f"{self.indicator}={observed:.4g} fails {comparison} {self.bound:.4g}"
            ),
            observed=observed,
            threshold=self.bound,
        )


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether TQQQ may be held, and the full evidence for that answer."""

    observation_date: date
    allowed: bool
    outcomes: tuple[RuleOutcome, ...]
    required_failed: tuple[str, ...] = ()
    confirmations: int = 0
    required_confirmations: int = 0
    disabled: bool = False

    @property
    def passed_rules(self) -> tuple[str, ...]:
        return tuple(outcome.name for outcome in self.outcomes if outcome.passed)

    def reason_codes(self) -> tuple[str, ...]:
        if self.disabled:
            return ("TQQQ_GATE_DISABLED",)
        if self.allowed:
            return (
                "TQQQ_GATE_PASSED",
                f"CONFIRMATIONS:{self.confirmations}/{self.required_confirmations}",
            )
        codes = ["TQQQ_GATE_BLOCKED"]
        if self.required_failed:
            codes.append(f"REQUIRED_RULE_FAILED:{','.join(self.required_failed)}")
        else:
            codes.append(
                f"INSUFFICIENT_CONFIRMATIONS:{self.confirmations}/"
                f"{self.required_confirmations}"
            )
        return tuple(codes)

    def explain(self) -> str:
        lines = [outcome.detail for outcome in self.outcomes]
        return "; ".join(lines)


@dataclass(frozen=True, slots=True)
class TqqqGate:
    """Evaluates bottom confirmation before the most aggressive sleeve is held."""

    rules: tuple[BottomConfirmationRule, ...]
    required_rules: tuple[str, ...]
    min_confirmations: int
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        names = {rule.name for rule in self.rules}
        unknown = set(self.required_rules) - names
        if unknown:
            raise GateConfigurationError(
                f"required_rules names rules that do not exist: {sorted(unknown)}"
            )
        if self.min_confirmations < 1:
            raise GateConfigurationError("min_confirmations must be >= 1")
        if self.min_confirmations > len(self.rules):
            raise GateConfigurationError(
                f"min_confirmations={self.min_confirmations} exceeds the "
                f"{len(self.rules)} configured rules, so the gate could never open"
            )

    @classmethod
    def from_spec(cls, spec: TqqqGateSpec) -> Self:
        if not spec.enabled:
            return cls(rules=(), required_rules=(), min_confirmations=1, enabled=False)
        if not spec.is_resolved:
            raise GateConfigurationError(
                "tqqq_gate required_rules / min_confirmations are unresolved "
                "research parameters (TASK-063)"
            )
        if not spec.rule_params:
            raise GateConfigurationError("tqqq_gate is enabled but defines no rule_params")

        rules: list[BottomConfirmationRule] = []
        for name in spec.candidate_rules:
            params = spec.rule_params.get(name)
            if params is None:
                logger.warning("candidate rule %s has no rule_params; it is skipped", name)
                continue
            rules.append(_build_rule(name, params))

        assert spec.required_rules is not None and spec.min_confirmations is not None
        return cls(
            rules=tuple(rules),
            required_rules=tuple(spec.required_rules),
            min_confirmations=spec.min_confirmations,
        )

    def evaluate(self, context: GateContext) -> GateDecision:
        if not self.enabled:
            return GateDecision(
                observation_date=context.observation_date,
                allowed=True,
                outcomes=(),
                disabled=True,
            )

        outcomes = tuple(rule.evaluate(context) for rule in self.rules)
        passed = {outcome.name for outcome in outcomes if outcome.passed}
        required_failed = tuple(
            name for name in self.required_rules if name not in passed
        )
        confirmations = len(passed)
        allowed = not required_failed and confirmations >= self.min_confirmations
        return GateDecision(
            observation_date=context.observation_date,
            allowed=allowed,
            outcomes=outcomes,
            required_failed=required_failed,
            confirmations=confirmations,
            required_confirmations=self.min_confirmations,
        )


def _build_rule(name: str, params: Mapping[str, object]) -> ThresholdRule:
    indicator = params.get("indicator")
    if not isinstance(indicator, str):
        raise GateConfigurationError(f"rule {name!r} must name an indicator")

    bounds = {key: value for key, value in params.items() if key in ("min_value", "max_value")}
    if len(bounds) != 1:
        raise GateConfigurationError(
            f"rule {name!r} needs exactly one of min_value / max_value, got {sorted(bounds)}"
        )
    key, value = next(iter(bounds.items()))
    if value is None:
        raise GateConfigurationError(
            f"rule {name!r}: {key} is an unresolved research parameter"
        )
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise GateConfigurationError(f"rule {name!r}: {key} must be a number, got {value!r}")

    return ThresholdRule(
        name=name,
        indicator=indicator,
        bound=float(value),
        at_least=(key == "min_value"),
        description=str(params.get("description", "")),
    )

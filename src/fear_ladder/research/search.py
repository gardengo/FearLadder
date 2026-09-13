"""Parameter search (TASK-080 .. TASK-085).

One framework rather than six searchers: every task in Phase 8 is "vary part of
the strategy configuration, backtest, rank". What differs is only which part,
so each task contributes a *space builder* and shares the evaluation, the
guardrails and the ranking.

Two rules from ``BACKTEST_SPEC.md`` are enforced here rather than remembered:

§11 — a candidate is never ranked on one number::

    백테스트 성능만 보고 weight를 선택하지 않는다.
    CAGR, Sharpe, Sortino, Calmar, MDD, Turnover, Recovery Time 을 종합한다.

§20 — a search may not read a protected window. :class:`SplitGuard` makes that
structural: the search asks the guard before it evaluates anything.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from pandas import DataFrame

from fear_ladder.backtest.metrics import PerformanceMetrics
from fear_ladder.config.schema import (
    AppConfig,
    ConfigError,
    IndicatorsConfig,
    StrategyConfig,
)
from fear_ladder.constants import Asset
from fear_ladder.research.backtest_runner import BacktestRun, MarketData, StrategyBacktest
from fear_ladder.research.splits import Split, SplitGuard

logger = logging.getLogger(__name__)


class SearchError(ValueError):
    """Raised when a search cannot be set up or run."""


# --------------------------------------------------------------- candidates


@dataclass(frozen=True, slots=True)
class Candidate:
    """One parameter set to evaluate."""

    label: str
    overrides: dict[str, Any]
    dimension: str = "mixed"
    #: Per-indicator overrides, keyed by indicator name, each a partial
    #: ``IndicatorSpec`` payload merged one level deep (so ``normalization``
    #: can be replaced without restating ``params``).
    indicator_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def apply(self, config: AppConfig) -> AppConfig:
        """Return a config with these overrides, fully re-validated.

        Re-validating rather than patching in place is deliberate: a candidate
        that violates a documented constraint (weights not summing to 1, an
        allocation missing a regime) must fail here, not silently produce a
        meaningless backtest.
        """
        payload = config.strategy.model_dump()
        payload.update(self.overrides)
        payload["strategy_version"] = f"{config.strategy.strategy_version}+{self.label}"
        try:
            strategy = StrategyConfig(**payload)
        except ConfigError as exc:
            raise SearchError(f"candidate {self.label!r} is not a valid strategy: {exc}") from exc
        return AppConfig(
            indicators=self._indicators(config),
            strategy=strategy,
            alerts=config.alerts,
            data_sources=config.data_sources,
        )

    def _indicators(self, config: AppConfig) -> IndicatorsConfig:
        if not self.indicator_overrides:
            return config.indicators
        payload = config.indicators.model_dump()
        specs = dict(payload["indicators"])
        for name, override in self.indicator_overrides.items():
            if name not in specs:
                raise SearchError(f"candidate {self.label!r} overrides unknown indicator {name!r}")
            merged = dict(specs[name])
            for key, value in override.items():
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value
            specs[name] = merged
        payload["indicators"] = specs
        try:
            return IndicatorsConfig(**payload)
        except ConfigError as exc:
            raise SearchError(
                f"candidate {self.label!r} is not a valid indicator set: {exc}"
            ) from exc


# --------------------------------------------------------------- objective


@dataclass(frozen=True, slots=True)
class Objective:
    """A multi-metric score, per ``BACKTEST_SPEC.md`` 11.

    ``weights`` map a metric name to its contribution. Metrics where *less is
    better* (MDD, turnover) carry negative weights. This is a ranking aid for a
    human reading the table, not an automatic decision rule: nothing in the
    pipeline acts on it.

    The terms have to be on comparable scales or one of them silently becomes
    the whole objective. Raw ``turnover`` is the trap: over a 16-year window it
    reaches ~340 while CAGR is ~0.06, so any visible weight on it makes the
    search optimise turnover alone. ``turnover_per_year`` is used instead, and
    weighted only enough to break ties between otherwise similar candidates —
    trading costs are already charged inside the NAV, so this is about
    robustness, not economics.
    """

    weights: dict[str, float] = field(
        default_factory=lambda: {
            "cagr": 1.0,
            "calmar": 0.5,
            "sharpe": 0.3,
            "sortino": 0.2,
            "max_drawdown": -0.5,
            "turnover_per_year": -0.003,
        }
    )

    def score(self, metrics: PerformanceMetrics) -> float:
        return sum(
            weight * float(getattr(metrics, name, 0.0) or 0.0)
            for name, weight in self.weights.items()
        )

    def describe(self) -> str:
        parts = [f"{name}x{weight:+g}" for name, weight in sorted(self.weights.items())]
        return ", ".join(parts)


# ----------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """One evaluated candidate."""

    candidate: Candidate
    metrics: PerformanceMetrics
    objective: float
    run: BacktestRun | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class SearchReport:
    """Everything a search produced, ranked but not decided."""

    dimension: str
    split: Split
    objective: Objective
    outcomes: tuple[SearchOutcome, ...]
    failures: tuple[tuple[str, str], ...] = ()

    @property
    def ranked(self) -> list[SearchOutcome]:
        return sorted(self.outcomes, key=lambda item: item.objective, reverse=True)

    @property
    def best(self) -> SearchOutcome | None:
        return self.ranked[0] if self.outcomes else None

    def to_frame(self) -> DataFrame:
        if not self.outcomes:
            return DataFrame()
        rows = []
        for outcome in self.ranked:
            row = {"label": outcome.candidate.label, "objective": outcome.objective}
            row.update(outcome.metrics.to_dict())
            rows.append(row)
        return DataFrame(rows).set_index("label")

    def summary(self) -> str:
        best = self.best
        head = (
            f"{self.dimension} over {self.split.value}: {len(self.outcomes)} candidates"
        )
        if self.failures:
            head += f", {len(self.failures)} rejected"
        if best is None:
            return head
        return f"{head}; top={best.candidate.label} ({best.metrics.summary()})"


# ------------------------------------------------------------- the searcher


@dataclass(frozen=True, slots=True)
class GridSearch:
    """Evaluates candidates over one dataset window."""

    config: AppConfig
    data: MarketData
    guard: SplitGuard
    objective: Objective = field(default_factory=Objective)
    keep_runs: bool = False

    def run(
        self,
        candidates: Iterable[Candidate],
        *,
        split: Split = Split.RESEARCH,
        dimension: str = "mixed",
    ) -> SearchReport:
        # BACKTEST_SPEC.md 20 — asked before anything is evaluated.
        self.guard.check(split, purpose=f"{dimension} search")
        window = self.guard.split.window(split)

        outcomes: list[SearchOutcome] = []
        failures: list[tuple[str, str]] = []
        for candidate in candidates:
            try:
                run = StrategyBacktest(candidate.apply(self.config)).run(
                    self.data,
                    start=window.start,
                    end=window.end,
                    include_benchmarks=False,
                    name=candidate.label,
                )
            except (SearchError, ValueError) as exc:
                logger.info("candidate %s rejected: %s", candidate.label, exc)
                failures.append((candidate.label, str(exc)))
                continue
            outcomes.append(
                SearchOutcome(
                    candidate=candidate,
                    metrics=run.metrics,
                    objective=self.objective.score(run.metrics),
                    run=run if self.keep_runs else None,
                )
            )

        report = SearchReport(
            dimension=dimension,
            split=split,
            objective=self.objective,
            outcomes=tuple(outcomes),
            failures=tuple(failures),
        )
        logger.info("%s", report.summary())
        return report


# ------------------------------------------------------------ space builders
# One per Phase 8 task. Each yields candidates; none of them decides anything.


def indicator_subsets(
    config: AppConfig, *, subsets: Sequence[Sequence[str]]
) -> Iterator[Candidate]:
    """TASK-080 — compare candidate indicator combinations.

    Dropping an indicator means re-spreading its weight over the survivors, so
    each subset stays a valid convex combination.
    """
    known = set(config.indicators.enabled_indicators)
    for names in subsets:
        chosen = list(names)
        if not chosen:
            continue
        unknown = set(chosen) - known
        if unknown:
            raise SearchError(
                f"indicator subset names disabled/unknown indicators: {sorted(unknown)}"
            )
        share = 1.0 / len(chosen)
        weights = dict.fromkeys(chosen, share)
        weights[chosen[0]] += 1.0 - share * len(chosen)  # absorb rounding
        yield Candidate(
            label=f"indicators[{len(chosen)}]:{'+'.join(chosen[:3])}"
            + ("..." if len(chosen) > 3 else ""),
            overrides={"score": {"weights": weights}},
            dimension="indicator_selection",
        )


def weight_candidates(
    config: AppConfig, *, weight_sets: Sequence[dict[str, float]]
) -> Iterator[Candidate]:
    """TASK-081 — explicit weight vectors to compare."""
    known = set(config.indicators.enabled_indicators)
    for index, weights in enumerate(weight_sets):
        unknown = set(weights) - known
        if unknown:
            raise SearchError(
                f"weight set #{index} names disabled/unknown indicators: {sorted(unknown)}"
            )
        total = sum(weights.values())
        if total <= 0:
            continue
        normalised = {name: value / total for name, value in weights.items()}
        yield Candidate(
            label=f"weights#{index}",
            overrides={"score": {"weights": normalised}},
            dimension="weight_search",
        )


def regime_count_candidates(
    config: AppConfig,
    *,
    counts: Sequence[int] | None = None,
    label_prefix: str = "R",
) -> Iterator[Candidate]:
    """TASK-082 — compare 3 / 5 / 7 / 9 stages.

    Boundaries are spaced evenly so the comparison isolates the *number* of
    stages; where the cut points sit is TASK-083's question. Allocations are
    interpolated along the existing leverage ladder so every candidate stays a
    valid, monotone portfolio set.
    """
    candidates = counts or config.strategy.regime.research_candidates
    ladder = _leverage_ladder(config)
    for count in candidates:
        labels = tuple(f"{label_prefix}{index}" for index in range(count))
        step = 100.0 / count
        boundaries = tuple(round(step * (index + 1), 6) for index in range(count - 1))
        mappings = {
            label: _interpolate_allocation(ladder, index, count)
            for index, label in enumerate(labels)
        }
        yield Candidate(
            label=f"regimes={count}",
            overrides={
                "regime": {
                    "count": count,
                    "labels": labels,
                    "boundaries": boundaries,
                    "research_candidates": list(
                        config.strategy.regime.research_candidates
                    ),
                },
                "allocation": {
                    "mappings": mappings,
                    "constraints": config.strategy.allocation.constraints.model_dump(),
                },
                "tqqq_gate": config.strategy.tqqq_gate.model_dump(),
            },
            dimension="regime_count",
        )


def boundary_candidates(
    config: AppConfig, *, boundary_sets: Sequence[Sequence[float]]
) -> Iterator[Candidate]:
    """TASK-083 — move the regime cut points, keeping the stage count fixed."""
    regime = config.strategy.regime.model_dump()
    for index, boundaries in enumerate(boundary_sets):
        variant = dict(regime)
        variant["boundaries"] = list(boundaries)
        yield Candidate(
            label=f"boundaries#{index}:{','.join(f'{value:g}' for value in boundaries)}",
            overrides={"regime": variant},
            dimension="threshold_search",
        )


def gate_threshold_candidates(
    config: AppConfig, *, rule: str, values: Sequence[float], bound: str = "min_value"
) -> Iterator[Candidate]:
    """TASK-083 — move one TQQQ bottom-confirmation threshold."""
    gate = config.strategy.tqqq_gate.model_dump()
    params = gate.get("rule_params") or {}
    if rule not in params:
        raise SearchError(f"tqqq_gate has no rule {rule!r}")
    for value in values:
        variant = dict(gate)
        rules = {name: dict(spec) for name, spec in params.items()}
        rules[rule] = {**rules[rule], bound: value}
        variant["rule_params"] = rules
        yield Candidate(
            label=f"gate.{rule}.{bound}={value:g}",
            overrides={"tqqq_gate": variant},
            dimension="threshold_search",
        )


def allocation_candidates(
    config: AppConfig, *, mappings: Sequence[dict[str, dict[Asset, float]]]
) -> Iterator[Candidate]:
    """TASK-084 — compare regime → weight tables."""
    constraints = config.strategy.allocation.constraints.model_dump()
    for index, mapping in enumerate(mappings):
        yield Candidate(
            label=f"allocation#{index}",
            overrides={"allocation": {"mappings": mapping, "constraints": constraints}},
            dimension="allocation_search",
        )


def transition_candidates(
    config: AppConfig,
    *,
    confirmation_days: Sequence[int] | None = None,
    hysteresis: Sequence[float] | None = None,
    minimum_duration_days: Sequence[int] | None = None,
) -> Iterator[Candidate]:
    """TASK-085 — the full confirmation / hysteresis / duration grid."""
    spec = config.strategy.transition
    research = spec.research_candidates
    confirmations = confirmation_days or [
        int(value) for value in research.get("confirmation_days", (spec.confirmation_days or 1,))
    ]
    hysteresis_values = hysteresis or [
        float(value) for value in research.get("hysteresis", (spec.hysteresis or 0.0,))
    ]
    durations = minimum_duration_days or [
        int(value)
        for value in research.get(
            "minimum_duration_days", (spec.minimum_duration_days or 1,)
        )
    ]

    for confirm, hyst, duration in itertools.product(
        confirmations, hysteresis_values, durations
    ):
        yield Candidate(
            label=f"transition(c={confirm},h={hyst:g},d={duration})",
            overrides={
                "transition": {
                    "confirmation_days": confirm,
                    "hysteresis": hyst,
                    "minimum_duration_days": duration,
                    "research_candidates": {
                        key: list(values) for key, values in research.items()
                    },
                }
            },
            dimension="transition_search",
        )


# ----------------------------------------------------------------- helpers


def _leverage_ladder(config: AppConfig) -> list[dict[Asset, float]]:
    mappings = config.strategy.allocation.mappings
    labels = config.strategy.regime.labels
    if not mappings or not labels:
        raise SearchError(
            "regime-count search needs a resolved allocation to interpolate from"
        )
    return [dict(mappings[label]) for label in labels]


def _interpolate_allocation(
    ladder: Sequence[dict[Asset, float]], index: int, count: int
) -> dict[Asset, float]:
    """Pick the ladder rung nearest this stage's position on the fear axis.

    Nearest-rung rather than blending weights: a blend can invent a portfolio
    nobody chose (TQQQ appearing in a mildly fearful regime), whereas picking an
    existing rung keeps every candidate made of allocations a human wrote.
    """
    if count == 1:
        return dict(ladder[0])
    position = index / (count - 1)
    rung = round(position * (len(ladder) - 1))
    return dict(ladder[rung])


def build_search(
    config: AppConfig,
    data: MarketData,
    *,
    guard: SplitGuard | None = None,
    objective: Objective | None = None,
    keep_runs: bool = False,
) -> GridSearch:
    """Convenience constructor that derives the guard from configuration."""
    from fear_ladder.research.splits import DatasetSplit

    resolved_guard = guard or SplitGuard(
        DatasetSplit.from_spec(config.strategy.dataset_split)
    )
    return GridSearch(
        config=config,
        data=data,
        guard=resolved_guard,
        objective=objective or Objective(),
        keep_runs=keep_runs,
    )


ObjectiveFn = Callable[[PerformanceMetrics], float]

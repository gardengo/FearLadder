"""Parameter sensitivity analysis (TASK-093).

``BACKTEST_SPEC.md`` 21::

    단일 최적점보다 안정적인 parameter region을 선호한다.
    RSI threshold 25 / 27 / 29 / 31 / 33 주변값에서 성능이 모두
    합리적인지 확인한다.

A peak that collapses one step to either side is almost always a fitting
artefact. So this module scores a neighbourhood and reports its *shape*: how far
the best result stands above its neighbours, and how much the result moves as
the parameter moves. A stable plateau is preferred to a taller spike, and the
numbers here are what let a human see which one they have.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean, pstdev

from pandas import DataFrame

from fear_ladder.research.search import (
    Candidate,
    GridSearch,
    SearchOutcome,
    SearchReport,
)
from fear_ladder.research.splits import Split

logger = logging.getLogger(__name__)

#: A best result standing more than this far above its neighbours, in relative
#: terms, looks like a spike rather than a plateau. It is a reporting threshold
#: for a human, not a rule anything acts on.
SPIKE_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class SensitivityReport:
    """How the objective behaves across a parameter neighbourhood."""

    parameter: str
    values: tuple[float, ...]
    objectives: tuple[float, ...]
    outcomes: tuple[SearchOutcome, ...]

    @property
    def best_index(self) -> int:
        return max(range(len(self.objectives)), key=lambda index: self.objectives[index])

    @property
    def best_value(self) -> float:
        return self.values[self.best_index]

    @property
    def spread(self) -> float:
        """Standard deviation of the objective across the neighbourhood."""
        return pstdev(self.objectives) if len(self.objectives) > 1 else 0.0

    @property
    def neighbour_gap(self) -> float:
        """How far the peak stands above its immediate neighbours, relatively.

        Near zero means a plateau. Large means the optimum depends on hitting
        one exact value, which is the fitting artefact §21 warns about.
        """
        index = self.best_index
        neighbours = [
            self.objectives[offset]
            for offset in (index - 1, index + 1)
            if 0 <= offset < len(self.objectives)
        ]
        if not neighbours:
            return 0.0
        peak = self.objectives[index]
        reference = mean(neighbours)
        scale = max(abs(peak), 1e-9)
        return (peak - reference) / scale

    @property
    def is_plateau(self) -> bool:
        return self.neighbour_gap <= SPIKE_RATIO

    def to_frame(self) -> DataFrame:
        return DataFrame(
            {
                self.parameter: self.values,
                "objective": self.objectives,
                "cagr": [outcome.metrics.cagr for outcome in self.outcomes],
                "max_drawdown": [outcome.metrics.max_drawdown for outcome in self.outcomes],
                "sharpe": [outcome.metrics.sharpe for outcome in self.outcomes],
                "turnover": [outcome.metrics.turnover for outcome in self.outcomes],
            }
        ).set_index(self.parameter)

    def summary(self) -> str:
        verdict = "plateau" if self.is_plateau else "SPIKE — likely overfit"
        return (
            f"{self.parameter}: best={self.best_value:g}, "
            f"neighbour gap {self.neighbour_gap:+.1%} ({verdict}), "
            f"objective spread {self.spread:.3f}"
        )


def analyse(
    search: GridSearch,
    candidates: Sequence[Candidate],
    values: Sequence[float],
    *,
    parameter: str,
    split: Split = Split.RESEARCH,
) -> SensitivityReport:
    """Score a parameter neighbourhood and describe its shape.

    ``candidates`` and ``values`` are positionally paired, so the caller keeps
    control of what "one step away" means for that parameter.
    """
    if len(candidates) != len(values):
        raise ValueError("candidates and values must be the same length")
    if len(values) < 3:
        raise ValueError("a neighbourhood needs at least three points to have a shape")

    report: SearchReport = search.run(candidates, split=split, dimension=f"sensitivity:{parameter}")
    by_label = {outcome.candidate.label: outcome for outcome in report.outcomes}

    kept_values: list[float] = []
    kept_outcomes: list[SearchOutcome] = []
    for candidate, value in zip(candidates, values, strict=True):
        outcome = by_label.get(candidate.label)
        if outcome is None:
            logger.warning("sensitivity point %s=%g could not be evaluated", parameter, value)
            continue
        kept_values.append(float(value))
        kept_outcomes.append(outcome)

    if len(kept_outcomes) < 3:
        raise ValueError(
            f"only {len(kept_outcomes)} of {len(values)} sensitivity points evaluated; "
            "the neighbourhood has no shape to report"
        )

    result = SensitivityReport(
        parameter=parameter,
        values=tuple(kept_values),
        objectives=tuple(outcome.objective for outcome in kept_outcomes),
        outcomes=tuple(kept_outcomes),
    )
    logger.info("%s", result.summary())
    return result

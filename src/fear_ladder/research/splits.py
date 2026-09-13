"""Dataset splits and the OOS guard (TASK-090).

``BACKTEST_SPEC.md`` 18 and 20 are the rules this module exists to make
mechanical rather than aspirational::

    운영 전략 확정 후 OOS 데이터로 규칙을 수정하면 안 된다.
    OOS 결과를 보고 수정하면 해당 기간은 더 이상 순수 OOS가 아니다.

Discipline alone does not survive a long project, so the optimiser physically
cannot see a protected window: :class:`SplitGuard` raises rather than returning
a result. Unsealing a window is possible but requires an explicit, logged act,
which is the point — it becomes a decision someone made, not an accident.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Self

from fear_ladder.config.schema import DatasetSplitSpec

logger = logging.getLogger(__name__)


class Split(StrEnum):
    """The three windows ``BACKTEST_SPEC.md`` 18 requires."""

    RESEARCH = "RESEARCH"
    VALIDATION = "VALIDATION"
    OOS = "OOS"


class OutOfSampleViolationError(RuntimeError):
    """Raised when optimisation would touch a protected window."""


@dataclass(frozen=True, slots=True)
class Window:
    """A closed date range."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"window start {self.start} is after end {self.end}")

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def overlaps(self, other: Window) -> bool:
        return self.start <= other.end and other.start <= self.end

    def __str__(self) -> str:
        return f"{self.start}..{self.end}"


@dataclass(frozen=True, slots=True)
class DatasetSplit:
    """Research / Validation / Out-of-sample windows."""

    research: Window
    validation: Window
    oos: Window

    def __post_init__(self) -> None:
        if self.research.overlaps(self.validation):
            raise ValueError("research and validation windows overlap")
        if self.validation.overlaps(self.oos):
            raise ValueError("validation and oos windows overlap")
        if self.research.overlaps(self.oos):
            raise ValueError("research and oos windows overlap")
        if not (self.research.end < self.validation.start < self.oos.start):
            raise ValueError("windows must run research -> validation -> oos in time order")

    @classmethod
    def from_spec(cls, spec: DatasetSplitSpec) -> Self:
        if not spec.is_resolved:
            raise ValueError(
                "dataset_split is unresolved (TASK-090); the OOS boundary must be "
                "declared before any optimisation runs, not after"
            )
        return cls(
            research=Window(spec.research_start, spec.research_end),  # type: ignore[arg-type]
            validation=Window(spec.validation_start, spec.validation_end),  # type: ignore[arg-type]
            oos=Window(spec.oos_start, spec.oos_end),  # type: ignore[arg-type]
        )

    def window(self, split: Split) -> Window:
        return {
            Split.RESEARCH: self.research,
            Split.VALIDATION: self.validation,
            Split.OOS: self.oos,
        }[split]

    def classify(self, day: date) -> Split | None:
        for split in Split:
            if self.window(split).contains(day):
                return split
        return None


@dataclass(slots=True)
class SplitGuard:
    """Refuses to let optimisation read a protected window.

    ``optimisable`` lists the windows a parameter search may score against.
    Everything else raises. :meth:`unseal` exists for the moment the strategy is
    frozen and the OOS test is deliberately run — and it logs loudly, because
    after that point the window is spent.
    """

    split: DatasetSplit
    optimisable: set[Split] = field(default_factory=lambda: {Split.RESEARCH})
    unsealed: set[Split] = field(default_factory=set)

    def is_optimisable(self, split: Split) -> bool:
        return split in self.optimisable or split in self.unsealed

    def check(self, split: Split, *, purpose: str = "optimisation") -> None:
        if self.is_optimisable(split):
            return
        raise OutOfSampleViolationError(
            f"{purpose} may not read the {split.value} window "
            f"({self.split.window(split)}). Optimising against it would destroy "
            "its value as an out-of-sample test (BACKTEST_SPEC.md 20)."
        )

    def check_range(self, start: date, end: date, *, purpose: str = "optimisation") -> None:
        """Reject a window that reaches into protected data."""
        requested = Window(start, end)
        for split in Split:
            if self.is_optimisable(split):
                continue
            if requested.overlaps(self.split.window(split)):
                raise OutOfSampleViolationError(
                    f"{purpose} window {requested} overlaps the protected "
                    f"{split.value} window ({self.split.window(split)})"
                )

    def unseal(self, split: Split, *, reason: str) -> None:
        """Deliberately open a protected window, once, with a stated reason."""
        if not reason:
            raise ValueError("unsealing a window requires a stated reason")
        logger.warning(
            "UNSEALING the %s window (%s): %s. From now on it is no longer a "
            "clean out-of-sample test.",
            split.value,
            self.split.window(split),
            reason,
        )
        self.unsealed.add(split)

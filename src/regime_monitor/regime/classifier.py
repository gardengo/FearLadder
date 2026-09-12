"""Regime classification (TASK-050).

The classifier is a pure function of the composite score and the configured
boundaries. Stage count (3 / 5 / 7 / 9) and the cut points are research
parameters: ``BACKTEST_SPEC.md`` 12 says more stages are not assumed to be
better, and ``CLAUDE_CODE_INITIAL_PROMPT.md`` 7 forbids fixing the numbers here.

Boundary convention, fixed once so the whole system agrees::

    labels:     [ L0 ][ L1 ][ L2 ][ L3 ]
    boundaries:     b0    b1    b2

    score < b0          -> L0   (most fear)
    b0 <= score < b1    -> L1
    ...
    score >= b_{n-2}    -> L_{n-1}   (most greed)

Lower-inclusive / upper-exclusive, with the last band closed, so every score in
[0, 100] lands in exactly one regime and a score exactly on a boundary always
belongs to the greedier side.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Self

import pandas as pd
from pandas import Series

from regime_monitor.config.schema import RegimeSpec
from regime_monitor.constants import SCORE_MAX, SCORE_MIN, UNKNOWN_REGIME


class RegimeError(ValueError):
    """Raised when a regime cannot be determined as configured."""


@dataclass(frozen=True, slots=True)
class RegimeScale:
    """An ordered fear-to-greed scale with its cut points."""

    labels: tuple[str, ...]
    boundaries: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.labels) < 2:
            raise RegimeError("a regime scale needs at least two labels")
        if len(self.boundaries) != len(self.labels) - 1:
            raise RegimeError(
                f"{len(self.labels)} labels need {len(self.labels) - 1} boundaries, "
                f"got {len(self.boundaries)}"
            )
        if list(self.boundaries) != sorted(self.boundaries):
            raise RegimeError("boundaries must be ascending")
        if len(set(self.boundaries)) != len(self.boundaries):
            raise RegimeError("boundaries must be distinct")
        if UNKNOWN_REGIME in self.labels:
            raise RegimeError(f"{UNKNOWN_REGIME} is reserved")
        for boundary in self.boundaries:
            if not SCORE_MIN < boundary < SCORE_MAX:
                raise RegimeError(
                    f"boundary {boundary} must lie strictly inside "
                    f"({SCORE_MIN}, {SCORE_MAX}); an edge band would be unreachable"
                )

    @classmethod
    def from_spec(cls, spec: RegimeSpec) -> Self:
        if not spec.is_resolved:
            raise RegimeError(
                "regime count / labels / boundaries are unresolved research "
                "parameters (TASK-082); classification is not possible yet"
            )
        assert spec.labels is not None and spec.boundaries is not None
        return cls(labels=tuple(spec.labels), boundaries=tuple(spec.boundaries))

    # -- lookups -----------------------------------------------------------
    @property
    def count(self) -> int:
        return len(self.labels)

    def index_of(self, label: str) -> int:
        try:
            return self.labels.index(label)
        except ValueError as exc:
            raise RegimeError(f"unknown regime label {label!r}") from exc

    def classify(self, score: float | None) -> str:
        """Map a score onto a regime label.

        ``None`` in, ``UNKNOWN`` out: a day without a score never gets a regime
        (``ARCHITECTURE.md`` 15).
        """
        if score is None or pd.isna(score):
            return UNKNOWN_REGIME
        if not SCORE_MIN <= score <= SCORE_MAX:
            raise RegimeError(f"score {score} is outside [{SCORE_MIN}, {SCORE_MAX}]")
        return self.labels[bisect_right(self.boundaries, score)]

    def classify_series(self, scores: Series) -> Series:
        return Series(
            [self.classify(None if pd.isna(value) else float(value)) for value in scores],
            index=scores.index,
            name="regime",
            dtype="object",
        )

    def band(self, label: str) -> tuple[float, float]:
        """Half-open score range ``[low, high)`` owned by ``label``."""
        index = self.index_of(label)
        low = SCORE_MIN if index == 0 else self.boundaries[index - 1]
        high = SCORE_MAX if index == self.count - 1 else self.boundaries[index]
        return (low, high)

    def is_more_fearful(self, left: str, right: str) -> bool:
        """Is ``left`` further toward fear than ``right``?"""
        return self.index_of(left) < self.index_of(right)

    @property
    def most_fearful(self) -> str:
        return self.labels[0]

    @property
    def most_greedy(self) -> str:
        return self.labels[-1]


@dataclass(frozen=True, slots=True)
class RegimeClassifier:
    """Thin wrapper that keeps the scale and its spec together."""

    scale: RegimeScale

    @classmethod
    def from_spec(cls, spec: RegimeSpec) -> Self:
        return cls(scale=RegimeScale.from_spec(spec))

    def classify(self, score: float | None) -> str:
        return self.scale.classify(score)

    def classify_series(self, scores: Series) -> Series:
        return self.scale.classify_series(scores)

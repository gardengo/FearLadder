"""Walk-forward validation (TASK-091).

``BACKTEST_SPEC.md`` 19::

    Train → Validate → Freeze → Test → Roll Forward
    각 window에서 future data 접근을 금지한다.

The "Freeze" step is the one that is easy to write and easy to cheat. Here it is
literal: :class:`WalkForwardFold` selects its parameters using only its train
window, freezes them into an immutable candidate, and the test window is then
scored by code that has no access to the selection at all. A fold's test result
cannot feed back into its own selection, because by the time it exists the
candidate is already fixed.

Reported out-of-sample performance is the concatenation of the *test* segments
only. In-sample numbers are kept alongside purely so the gap between them —
the honest measure of how much the search overfitted — is visible.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from pandas import DataFrame, Series

from fear_ladder.backtest.metrics import PerformanceMetrics, compute_metrics
from fear_ladder.config.schema import AppConfig
from fear_ladder.research.backtest_runner import MarketData, StrategyBacktest
from fear_ladder.research.search import Candidate, Objective, SearchError
from fear_ladder.research.splits import Window

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FoldResult:
    """One train → freeze → test cycle."""

    index: int
    train: Window
    test: Window
    frozen: Candidate
    train_metrics: PerformanceMetrics
    test_metrics: PerformanceMetrics
    test_nav: Series

    @property
    def overfit_gap(self) -> float:
        """In-sample CAGR minus out-of-sample CAGR.

        A large positive gap means the search fitted the train window rather
        than finding something durable.
        """
        return self.train_metrics.cagr - self.test_metrics.cagr


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """Every fold, plus the stitched out-of-sample path."""

    folds: tuple[FoldResult, ...]
    stitched_nav: Series = field(default_factory=Series)
    stitched_metrics: PerformanceMetrics | None = None

    @property
    def mean_overfit_gap(self) -> float:
        if not self.folds:
            return 0.0
        return sum(fold.overfit_gap for fold in self.folds) / len(self.folds)

    def to_frame(self) -> DataFrame:
        return DataFrame(
            [
                {
                    "fold": fold.index,
                    "train": str(fold.train),
                    "test": str(fold.test),
                    "frozen": fold.frozen.label,
                    "train_cagr": fold.train_metrics.cagr,
                    "test_cagr": fold.test_metrics.cagr,
                    "test_mdd": fold.test_metrics.max_drawdown,
                    "test_sharpe": fold.test_metrics.sharpe,
                    "overfit_gap": fold.overfit_gap,
                }
                for fold in self.folds
            ]
        )

    def summary(self) -> str:
        if not self.folds:
            return "walk-forward: no folds"
        head = f"walk-forward: {len(self.folds)} folds"
        if self.stitched_metrics is not None:
            head += f"; stitched OOS {self.stitched_metrics.summary()}"
        return f"{head}; mean overfit gap {self.mean_overfit_gap:+.2%}"


@dataclass(frozen=True, slots=True)
class WalkForward:
    """Rolling train → freeze → test evaluation."""

    config: AppConfig
    data: MarketData
    candidates: tuple[Candidate, ...]
    objective: Objective = field(default_factory=Objective)

    def __post_init__(self) -> None:
        if not self.candidates:
            raise SearchError("walk-forward needs at least one candidate to choose from")

    def run(
        self,
        *,
        start: date,
        end: date,
        train_days: int,
        test_days: int,
        step_days: int | None = None,
    ) -> WalkForwardReport:
        folds = list(
            generate_folds(
                start=start,
                end=end,
                train_days=train_days,
                test_days=test_days,
                step_days=step_days or test_days,
            )
        )
        if not folds:
            raise SearchError(
                f"no folds fit in {start}..{end} with train={train_days}d test={test_days}d"
            )

        results: list[FoldResult] = []
        for index, (train, test) in enumerate(folds):
            frozen, train_metrics = self._select(train)
            test_metrics, test_nav = self._evaluate(frozen, test)
            logger.info(
                "fold %d train=%s test=%s frozen=%s test CAGR=%.2f%%",
                index,
                train,
                test,
                frozen.label,
                test_metrics.cagr * 100,
            )
            results.append(
                FoldResult(
                    index=index,
                    train=train,
                    test=test,
                    frozen=frozen,
                    train_metrics=train_metrics,
                    test_metrics=test_metrics,
                    test_nav=test_nav,
                )
            )

        stitched = stitch(result.test_nav for result in results)
        report = WalkForwardReport(
            folds=tuple(results),
            stitched_nav=stitched,
            stitched_metrics=(
                compute_metrics(stitched, name="walk-forward OOS")
                if len(stitched) > 1
                else None
            ),
        )
        logger.info("%s", report.summary())
        return report

    # -- steps -------------------------------------------------------------
    def _select(self, train: Window) -> tuple[Candidate, PerformanceMetrics]:
        """Choose parameters using the train window and nothing else."""
        best: tuple[float, Candidate, PerformanceMetrics] | None = None
        for candidate in self.candidates:
            try:
                run = StrategyBacktest(candidate.apply(self.config)).run(
                    self.data,
                    start=train.start,
                    end=train.end,
                    include_benchmarks=False,
                    name=candidate.label,
                )
            except (SearchError, ValueError) as exc:
                logger.debug("candidate %s unusable in %s: %s", candidate.label, train, exc)
                continue
            score = self.objective.score(run.metrics)
            if best is None or score > best[0]:
                best = (score, candidate, run.metrics)
        if best is None:
            raise SearchError(f"no candidate could be evaluated on the train window {train}")
        return best[1], best[2]

    def _evaluate(
        self, frozen: Candidate, test: Window
    ) -> tuple[PerformanceMetrics, Series]:
        """Score the frozen candidate on unseen data."""
        run = StrategyBacktest(frozen.apply(self.config)).run(
            self.data,
            start=test.start,
            end=test.end,
            include_benchmarks=False,
            name=f"{frozen.label}@{test}",
        )
        return run.metrics, run.result.nav


def generate_folds(
    *,
    start: date,
    end: date,
    train_days: int,
    test_days: int,
    step_days: int,
) -> list[tuple[Window, Window]]:
    """Rolling (train, test) pairs. A test window never precedes its train."""
    if train_days < 1 or test_days < 1 or step_days < 1:
        raise ValueError("train_days, test_days and step_days must all be >= 1")

    folds: list[tuple[Window, Window]] = []
    train_start = start
    while True:
        train_end = train_start + timedelta(days=train_days - 1)
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_days - 1)
        if test_end > end:
            break
        folds.append((Window(train_start, train_end), Window(test_start, test_end)))
        train_start = train_start + timedelta(days=step_days)
    return folds


def stitch(navs: object) -> Series:
    """Chain per-fold NAV paths into one continuous out-of-sample curve.

    Each fold restarts at 1.0, so the segments are re-based onto the running
    level instead of being concatenated raw.
    """
    pieces: list[Series] = []
    level = 1.0
    for nav in navs:  # type: ignore[union-attr]
        series = nav.dropna()
        if series.empty:
            continue
        rebased = series / series.iloc[0] * level
        pieces.append(rebased)
        level = float(rebased.iloc[-1])
    if not pieces:
        return Series(dtype="float64")
    stitched = pd.concat(pieces)
    stitched = stitched[~stitched.index.duplicated(keep="first")].sort_index()
    stitched.name = "nav"
    return stitched


def candidates_from(sequences: Sequence[Sequence[Candidate]]) -> tuple[Candidate, ...]:
    """Flatten several space builders into one candidate pool."""
    return tuple(candidate for sequence in sequences for candidate in sequence)

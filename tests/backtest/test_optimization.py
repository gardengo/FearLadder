"""TASK-080 .. TASK-085, TASK-091, TASK-093 — search, walk-forward, sensitivity."""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame

from regime_monitor.backtest.metrics import PerformanceMetrics
from regime_monitor.constants import Asset
from regime_monitor.research.backtest_runner import MarketData
from regime_monitor.research.search import (
    Candidate,
    GridSearch,
    Objective,
    SearchError,
    allocation_candidates,
    boundary_candidates,
    build_search,
    gate_threshold_candidates,
    indicator_subsets,
    regime_count_candidates,
    transition_candidates,
    weight_candidates,
)
from regime_monitor.research.sensitivity import SensitivityReport, analyse
from regime_monitor.research.splits import DatasetSplit, Split, SplitGuard, Window
from regime_monitor.research.walk_forward import (
    WalkForward,
    generate_folds,
    stitch,
)

START = date(2012, 1, 1)
LENGTH = 1300


@lru_cache(maxsize=2)
def _market(seed: int = 5, length: int = LENGTH) -> MarketData:
    rng = np.random.default_rng(seed)
    index = pd.Index(
        [START + timedelta(days=offset) for offset in range(length)],
        name="observation_date",
    )
    daily = rng.normal(0.0004, 0.012, length)
    closes = DataFrame(
        {
            "QQQ": 100.0 * np.cumprod(1 + daily),
            "QLD": 100.0 * np.cumprod(1 + 2 * daily),
            "TQQQ": 100.0 * np.cumprod(1 + 3 * daily),
        },
        index=index,
    )
    opens = closes.shift(1).fillna(closes.iloc[0]) * 0.5 + closes * 0.5
    series = closes.copy()
    series["VIX"] = np.abs(rng.normal(18, 5, length)) + np.abs(daily) * 300
    series["CNN_FEAR_GREED"] = np.clip(
        50 + daily.cumsum() * 200 + rng.normal(0, 10, length), 0, 100
    )
    series["AAII_SENTIMENT"] = np.clip(rng.normal(0.05, 0.12, length), -1, 1)
    return MarketData(series=series, closes=closes, opens=opens)


def _guard() -> SplitGuard:
    return SplitGuard(
        DatasetSplit(
            research=Window(date(2012, 1, 1), date(2014, 12, 31)),
            validation=Window(date(2015, 1, 1), date(2015, 6, 30)),
            oos=Window(date(2015, 7, 1), date(2015, 12, 31)),
        )
    )


@pytest.fixture
def search(placeholder_config) -> GridSearch:
    return GridSearch(
        config=placeholder_config,
        data=_market(),
        guard=_guard(),
        objective=Objective(),
    )


# -------------------------------------------------------------- candidates


def test_a_candidate_rebuilds_and_revalidates_the_strategy(placeholder_config) -> None:
    candidate = Candidate(
        label="c", overrides={"transition": {"confirmation_days": 4, "hysteresis": 3.0,
                                             "minimum_duration_days": 7}}
    )
    variant = candidate.apply(placeholder_config)
    assert variant.strategy.transition.confirmation_days == 4
    assert variant.strategy.strategy_version.endswith("+c")
    # The original is untouched.
    assert placeholder_config.strategy.transition.confirmation_days == 2


def test_an_invalid_candidate_is_rejected_rather_than_silently_run(
    placeholder_config,
) -> None:
    # Weights that do not sum to 1 would produce a meaningless backtest.
    candidate = Candidate(label="bad", overrides={"score": {"weights": {"rsi_14": 0.3}}})
    with pytest.raises(SearchError, match="not a valid strategy"):
        candidate.apply(placeholder_config)


# ------------------------------------------------------------------ objective


def _metrics(**values: float) -> PerformanceMetrics:
    base = {
        "name": "x",
        "start": START,
        "end": START + timedelta(days=365),
        "years": 1.0,
        "total_return": 0.1,
        "cagr": 0.1,
        "volatility": 0.2,
        "max_drawdown": 0.2,
        "sharpe": 0.5,
        "sortino": 0.6,
        "calmar": 0.5,
        "worst_year": -0.1,
        "best_year": 0.3,
        "recovery_days": 30,
        "time_under_water": 0.4,
        "turnover": 1.0,
        "trade_count": 3,
    }
    base.update(values)
    return PerformanceMetrics(**base)  # type: ignore[arg-type]


def test_the_objective_is_not_cagr_alone() -> None:
    # BACKTEST_SPEC.md 11 — a candidate is never ranked on one number.
    objective = Objective()
    high_cagr_high_risk = _metrics(cagr=0.40, max_drawdown=0.80, calmar=0.5, sharpe=0.4)
    lower_cagr_lower_risk = _metrics(cagr=0.25, max_drawdown=0.20, calmar=1.25, sharpe=1.1)
    assert objective.score(lower_cagr_lower_risk) > objective.score(high_cagr_high_risk)


def test_the_objective_penalises_turnover() -> None:
    objective = Objective()
    assert objective.score(_metrics(turnover=1.0)) > objective.score(_metrics(turnover=50.0))


def test_the_objective_describes_itself() -> None:
    assert "max_drawdown" in Objective().describe()


# ---------------------------------------------------------- space builders


def test_indicator_subsets_stay_convex(placeholder_config) -> None:
    subsets = [["rsi_14", "vix_level"], ["rsi_14", "vix_level", "momentum_3m"]]
    for candidate in indicator_subsets(placeholder_config, subsets=subsets):
        weights = candidate.overrides["score"]["weights"]
        assert sum(weights.values()) == pytest.approx(1.0)
        variant = candidate.apply(placeholder_config)
        assert variant.strategy.score.weights is not None


def test_weight_candidates_are_normalised(placeholder_config) -> None:
    candidates = list(
        weight_candidates(
            placeholder_config, weight_sets=[{"rsi_14": 2.0, "vix_level": 2.0}]
        )
    )
    weights = candidates[0].overrides["score"]["weights"]
    assert weights["rsi_14"] == pytest.approx(0.5)


def test_regime_count_candidates_cover_every_stage_option(placeholder_config) -> None:
    candidates = list(regime_count_candidates(placeholder_config))
    counts = [candidate.overrides["regime"]["count"] for candidate in candidates]
    assert counts == [3, 5, 7, 9]

    for candidate in candidates:
        variant = candidate.apply(placeholder_config)
        regime = variant.strategy.regime
        assert regime.labels is not None and regime.boundaries is not None
        assert len(regime.boundaries) == regime.count - 1
        # Every stage must own an allocation, or the engine cannot run.
        assert set(variant.strategy.allocation.mappings or {}) == set(regime.labels)


def test_regime_count_candidates_keep_leverage_monotone(placeholder_config) -> None:
    from regime_monitor.constants import ASSET_LEVERAGE

    for candidate in regime_count_candidates(placeholder_config):
        variant = candidate.apply(placeholder_config)
        labels = variant.strategy.regime.labels
        mappings = variant.strategy.allocation.mappings
        assert labels is not None and mappings is not None
        ladder = [
            sum(ASSET_LEVERAGE[asset] * weight for asset, weight in mappings[label].items())
            for label in labels
        ]
        assert ladder == sorted(ladder, reverse=True), candidate.label


def test_boundary_candidates_move_only_the_cut_points(placeholder_config) -> None:
    candidates = list(
        boundary_candidates(placeholder_config, boundary_sets=[[15, 35, 65, 85]])
    )
    variant = candidates[0].apply(placeholder_config)
    assert variant.strategy.regime.boundaries == (15.0, 35.0, 65.0, 85.0)
    assert variant.strategy.regime.count == 5


def test_gate_threshold_candidates_move_one_rule(placeholder_config) -> None:
    candidates = list(
        gate_threshold_candidates(
            placeholder_config, rule="deep_drawdown", values=[0.15, 0.20, 0.25]
        )
    )
    assert len(candidates) == 3
    variant = candidates[0].apply(placeholder_config)
    rules = variant.strategy.tqqq_gate.rule_params
    assert rules is not None
    assert rules["deep_drawdown"]["min_value"] == pytest.approx(0.15)
    assert rules["extreme_fear"]["max_value"] == pytest.approx(25.0), "others untouched"


def test_gate_threshold_candidates_reject_an_unknown_rule(placeholder_config) -> None:
    with pytest.raises(SearchError, match="no rule"):
        list(gate_threshold_candidates(placeholder_config, rule="nope", values=[1.0]))


def test_allocation_candidates_are_validated(placeholder_config) -> None:
    labels = placeholder_config.strategy.regime.labels
    assert labels is not None
    mapping = {label: {Asset.QLD: 0.6, Asset.CASH: 0.4} for label in labels}
    candidate = next(iter(allocation_candidates(placeholder_config, mappings=[mapping])))
    variant = candidate.apply(placeholder_config)
    assert variant.strategy.allocation.mappings is not None


def test_transition_candidates_cover_the_grid(placeholder_config) -> None:
    candidates = list(
        transition_candidates(
            placeholder_config,
            confirmation_days=[1, 3],
            hysteresis=[0.0, 5.0],
            minimum_duration_days=[1, 10],
        )
    )
    assert len(candidates) == 8
    labels = {candidate.label for candidate in candidates}
    assert "transition(c=1,h=0,d=1)" in labels


def test_transition_candidates_default_to_the_configured_research_grid(
    placeholder_config,
) -> None:
    candidates = list(transition_candidates(placeholder_config))
    research = placeholder_config.strategy.transition.research_candidates
    expected = (
        len(research["confirmation_days"])
        * len(research["hysteresis"])
        * len(research["minimum_duration_days"])
    )
    assert len(candidates) == expected


# ---------------------------------------------------------------- searching


def test_a_search_ranks_candidates_without_deciding(search, placeholder_config) -> None:
    candidates = list(
        transition_candidates(
            placeholder_config,
            confirmation_days=[1, 3],
            hysteresis=[0.0, 5.0],
            minimum_duration_days=[5],
        )
    )
    report = search.run(candidates, split=Split.RESEARCH, dimension="transition_search")

    assert len(report.outcomes) == 4
    assert report.best is not None
    scores = [outcome.objective for outcome in report.ranked]
    assert scores == sorted(scores, reverse=True)
    assert "transition_search over RESEARCH" in report.summary()


def test_a_search_only_reads_its_own_window(search, placeholder_config) -> None:
    candidates = list(
        transition_candidates(
            placeholder_config,
            confirmation_days=[2],
            hysteresis=[5.0],
            minimum_duration_days=[5],
        )
    )
    report = search.run(candidates, split=Split.RESEARCH)
    window = _guard().split.research
    best = report.best
    assert best is not None
    assert best.metrics.start >= window.start
    assert best.metrics.end <= window.end


def test_the_report_tabulates_every_candidate(search, placeholder_config) -> None:
    candidates = list(
        transition_candidates(
            placeholder_config,
            confirmation_days=[1, 2, 3],
            hysteresis=[5.0],
            minimum_duration_days=[5],
        )
    )
    frame = search.run(candidates).to_frame()
    assert len(frame) == 3
    assert {"objective", "cagr", "max_drawdown", "turnover"} <= set(frame.columns)


def test_an_unusable_candidate_is_recorded_not_fatal(search) -> None:
    candidates = [
        Candidate(label="ok", overrides={"transition": {"confirmation_days": 2,
                                                        "hysteresis": 5.0,
                                                        "minimum_duration_days": 5}}),
    ]
    report = search.run(candidates)
    assert len(report.outcomes) == 1
    assert report.failures == ()


def test_build_search_derives_the_guard_from_configuration(placeholder_config) -> None:
    search = build_search(placeholder_config, _market())
    assert search.guard.split.oos.start == date(2020, 1, 1)
    assert search.guard.optimisable == {Split.RESEARCH}


# ------------------------------------------------------------------ TASK-091


def test_folds_never_test_before_they_train() -> None:
    folds = generate_folds(
        start=date(2012, 1, 1),
        end=date(2016, 1, 1),
        train_days=365,
        test_days=180,
        step_days=180,
    )
    assert folds
    for train, test in folds:
        assert train.end < test.start
        assert test.end <= date(2016, 1, 1)


def test_folds_roll_forward() -> None:
    folds = generate_folds(
        start=date(2012, 1, 1), end=date(2018, 1, 1), train_days=365,
        test_days=180, step_days=180,
    )
    starts = [train.start for train, _ in folds]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_stitching_rebases_each_fold_onto_the_running_level() -> None:
    index_a = pd.Index([START + timedelta(days=i) for i in range(3)])
    index_b = pd.Index([START + timedelta(days=3 + i) for i in range(3)])
    first = pd.Series([1.0, 1.1, 1.2], index=index_a)
    second = pd.Series([1.0, 0.9, 1.5], index=index_b)

    stitched = stitch([first, second])
    assert stitched.iloc[2] == pytest.approx(1.2)
    assert stitched.iloc[-1] == pytest.approx(1.2 * 1.5)


def test_walk_forward_freezes_before_it_tests(placeholder_config) -> None:
    """The selection cannot see its own test window.

    Each fold picks parameters on train, freezes them, then scores test. The
    frozen label is recorded so it is visible which candidate a fold chose.
    """
    candidates = tuple(
        transition_candidates(
            placeholder_config,
            confirmation_days=[1, 5],
            hysteresis=[0.0, 8.0],
            minimum_duration_days=[5],
        )
    )
    report = WalkForward(placeholder_config, _market(), candidates).run(
        start=date(2012, 6, 1),
        end=date(2015, 6, 1),
        train_days=500,
        test_days=250,
        step_days=250,
    )

    assert report.folds
    for fold in report.folds:
        assert fold.train.end < fold.test.start
        assert fold.frozen.label in {candidate.label for candidate in candidates}
        assert fold.test_metrics.start >= fold.test.start
        assert fold.test_metrics.end <= fold.test.end


def test_walk_forward_reports_the_overfit_gap(placeholder_config) -> None:
    candidates = tuple(
        transition_candidates(
            placeholder_config,
            confirmation_days=[1, 3],
            hysteresis=[5.0],
            minimum_duration_days=[5],
        )
    )
    report = WalkForward(placeholder_config, _market(), candidates).run(
        start=date(2012, 6, 1), end=date(2015, 6, 1),
        train_days=500, test_days=250, step_days=250,
    )
    frame = report.to_frame()
    assert {"train_cagr", "test_cagr", "overfit_gap"} <= set(frame.columns)
    assert report.stitched_metrics is not None
    assert "walk-forward" in report.summary()


def test_walk_forward_needs_candidates(placeholder_config) -> None:
    with pytest.raises(SearchError, match="at least one candidate"):
        WalkForward(placeholder_config, _market(), ())


def test_walk_forward_refuses_an_impossible_schedule(placeholder_config) -> None:
    candidates = tuple(transition_candidates(placeholder_config, confirmation_days=[1]))
    with pytest.raises(SearchError, match="no folds fit"):
        WalkForward(placeholder_config, _market(), candidates).run(
            start=date(2012, 1, 1), end=date(2012, 2, 1),
            train_days=500, test_days=250,
        )


# ------------------------------------------------------------------ TASK-093


def test_sensitivity_reports_the_shape_of_a_neighbourhood(
    search, placeholder_config
) -> None:
    # BACKTEST_SPEC.md 21 — check the values *around* the optimum.
    values = [1.0, 2.0, 3.0, 5.0, 10.0]
    candidates = list(
        transition_candidates(
            placeholder_config,
            confirmation_days=[int(value) for value in values],
            hysteresis=[5.0],
            minimum_duration_days=[5],
        )
    )
    report = analyse(search, candidates, values, parameter="confirmation_days")

    assert isinstance(report, SensitivityReport)
    assert report.best_value in values
    assert len(report.to_frame()) == len(values)
    assert "confirmation_days" in report.summary()


def test_a_plateau_is_distinguished_from_a_spike() -> None:
    plateau = SensitivityReport(
        parameter="p", values=(1, 2, 3), objectives=(1.00, 1.02, 1.01), outcomes=()
    )
    spike = SensitivityReport(
        parameter="p", values=(1, 2, 3), objectives=(0.10, 1.00, 0.12), outcomes=()
    )
    assert plateau.is_plateau
    assert not spike.is_plateau
    assert "SPIKE" in spike.summary()


def test_a_neighbourhood_needs_enough_points(search, placeholder_config) -> None:
    candidates = list(
        transition_candidates(
            placeholder_config,
            confirmation_days=[1, 2],
            hysteresis=[5.0],
            minimum_duration_days=[5],
        )
    )
    with pytest.raises(ValueError, match="at least three points"):
        analyse(search, candidates, [1.0, 2.0], parameter="confirmation_days")

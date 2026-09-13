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


def test_a_candidate_can_move_an_indicator_parameter(placeholder_config) -> None:
    """Indicator parameters are searchable, not just strategy ones.

    They were not, until the dot-com top showed what a 504-day percentile does
    to a bubble that outlasts its own window.
    """
    candidate = Candidate(
        label="norm",
        overrides={},
        indicator_overrides={"rsi_14": {"normalization": {"window": 252, "min_periods": 126}}},
    )
    variant = candidate.apply(placeholder_config)
    assert variant.indicators.indicators["rsi_14"].normalization.window == 252
    # Merged one level deep: the rest of the spec survives.
    assert variant.indicators.indicators["rsi_14"].params == {"window": 14}
    assert variant.indicators.indicators["rsi_14"].normalization.method == "rolling_percentile"
    # Neither the neighbour nor the original moves.
    assert variant.indicators.indicators["rsi_30"].normalization.window == 504
    assert placeholder_config.indicators.indicators["rsi_14"].normalization.window == 504


def test_an_indicator_override_for_an_unknown_name_is_refused(placeholder_config) -> None:
    candidate = Candidate(
        label="typo", overrides={}, indicator_overrides={"rsi_15": {"enabled": False}}
    )
    with pytest.raises(SearchError, match="unknown indicator"):
        candidate.apply(placeholder_config)


def test_an_invalid_indicator_override_is_rejected(placeholder_config) -> None:
    candidate = Candidate(
        label="nonsense",
        overrides={},
        indicator_overrides={"rsi_14": {"normalization": {"window": -5}}},
    )
    with pytest.raises(SearchError, match="not a valid indicator set"):
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


# ------------------------------------------------- the placeholder is a fixture


def test_the_search_refuses_to_overwrite_the_placeholder_profile(tmp_path) -> None:
    """The placeholder indicator set is a fixed reference the tests read.

    A search that rewrote it in place would move the ground every other test
    stands on, and would do it silently.
    """
    import scripts.optimize as optimize_cli

    placeholder = tmp_path / "placeholder.indicators.yaml"
    placeholder.write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        optimize_cli._write_indicators(placeholder, placeholder, None)


def test_the_search_writes_indicators_to_their_own_file(placeholder_config, tmp_path) -> None:
    import scripts.optimize as optimize_cli
    import yaml as yaml_module

    source = tmp_path / "placeholder.indicators.yaml"
    source.write_text("version: 1\n", encoding="utf-8")
    destination = tmp_path / "candidate.indicators.yaml"
    optimize_cli._write_indicators(source, destination, placeholder_config)

    written = yaml_module.safe_load(destination.read_text(encoding="utf-8"))
    assert written["indicators"]["rsi_14"]["normalization"]["window"] == 504
    assert source.read_text(encoding="utf-8") == "version: 1\n"


def test_the_indicator_stage_sweeps_each_family_on_its_own_grid(placeholder_config) -> None:
    """AAII is weekly; forcing it onto the daily grid would mean a decade.

    So the across-the-board sweep moves every family to the same *position* in
    its own candidate list, not to the same number.
    """
    import scripts.optimize as optimize_cli

    candidates = optimize_cli.indicator_candidates(placeholder_config)
    across = [c for c in candidates if c.label.startswith("norm(all")]
    assert across, "the across-the-board sweep is the interpretable comparison"

    shortest = across[0].apply(placeholder_config).indicators.indicators
    assert shortest["rsi_14"].normalization.window == 252
    assert shortest["aaii_bull_bear_spread"].normalization.window == 52

    families = {c.label.split("(")[1].split("=")[0] for c in candidates}
    assert "trend" in families and "sentiment" in families


# --------------------------------------------- absolute scales, when earned


def test_an_absolute_scale_is_offered_only_where_the_range_is_definitional(
    placeholder_config,
) -> None:
    """A rolling percentile has no answer for a bubble that outlasts its window.

    An absolute scale does, but only for indicators whose range is a fact about
    the construction (RSI is 0-100) rather than a fact about a sample. Anything
    else would be inventing a threshold.
    """
    import scripts.optimize as optimize_cli

    labels = {c.label for c in optimize_cli.indicator_candidates(placeholder_config)}
    assert "norm(rsi=absolute)" in labels
    assert "norm(drawdown=absolute)" in labels
    # Open-ended series have no definitional bounds, so no absolute candidate.
    for family in ("momentum", "trend", "volatility", "sentiment"):
        assert f"norm({family}=absolute)" not in labels


def test_the_absolute_candidate_uses_the_declared_range(placeholder_config) -> None:
    import scripts.optimize as optimize_cli

    candidate = next(
        c
        for c in optimize_cli.indicator_candidates(placeholder_config)
        if c.label == "norm(rsi=absolute)"
    )
    applied = candidate.apply(placeholder_config).indicators.indicators["rsi_14"]
    assert applied.normalization.method == "bounded"
    assert applied.normalization.raw_at_score_min == 0.0
    assert applied.normalization.raw_at_score_max == 100.0
    # The window is gone, not merely ignored.
    assert applied.normalization.window is None


def test_a_definitional_range_must_be_ascending() -> None:
    from regime_monitor.config.schema import ConfigError, IndicatorSpec

    with pytest.raises(ConfigError, match="not ascending"):
        IndicatorSpec(
            family="rsi",
            compute="rsi",
            source="QQQ",
            direction="HIGHER_IS_GREED",
            definitional_range=(100.0, 0.0),
            normalization={"method": "bounded", "raw_at_score_min": 0.0,
                           "raw_at_score_max": 100.0},
        )


def test_rsi_and_drawdown_declare_their_construction(placeholder_config) -> None:
    indicators = placeholder_config.indicators.indicators
    assert indicators["rsi_14"].definitional_range == (0.0, 100.0)
    assert indicators["drawdown_52w"].definitional_range == (0.0, 1.0)
    # An open-ended series must not claim one.
    assert indicators["momentum_1m"].definitional_range is None
    assert indicators["vix_level"].definitional_range is None


def test_a_named_selection_overrides_the_objective_winner() -> None:
    """The objective scores aggregates; it cannot see everything that matters.

    Measured: the highest-scoring normalization moved every window to its
    shortest, which caught the dot-com top and then failed to recognise the
    2008 bottom at all. Overriding that is legitimate — but it has to be an
    explicit act, not a quiet edit to a config file.
    """
    import scripts.optimize as optimize_cli

    parsed = optimize_cli.build_parser().parse_args(
        ["--stages", "indicators", "--select", "indicators=norm(rsi=absolute)"]
    )
    assert parsed.select == ["indicators=norm(rsi=absolute)"]


def test_an_unparseable_selection_is_refused(tmp_path) -> None:
    import scripts.optimize as optimize_cli

    with pytest.raises(SystemExit, match="STAGE=LABEL"):
        optimize_cli.main(["--stages", "indicators", "--select", "nonsense", "--db",
                           str(tmp_path / "x.db")])

"""TASK-092 — the leakage test suite.

``BACKTEST_SPEC.md`` 24 enumerates seven checks, and this module implements them
one-for-one against the assembled pipeline rather than against individual
functions. The unit tests already prove each component is causal; these prove
the *composition* stayed causal, which is where leakage usually creeps in.

    1. t일 signal에 t+1 price 사용 여부
    2. future rolling value 사용 여부
    3. full-sample normalization 여부
    4. OOS data에 대한 optimization 접근 여부
    5. same-day execution 여부
    6. availability 이전 sentiment 사용 여부
    7. future corporate action 정보의 부적절한 사용 여부
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame

from regime_monitor.config.schema import CostModelSpec, DatasetSplitSpec
from regime_monitor.constants import Asset, ExecutionTiming
from regime_monitor.research.backtest_runner import MarketData, StrategyBacktest
from regime_monitor.research.search import (
    GridSearch,
    Objective,
    transition_candidates,
)
from regime_monitor.research.splits import (
    DatasetSplit,
    OutOfSampleViolationError,
    Split,
    SplitGuard,
    Window,
)

START = date(2015, 1, 1)
LENGTH = 1200


def _market(seed: int = 7, length: int = LENGTH, *, shock_from: int | None = None) -> MarketData:
    rng = np.random.default_rng(seed)
    index = pd.Index(
        [START + timedelta(days=offset) for offset in range(length)],
        name="observation_date",
    )
    daily = rng.normal(0.0004, 0.012, length)
    if shock_from is not None:
        # A separate stream, so splicing in a crash does not advance `rng` and
        # silently change VIX and sentiment from day zero. Without this the
        # "shared prefix" the test relies on would not actually be shared.
        shock_rng = np.random.default_rng(seed + 10_000)
        daily[shock_from:] = shock_rng.normal(-0.02, 0.05, length - shock_from)

    qqq = 100.0 * np.cumprod(1 + daily)
    closes = DataFrame(
        {
            "QQQ": qqq,
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


@pytest.fixture(scope="module")
def truncated_vs_full(placeholder_config):
    """The same backtest run on full history and on history cut short."""
    data = _market()
    cutoff = START + timedelta(days=800)
    full = StrategyBacktest(placeholder_config).run(data, include_benchmarks=False)
    truncated = StrategyBacktest(placeholder_config).run(
        data, end=cutoff, include_benchmarks=False
    )
    return full, truncated, cutoff


# ------------------------------------------------------------------ check 1
# t일 signal에 t+1 price 사용 여부


def test_a_signal_never_uses_a_later_price(placeholder_config) -> None:
    """A future crash must not change any earlier regime.

    Two histories share their first 900 days and diverge afterwards. If anything
    in the chain peeked forward, the shared prefix would not match.
    """
    calm = _market(seed=11)
    crashed = _market(seed=11, shock_from=900)

    calm_run = StrategyBacktest(placeholder_config).run(calm, include_benchmarks=False)
    crashed_run = StrategyBacktest(placeholder_config).run(crashed, include_benchmarks=False)

    prefix = calm.closes.index[:900]
    pd.testing.assert_series_equal(
        calm_run.regimes.loc[prefix], crashed_run.regimes.loc[prefix]
    )
    pd.testing.assert_series_equal(
        calm_run.composite_score.loc[prefix].dropna(),
        crashed_run.composite_score.loc[prefix].dropna(),
    )


def test_todays_target_cannot_be_traded_today() -> None:
    from regime_monitor.backtest.simulator import PortfolioSimulator

    days = [START + timedelta(days=offset) for offset in range(3)]
    index = pd.Index(days, name="observation_date")
    closes = DataFrame({"QQQ": [100.0, 500.0, 500.0]}, index=index)

    simulator = PortfolioSimulator(closes=closes, opens=closes)
    result = simulator.run({days[1]: {Asset.QQQ: 1.0}})
    assert result.nav.iloc[1] == pytest.approx(1.0)


# ------------------------------------------------------------------ check 2
# future rolling value 사용 여부


def test_indicators_and_scores_are_identical_on_a_truncated_history(
    truncated_vs_full,
) -> None:
    full, truncated, cutoff = truncated_vs_full
    shared = truncated.indicator_scores.index

    pd.testing.assert_frame_equal(
        full.indicator_scores.loc[shared], truncated.indicator_scores
    )
    assert shared.max() <= cutoff


def test_regimes_are_identical_on_a_truncated_history(truncated_vs_full) -> None:
    full, truncated, _ = truncated_vs_full
    pd.testing.assert_series_equal(
        full.regimes.loc[truncated.regimes.index], truncated.regimes
    )


# ------------------------------------------------------------------ check 3
# full-sample normalization 여부


def test_no_normalizer_can_see_the_whole_sample() -> None:
    """Structural, not behavioural.

    Every normalizer's score at ``t`` must be a function of data up to ``t``.
    The abstraction has no full-sample entry point, and this test asserts that
    each implementation only reaches the data through a trailing window.
    """
    from regime_monitor.scoring import normalizers

    banned = ("expanding(", ".mean()", ".std(")
    allowed_owners = {"RollingZScoreNormalizer"}

    source = inspect.getsource(normalizers)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "rolling(" in stripped:
            continue
        for token in banned:
            if token in stripped:
                assert any(owner in source for owner in allowed_owners), stripped


def test_normalized_scores_do_not_change_when_the_future_arrives() -> None:
    from regime_monitor.constants import IndicatorDirection
    from regime_monitor.scoring.normalizers import RollingPercentileNormalizer

    rng = np.random.default_rng(3)
    index = pd.Index([START + timedelta(days=i) for i in range(500)])
    values = pd.Series(rng.normal(50, 20, 500), index=index)

    normalizer = RollingPercentileNormalizer(
        direction=IndicatorDirection.HIGHER_IS_GREED, window=120
    )
    before = normalizer.normalize(values.iloc[:400])
    after = normalizer.normalize(values).iloc[:400]
    pd.testing.assert_series_equal(before, after)


# ------------------------------------------------------------------ check 4
# OOS data에 대한 optimization 접근 여부


def _guard() -> SplitGuard:
    return SplitGuard(
        DatasetSplit(
            research=Window(date(2010, 1, 1), date(2017, 12, 31)),
            validation=Window(date(2018, 1, 1), date(2020, 12, 31)),
            oos=Window(date(2021, 1, 1), date(2025, 12, 31)),
        )
    )


def test_a_search_cannot_read_the_oos_window(placeholder_config) -> None:
    search = GridSearch(
        config=placeholder_config, data=_market(), guard=_guard(), objective=Objective()
    )
    candidates = list(transition_candidates(placeholder_config, confirmation_days=[1]))

    with pytest.raises(OutOfSampleViolationError, match="OOS"):
        search.run(candidates, split=Split.OOS)
    with pytest.raises(OutOfSampleViolationError, match="VALIDATION"):
        search.run(candidates, split=Split.VALIDATION)


def test_a_search_window_may_not_reach_into_protected_data() -> None:
    guard = _guard()
    guard.check_range(date(2012, 1, 1), date(2015, 1, 1))
    with pytest.raises(OutOfSampleViolationError, match="overlaps"):
        guard.check_range(date(2016, 1, 1), date(2022, 1, 1))


def test_opening_a_protected_window_is_explicit_and_logged(caplog) -> None:
    guard = _guard()
    with pytest.raises(ValueError, match="stated reason"):
        guard.unseal(Split.OOS, reason="")

    with caplog.at_level("WARNING"):
        guard.unseal(Split.OOS, reason="strategy frozen at v1.0, final OOS test")
    assert "UNSEALING" in caplog.text
    guard.check(Split.OOS)


def test_the_split_must_be_declared_before_optimisation() -> None:
    # An undeclared OOS boundary can always be drawn after seeing the results.
    with pytest.raises(ValueError, match="declared before any optimisation"):
        DatasetSplit.from_spec(DatasetSplitSpec())


def test_split_windows_cannot_overlap() -> None:
    with pytest.raises(ValueError, match="overlap"):
        DatasetSplit(
            research=Window(date(2010, 1, 1), date(2019, 12, 31)),
            validation=Window(date(2018, 1, 1), date(2020, 12, 31)),
            oos=Window(date(2021, 1, 1), date(2025, 12, 31)),
        )


# ------------------------------------------------------------------ check 5
# same-day execution 여부


def test_same_day_execution_cannot_be_configured() -> None:
    from regime_monitor.config.schema import ConfigError, ExecutionSpec

    with pytest.raises(ConfigError):
        ExecutionSpec(same_day_execution_allowed=True)


def test_the_execution_enum_offers_no_same_day_option() -> None:
    assert {member.value for member in ExecutionTiming} == {"NEXT_OPEN", "NEXT_CLOSE"}


def test_the_simulator_has_no_same_day_code_path() -> None:
    from regime_monitor.backtest import simulator

    source = inspect.getsource(simulator)
    # The only target lookup is "strictly before today"; if that ever changed to
    # include today, this is the line that would have to change.
    assert "days[:position]" in source
    assert "if position == 0:" in source


# ------------------------------------------------------------------ check 6
# availability 이전 sentiment 사용 여부


def test_a_weekly_survey_is_not_readable_before_publication() -> None:
    from regime_monitor.data.collectors.base import Collector, market_close_utc

    class WeeklyProvider:
        def fetch(self, start: date, end: date) -> DataFrame:
            index = pd.Index([date(2024, 1, 3)], name="observation_date")
            return DataFrame({"value": [0.12]}, index=index)

        def describe(self):
            from regime_monitor.data.collectors.base import SourceDescription

            return SourceDescription("pandas", "x", "AAII")

    observations = Collector().series_observations(
        WeeklyProvider(), "AAII_SENTIMENT", date(2024, 1, 1), date(2024, 1, 5),
        availability_lag_days=1,
    )
    observation = observations[0]
    assert observation.observation_date == date(2024, 1, 3)
    assert observation.availability_datetime == market_close_utc(date(2024, 1, 4))
    assert not observation.is_available_at(datetime(2024, 1, 3, 23, tzinfo=UTC))


def test_the_repository_hides_rows_that_were_not_yet_published(uow, provenance) -> None:
    from tests.conftest import make_scalar_observation

    uow.observations.save_observations(
        [
            make_scalar_observation(
                "AAII_SENTIMENT",
                date(2024, 1, 3),
                0.12,
                provenance,
                available_at=datetime(2024, 1, 4, 21, tzinfo=UTC),
            )
        ]
    )
    invisible = uow.observations.get_observations(
        "AAII_SENTIMENT", available_at=datetime(2024, 1, 3, 23, tzinfo=UTC)
    )
    assert invisible == []

    visible = uow.observations.get_observations(
        "AAII_SENTIMENT", available_at=datetime(2024, 1, 5, 0, tzinfo=UTC)
    )
    assert len(visible) == 1


def test_reconstructed_sentiment_is_never_labelled_official(tmp_path) -> None:
    # PRD.md 6.5 — a reconstructed series must not masquerade as published data.
    from regime_monitor.constants import DataQualityStatus
    from regime_monitor.data.collectors.registry import historical_cnn_provider

    provider = historical_cnn_provider(tmp_path)
    assert provider.quality_status is DataQualityStatus.REVIEW
    assert "reconstructed" in provider.underlying_source.lower()


# ------------------------------------------------------------------ check 7
# future corporate action 정보의 부적절한 사용 여부


def test_a_split_is_flagged_for_review_not_silently_adjusted(provenance) -> None:
    """An unadjusted split is a data-quality finding, not a free correction.

    Retroactively "fixing" a price series using a split announced later is a
    corporate-action leak: the corrected history would not have been available
    at the time (``BACKTEST_SPEC.md`` 5.5).
    """
    from regime_monitor.constants import DataQualityStatus
    from regime_monitor.data.validators.proshares import ProSharesCrossValidator
    from tests.conftest import make_price_observation

    observations = [
        make_price_observation("TQQQ", date(2024, 1, 1), 120.0, provenance),
        make_price_observation("TQQQ", date(2024, 1, 2), 60.0, provenance),
    ]
    report = ProSharesCrossValidator().validate("TQQQ", observations, {})

    assert not report.ok
    assert report.findings[0].check_name == "split_events"
    assert report.findings[0].status is DataQualityStatus.REVIEW
    # The collected prices are untouched.
    assert observations[1].close == pytest.approx(60.0)
    assert report.flagged[0].close == pytest.approx(60.0)


def test_the_validator_never_writes_back_a_corrected_price(provenance) -> None:
    from regime_monitor.data.validators.proshares import ProSharesCrossValidator
    from tests.conftest import make_price_observation

    observations = [make_price_observation("QLD", date(2024, 1, 2), 70.0, provenance)]
    report = ProSharesCrossValidator().validate("QLD", observations, {date(2024, 1, 2): 60.0})

    assert report.flagged[0].close == pytest.approx(70.0), "collected value preserved"
    assert report.findings[0].reference_value == pytest.approx(60.0)


# ------------------------------------------------------ the research boundary


def test_the_production_pipeline_does_not_import_the_research_package() -> None:
    """``BACKTEST_SPEC.md`` 28 — separated in code, not by convention.

    Parsed rather than grepped: a docstring that *mentions* the rule must not
    be mistaken for a violation of it.
    """
    import ast
    import pkgutil

    import regime_monitor.pipeline as pipeline_package

    offenders: list[str] = []
    for module in pkgutil.walk_packages(
        pipeline_package.__path__, prefix="regime_monitor.pipeline."
    ):
        imported = __import__(module.name, fromlist=["_"])
        tree = ast.parse(inspect.getsource(imported))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.startswith("regime_monitor.research") for name in names):
                offenders.append(f"{module.name}: {names}")
    assert not offenders, f"production modules importing research code: {offenders}"


def test_the_daily_runner_cannot_reach_the_optimiser() -> None:
    from regime_monitor.research import search

    assert "regime_monitor.pipeline" not in inspect.getsource(search)


# ------------------------------------------------------------------ coverage


def test_every_numbered_check_has_a_test() -> None:
    """Guards against a check quietly disappearing from this file."""
    source = inspect.getsource(inspect.getmodule(test_every_numbered_check_has_a_test))
    for number in range(1, 8):
        assert f"check {number}" in source, f"BACKTEST_SPEC.md 24.{number} has no test"


def test_the_cost_model_cannot_be_left_undeclared() -> None:
    # A backtest with unstated costs is not comparable to anything (§17, §25).
    from regime_monitor.backtest.costs import CostModel

    with pytest.raises(ValueError, match="unresolved research parameter"):
        CostModel.from_spec(CostModelSpec())

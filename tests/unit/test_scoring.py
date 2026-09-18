"""TASK-040 .. TASK-042 — normalization and the composite score."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame, Series

from fear_ladder.config.schema import NormalizationSpec, ScoreSpec
from fear_ladder.constants import IndicatorDirection
from fear_ladder.indicators.engine import IndicatorEngine
from fear_ladder.scoring.composite import ScoreEngine, ScoringError
from fear_ladder.scoring.normalizers import (
    LinearMappingNormalizer,
    NormalizationError,
    RollingPercentileNormalizer,
    RollingZScoreNormalizer,
    build_normalizer,
)

START = date(2020, 1, 1)
GREED = IndicatorDirection.HIGHER_IS_GREED
FEAR = IndicatorDirection.HIGHER_IS_FEAR


def _series(values: list[float]) -> Series:
    index = [START + timedelta(days=offset) for offset in range(len(values))]
    return Series(values, index=pd.Index(index, name="observation_date"), dtype="float64")


# ------------------------------------------------------------------ TASK-040


def test_percentile_normalizer_maps_rank_onto_the_score_axis() -> None:
    values = _series([10.0, 20.0, 15.0, 5.0, 30.0])
    scores = RollingPercentileNormalizer(direction=GREED, window=3).normalize(values)
    assert scores.iloc[:2].isna().all()
    assert scores.iloc[2] == pytest.approx(100 * 2 / 3)
    assert scores.iloc[4] == pytest.approx(100.0)


def test_direction_mirrors_fear_indicators() -> None:
    # VIX up must mean score down (PRD.md 7).
    values = _series([10.0, 20.0, 15.0, 5.0, 30.0])
    greed = RollingPercentileNormalizer(direction=GREED, window=3).normalize(values)
    fear = RollingPercentileNormalizer(direction=FEAR, window=3).normalize(values)
    pd.testing.assert_series_equal(fear.dropna(), (100.0 - greed).dropna())


def test_scores_stay_inside_the_domain() -> None:
    rng = np.random.default_rng(1)
    values = _series(list(rng.normal(0, 10, 500)))
    for normalizer in (
        RollingPercentileNormalizer(direction=GREED, window=100),
        RollingZScoreNormalizer(direction=GREED, window=100, clip_sigma=2.0),
        LinearMappingNormalizer(direction=GREED, raw_at_score_min=-5, raw_at_score_max=5),
    ):
        scores = normalizer.normalize(values).dropna()
        assert scores.between(0.0, 100.0).all(), normalizer.method


def test_zscore_puts_the_mean_at_the_midpoint() -> None:
    values = _series([100.0] * 50 + [100.0])
    scores = RollingZScoreNormalizer(direction=GREED, window=20).normalize(values)
    # A flat window carries no information; that is the midpoint, not infinity.
    assert scores.dropna().iloc[-1] == pytest.approx(50.0)


def test_zscore_clips_extremes_rather_than_exploding() -> None:
    values = _series([*[100.0 + i * 0.01 for i in range(60)], 1000.0])
    scores = RollingZScoreNormalizer(direction=GREED, window=30, clip_sigma=3.0).normalize(values)
    assert scores.iloc[-1] == pytest.approx(100.0)


def test_linear_mapping_is_the_identity_for_a_published_0_100_index() -> None:
    # CNN Fear & Greed is already on this axis.
    values = _series([0.0, 25.0, 50.0, 100.0])
    scores = LinearMappingNormalizer(
        direction=GREED, raw_at_score_min=0.0, raw_at_score_max=100.0
    ).normalize(values)
    assert list(scores) == [0.0, 25.0, 50.0, 100.0]


def test_linear_mapping_rejects_a_degenerate_range() -> None:
    with pytest.raises(NormalizationError, match="must differ"):
        LinearMappingNormalizer(raw_at_score_min=1.0, raw_at_score_max=1.0)


def test_builder_refuses_an_unresolved_window() -> None:
    # The window is a research parameter; there is no silent default.
    spec = NormalizationSpec(method="rolling_percentile", window=None)
    with pytest.raises(NormalizationError, match="unresolved research parameter"):
        build_normalizer(spec, GREED)


def test_builder_constructs_each_configured_method() -> None:
    assert isinstance(
        build_normalizer(NormalizationSpec(method="rolling_percentile", window=252), GREED),
        RollingPercentileNormalizer,
    )
    assert isinstance(
        build_normalizer(
            NormalizationSpec(method="rolling_zscore", window=252, clip_sigma=2.0), FEAR
        ),
        RollingZScoreNormalizer,
    )
    assert isinstance(
        build_normalizer(
            NormalizationSpec(method="bounded", raw_at_score_min=0, raw_at_score_max=1), FEAR
        ),
        LinearMappingNormalizer,
    )


# ------------------------------------------------------------------ TASK-041


@pytest.mark.parametrize(
    "normalizer",
    [
        RollingPercentileNormalizer(direction=GREED, window=60),
        RollingPercentileNormalizer(direction=FEAR, window=60),
        RollingZScoreNormalizer(direction=GREED, window=60),
        LinearMappingNormalizer(direction=GREED, raw_at_score_min=0, raw_at_score_max=100),
    ],
)
def test_normalization_is_past_only(normalizer) -> None:
    """Truncating the future must not change a single past score.

    This is the test ``BACKTEST_SPEC.md`` 24.3 asks for. A full-sample
    percentile or a full-sample mean/std would fail it immediately.
    """
    rng = np.random.default_rng(17)
    values = _series(list(rng.normal(50, 15, 400)))
    cutoff = 300

    full = normalizer.normalize(values).iloc[:cutoff]
    truncated = normalizer.normalize(values.iloc[:cutoff])
    pd.testing.assert_series_equal(full, truncated)


def test_a_future_shock_does_not_change_earlier_scores() -> None:
    # The concrete failure mode: a 2020-style crash must not retro-colour 2019.
    calm = _series([100.0 + np.sin(i / 5) for i in range(200)])
    crash = Series(
        [10.0] * 20,
        index=pd.Index(
            [calm.index[-1] + timedelta(days=i + 1) for i in range(20)],
            name="observation_date",
        ),
        dtype="float64",
    )
    with_shock = pd.concat([calm, crash])

    normalizer = RollingPercentileNormalizer(direction=GREED, window=60)
    before = normalizer.normalize(calm)
    after = normalizer.normalize(with_shock).iloc[: len(calm)]
    pd.testing.assert_series_equal(before, after)


# ------------------------------------------------------------------ TASK-042


def _engine(weights: dict[str, float], **kwargs) -> ScoreEngine:
    from fear_ladder.config.loader import load_research_placeholder_config

    config = load_research_placeholder_config()
    return ScoreEngine(
        indicators=config.indicators, score=ScoreSpec(weights=weights), **kwargs
    )


def _score_frame(rows: dict[date, dict[str, float | None]]) -> DataFrame:
    return DataFrame(rows).T.sort_index()


def test_composite_is_the_weighted_mean() -> None:
    engine = _engine({"rsi_14": 0.5, "vix_level": 0.5})
    scores = _score_frame({START: {"rsi_14": 80.0, "vix_level": 40.0}})
    assert engine.composite_series(scores).iloc[0] == pytest.approx(60.0)


def test_weights_are_respected() -> None:
    engine = _engine({"rsi_14": 0.75, "vix_level": 0.25})
    scores = _score_frame({START: {"rsi_14": 80.0, "vix_level": 40.0}})
    assert engine.composite_series(scores).iloc[0] == pytest.approx(70.0)


def test_a_missing_indicator_is_redistributed_not_read_as_zero() -> None:
    """The most dangerous possible default, guarded explicitly.

    Treating a missing indicator as 0 would read as extreme fear, and this
    strategy adds leverage into fear (``PRD.md`` 2.1). So the weight is
    redistributed over the indicators that are present.
    """
    engine = _engine({"rsi_14": 0.5, "vix_level": 0.5}, min_weight_coverage=0.5)
    scores = _score_frame({START: {"rsi_14": 80.0, "vix_level": None}})

    composite = engine.composite_series(scores).iloc[0]
    assert composite == pytest.approx(80.0)
    assert composite != pytest.approx(40.0), "a missing indicator must not count as 0"


def test_a_day_with_too_little_coverage_gets_no_score() -> None:
    engine = _engine(
        {"rsi_14": 0.2, "vix_level": 0.4, "momentum_1m": 0.4}, min_weight_coverage=0.6
    )
    scores = _score_frame({START: {"rsi_14": 80.0, "vix_level": None, "momentum_1m": None}})
    assert pd.isna(engine.composite_series(scores).iloc[0])


def test_composite_stays_inside_the_domain() -> None:
    engine = _engine({"rsi_14": 0.5, "vix_level": 0.5})
    scores = _score_frame(
        {
            START: {"rsi_14": 0.0, "vix_level": 0.0},
            START + timedelta(days=1): {"rsi_14": 100.0, "vix_level": 100.0},
        }
    )
    composite = engine.composite_series(scores)
    assert composite.min() >= 0.0
    assert composite.max() <= 100.0


def test_unresolved_weights_block_scoring() -> None:
    # CONTRIBUTING.md 10 — no silent default weighting.
    engine = _engine(None)  # type: ignore[arg-type]
    with pytest.raises(ScoringError, match="unresolved research parameter"):
        _ = engine.weights


def test_weights_must_name_enabled_indicators() -> None:
    engine = _engine({"rsi_14": 0.5, "breadth_pct_above_200dma": 0.5})
    with pytest.raises(ScoringError, match="disabled/unknown"):
        _ = engine.weights


# ---------------------------------------------------------------- breakdown


def test_breakdown_explains_the_score() -> None:
    engine = _engine({"rsi_14": 0.75, "vix_level": 0.25})
    scores = _score_frame({START: {"rsi_14": 80.0, "vix_level": 40.0}})

    breakdown = engine.breakdown(scores, START)
    assert breakdown.is_computable
    assert breakdown.composite == pytest.approx(70.0)
    assert breakdown.indicator_scores == {"rsi_14": 80.0, "vix_level": 40.0}
    assert breakdown.effective_weights["rsi_14"] == pytest.approx(0.75)
    assert sum(breakdown.contributions.values()) == pytest.approx(70.0)
    assert breakdown.weight_coverage == pytest.approx(1.0)


def test_breakdown_names_what_was_missing() -> None:
    engine = _engine({"rsi_14": 0.5, "vix_level": 0.5}, min_weight_coverage=0.5)
    scores = _score_frame({START: {"rsi_14": 80.0, "vix_level": None}})

    breakdown = engine.breakdown(scores, START)
    assert breakdown.missing == ("vix_level",)
    assert breakdown.effective_weights["rsi_14"] == pytest.approx(1.0)
    assert breakdown.weight_coverage == pytest.approx(0.5)


def test_breakdown_ranks_the_loudest_indicators() -> None:
    engine = _engine({"rsi_14": 0.5, "vix_level": 0.3, "momentum_1m": 0.2})
    scores = _score_frame({START: {"rsi_14": 52.0, "vix_level": 5.0, "momentum_1m": 95.0}})

    top = engine.breakdown(scores, START).top_contributors(2)
    assert [name for name, _ in top] == ["vix_level", "momentum_1m"]


def test_breakdown_of_an_unknown_day_is_not_computable() -> None:
    engine = _engine({"rsi_14": 1.0})
    breakdown = engine.breakdown(DataFrame(), START)
    assert not breakdown.is_computable
    assert breakdown.missing == ("rsi_14",)


# ------------------------------------------------------------ end to end


def _sources(length: int = 800) -> dict[str, Series]:
    rng = np.random.default_rng(29)
    return {
        "QQQ": _series(list(100.0 * np.cumprod(1 + rng.normal(0.0004, 0.011, length)))),
        "VIX": _series(list(np.abs(rng.normal(18, 4, length)))),
        "CNN_FEAR_GREED": _series(list(np.clip(rng.normal(50, 18, length), 0, 100))),
        "AAII_SENTIMENT": _series(list(rng.normal(0.05, 0.12, length))),
    }


def test_full_pipeline_from_prices_to_one_score(placeholder_config) -> None:
    results = IndicatorEngine(placeholder_config.indicators).compute_all(_sources())
    engine = ScoreEngine.from_config(placeholder_config)

    scores = engine.normalize(results)
    composite = engine.composite_series(scores)

    usable = composite.dropna()
    assert not usable.empty
    assert usable.between(0.0, 100.0).all()

    breakdown = engine.breakdown(scores, usable.index[-1])
    assert breakdown.is_computable
    assert breakdown.composite == pytest.approx(usable.iloc[-1], abs=1e-9)


def test_persistable_scores_carry_their_normalization(placeholder_config) -> None:
    results = IndicatorEngine(placeholder_config.indicators).compute_all(_sources())
    engine = ScoreEngine.from_config(placeholder_config)
    scores = engine.normalize(results)

    day = scores.dropna(how="all").index[-1]
    domain = engine.to_domain_scores(scores, day)
    by_name = {item.indicator_name: item for item in domain}

    assert by_name["rsi_14"].normalization_method == "rolling_percentile"
    assert by_name["rsi_14"].normalization_window == 504
    assert by_name["cnn_fear_greed"].normalization_method == "fixed_mapping"
    assert by_name["cnn_fear_greed"].normalization_window is None

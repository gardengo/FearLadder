"""The indicator-ablation harness (``scripts/indicator_ablation.py``).

TASK-183 removes indicators from the composite score. The thing that makes such
a measurement meaningful — or silently wrong — is the renormalisation.

``BACKTEST_SPEC.md`` §10 defines the score as a convex combination. Leaving a
removed indicator's weight as a hole would shrink every composite toward the
neutral midpoint, which moves every band edge in ``regime.boundaries`` without
changing a single configured number. The ablation would then be measuring a
quietly rescaled ladder rather than a smaller indicator set.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from types import ModuleType

import pandas as pd
import pytest
from pandas import Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.scoring.composite import ScoreEngine

START = date(2020, 1, 2)


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_indicator_ablation", paths.PROJECT_ROOT / "scripts" / "indicator_ablation.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class _Run:
    """Only the two fields ``signal_shift`` reads."""

    composite_score: Series
    regimes: Series


def _series(values: list[float]) -> Series:
    index = pd.Index(
        [START + timedelta(days=offset) for offset in range(len(values))],
        name="observation_date",
    )
    return Series(values, index=index, dtype="float64")


# ------------------------------------------------------------ renormalisation


def test_the_remaining_weights_still_sum_to_one(harness: ModuleType) -> None:
    weights = {"a": 0.5, "b": 0.3, "c": 0.2}
    kept = harness.renormalised(weights, ("b",))

    assert set(kept) == {"a", "c"}
    assert sum(kept.values()) == pytest.approx(1.0)


def test_renormalisation_preserves_relative_size(harness: ModuleType) -> None:
    """Rescaling must not re-rank what is left — that would be a different strategy."""
    weights = {"a": 0.5, "b": 0.3, "c": 0.2}
    kept = harness.renormalised(weights, ("b",))

    assert kept["a"] / kept["c"] == pytest.approx(weights["a"] / weights["c"])


def test_dropping_everything_is_refused(harness: ModuleType) -> None:
    with pytest.raises(ValueError, match="no indicator"):
        harness.renormalised({"a": 1.0}, ("a",))


def test_a_variant_leaves_the_frozen_config_alone(harness: ModuleType) -> None:
    config = load_config()
    before = dict(config.strategy.score.weights or {})

    variant = harness.variant_config(config, ("rsi_14",))
    assert "rsi_14" not in (variant.strategy.score.weights or {})
    assert config.strategy.score.weights == before


def test_the_variant_is_a_score_the_engine_will_accept(harness: ModuleType) -> None:
    """A renormalised set that the ScoreEngine rejects would fail 20 runs deep."""
    config = load_config()
    variant = harness.variant_config(config, ("vix_level",))

    weights = ScoreEngine.from_config(variant).weights
    assert "vix_level" not in weights
    assert sum(weights.values()) == pytest.approx(1.0)
    assert len(weights) == len(config.strategy.score.weights or {}) - 1


def test_a_family_variant_drops_every_member(harness: ModuleType) -> None:
    config = load_config()
    specs = config.indicators.enabled_indicators
    rsi = sorted(
        name
        for name in (config.strategy.score.weights or {})
        if specs[name].family == "rsi"
    )

    variant = harness.variant_config(config, tuple(rsi))
    remaining = variant.strategy.score.weights or {}
    assert not any(specs[name].family == "rsi" for name in remaining)
    assert sum(remaining.values()) == pytest.approx(1.0)


# -------------------------------------------------------------------- the plan


def test_the_plan_covers_every_weighted_indicator_once(harness: ModuleType) -> None:
    config = load_config()
    weights = config.strategy.score.weights or {}

    variants = harness.plan(config, families_only=False)
    singles = [item for item in variants if item.kind == "single"]
    assert sorted(item.dropped[0] for item in singles) == sorted(weights)
    assert all(len(item.dropped) == 1 for item in singles)


def test_the_family_pass_partitions_the_indicator_set(harness: ModuleType) -> None:
    """Correlated indicators hide each other in leave-one-out; the families must not overlap."""
    config = load_config()
    weights = config.strategy.score.weights or {}

    families = harness.plan(config, families_only=True)
    assert all(item.kind == "family" for item in families)
    members = [name for item in families for name in item.dropped]
    assert sorted(members) == sorted(weights), "families must tile the set exactly"
    assert len(members) == len(set(members)), "no indicator may be in two families"


def test_families_only_skips_the_single_pass(harness: ModuleType) -> None:
    config = load_config()
    assert all(
        item.kind == "family" for item in harness.plan(config, families_only=True)
    )


def test_the_windows_are_the_strategys_own_splits(harness: ModuleType) -> None:
    config = load_config()
    split = config.strategy.dataset_split
    spans = harness.windows(config)

    assert spans[0] == ("full 1996-2026", None, None)
    assert (split.research_start, split.research_end) in [(s, e) for _, s, e in spans]
    assert (split.oos_start, split.oos_end) in [(s, e) for _, s, e in spans]


# ------------------------------------------------------------- the signal shift


def test_an_identical_run_shows_no_shift(harness: ModuleType) -> None:
    scores = _series([10.0, 40.0, 70.0, 30.0])
    regimes = Series(["Fear", "Neutral", "Greed", "Fear"], index=scores.index)
    run = _Run(scores, regimes)

    shift = harness.signal_shift(run, _Run(scores.copy(), regimes.copy()))
    assert shift["score_correlation"] == pytest.approx(1.0)
    assert shift["score_mean_abs_shift"] == pytest.approx(0.0)
    assert shift["regime_disagreement"] == pytest.approx(0.0)


def test_a_moved_signal_is_reported_even_when_the_ranking_holds(harness: ModuleType) -> None:
    """Correlation alone would call a uniformly shifted score identical."""
    scores = _series([10.0, 40.0, 70.0, 30.0])
    regimes = Series(["Fear", "Neutral", "Greed", "Fear"], index=scores.index)

    shift = harness.signal_shift(
        _Run(scores, regimes), _Run(scores + 5.0, regimes.copy())
    )
    assert shift["score_correlation"] == pytest.approx(1.0)
    assert shift["score_mean_abs_shift"] == pytest.approx(5.0)


def test_regime_disagreement_counts_days(harness: ModuleType) -> None:
    scores = _series([10.0, 40.0, 70.0, 30.0])
    left = Series(["Fear", "Neutral", "Greed", "Fear"], index=scores.index)
    right = Series(["Fear", "Fear", "Greed", "Greed"], index=scores.index)

    shift = harness.signal_shift(_Run(scores, left), _Run(scores, right))
    assert shift["regime_disagreement"] == pytest.approx(0.5)

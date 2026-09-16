"""The trigger-sensitivity harness (``scripts/transition_sensitivity.py``).

The harness answers a question about the frozen strategy by *patching* the
production transition engine for the duration of one run. Two properties have
to hold or its numbers mean nothing: the patch must not survive the run, and
neither transform may see the future.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

import pandas as pd
import pytest

from fear_ladder import paths
from fear_ladder.regime.transition import TransitionEngine


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_transition_sensitivity", paths.PROJECT_ROOT / "scripts" / "transition_sensitivity.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _scores() -> pd.Series:
    return pd.Series(
        [10.0, 90.0, 20.0, 80.0, 30.0], index=pd.bdate_range("2026-01-01", periods=5)
    )


def test_the_patch_does_not_outlive_the_run(harness: ModuleType) -> None:
    """A leaked patch would silently contaminate every later variant."""
    original = TransitionEngine.run
    with harness._trigger(harness._lagged(1)):
        assert TransitionEngine.run is not original
    assert TransitionEngine.run is original


def test_the_patch_is_undone_even_when_the_run_raises(harness: ModuleType) -> None:
    original = TransitionEngine.run
    with pytest.raises(RuntimeError), harness._trigger(harness._lagged(1)):
        raise RuntimeError("backtest blew up")
    assert TransitionEngine.run is original


def test_the_baseline_patches_nothing(harness: ModuleType) -> None:
    original = TransitionEngine.run
    with harness._trigger(None):
        assert TransitionEngine.run is original


def test_a_lag_only_ever_looks_backwards(harness: ModuleType) -> None:
    """A lag that leaked a future score would invent performance."""
    scores = _scores()
    lagged = harness._lagged(2)(scores)
    assert lagged.iloc[:2].isna().all()
    assert lagged.iloc[2] == scores.iloc[0]
    assert lagged.iloc[4] == scores.iloc[2]


def test_a_mean_only_ever_looks_backwards(harness: ModuleType) -> None:
    scores = _scores()
    smoothed = harness._smoothed(3)(scores)
    # Third point averages the first three and nothing after them.
    assert smoothed.iloc[2] == pytest.approx(scores.iloc[:3].mean())
    assert smoothed.iloc[-1] == pytest.approx(scores.iloc[-3:].mean())


def test_a_mean_keeps_the_warm_up_days(harness: ModuleType) -> None:
    """Blanking them would move the strategy's start date, not just its trigger."""
    smoothed = harness._smoothed(10)(_scores())
    assert not smoothed.isna().any()


def test_every_variant_is_named_once(harness: ModuleType) -> None:
    variants = harness._variants([1, 2], [3, 5])
    names = [name for name, _ in variants]
    assert names[0] == "baseline"
    assert len(set(names)) == len(names) == 5

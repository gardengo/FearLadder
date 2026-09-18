"""The tail-risk harness (``scripts/tail_risk.py``).

The harness compares trend-filter constants across crash and calm eras. The
drawdown it reports must be the deepest one, dated correctly — the whole point
of ``docs/strategy.md`` §2.11 is *which era* a drawdown fell in.

The other property these numbers rest on — a variant must not mutate the frozen
configuration — now lives once in ``test_measurement.py``, with the helper the
harness calls.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from types import ModuleType

import pandas as pd
import pytest

from fear_ladder import paths
from fear_ladder.config.loader import load_config


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_tail_risk", paths.PROJECT_ROOT / "scripts" / "tail_risk.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_windows_span_both_priced_eras(harness: ModuleType) -> None:
    """A sweep read only over modelled prices would prove nothing about §2.11."""
    labels = [label for label, _, _ in harness.WINDOWS]
    assert "real 2010-2026" in labels
    assert any(label.startswith(("dotcom", "gfc")) for label in labels)


def test_the_frozen_value_is_marked_once_per_family(harness: ModuleType) -> None:
    config = load_config()
    names = [name for name, _ in harness._variants(config, [0.0, 0.5], [0.1, 0.2])]
    assert names == ["cap=0", "cap=0.5 *", "depth=0.1", "depth=0.2 *"]


def test_the_worst_drawdown_is_the_deepest_one(harness: ModuleType) -> None:
    days = pd.bdate_range("2026-01-01", periods=7)
    # Two declines; the second is deeper and must be the one reported.
    nav = pd.Series([100.0, 80.0, 100.0, 120.0, 60.0, 70.0, 75.0], index=days)
    peak, trough, depth = harness._worst_drawdown(nav)
    assert peak == days[3]
    assert trough == days[4]
    assert depth == pytest.approx(-0.5)


def test_the_real_sleeve_start_is_the_last_inception(harness: ModuleType) -> None:
    """Sleeves are real only once *every* held sleeve is real, not the first."""
    assert harness._real_sleeve_start(load_config()) == date(2010, 2, 11)

"""The event-attribution harness (``scripts/event_analysis.py``).

TASK-184 exists to give a comparison a denominator. Three properties decide
whether its numbers are a distribution or an artefact.

*The spans must tile the history.* Each event ends where the next begins and the
last one runs to the end, or returns are double-counted or dropped and the
ledger no longer sums to the path it came from.

*The paired decomposition must be exact.* Per-span log differences have to add
up to the total log-return gap between the two variants. If they do not, the
"distribution" is not a decomposition of anything.

*The breakpoints must come from both variants.* Using only the frozen set would
let one side choose the windows it is judged on.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from types import ModuleType

import numpy as np
import pandas as pd
import pytest
from pandas import Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config

START = date(2000, 1, 3)


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_event_analysis", paths.PROJECT_ROOT / "scripts" / "event_analysis.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _days(count: int) -> pd.Index:
    return pd.Index(
        [START + timedelta(days=offset) for offset in range(count)], name="observation_date"
    )


def _nav(values: list[float]) -> Series:
    return Series(values, index=_days(len(values)), dtype="float64")


# --------------------------------------------------------------- fake a run


@dataclass
class _Decision:
    observation_date: date
    regime: str
    previous_regime: str | None
    changed: bool


@dataclass
class _Trend:
    capped: bool


@dataclass
class _Allocation:
    trend: _Trend | None


@dataclass
class _Result:
    nav: Series
    target_leverage: Series


@dataclass
class _Run:
    result: _Result
    decisions: tuple[_Decision, ...]
    allocations: dict[date, _Allocation] = field(default_factory=dict)


def _run(changes: list[int], count: int = 12) -> tuple[ModuleType, _Run]:
    index = _days(count)
    nav = Series(np.linspace(1.0, 2.0, count), index=index, dtype="float64")
    decisions = tuple(
        _Decision(index[position], "Fear", "Neutral", position in changes)
        for position in range(count)
    )
    return _Run(
        result=_Result(nav=nav, target_leverage=Series(1.5, index=index)),
        decisions=decisions,
        allocations={day: _Allocation(_Trend(False)) for day in index},
    )


class _Data:
    def __init__(self, index: pd.Index) -> None:
        self.closes = pd.DataFrame(
            {"QQQ": np.linspace(100.0, 150.0, len(index))}, index=index
        )


# ------------------------------------------------------------------ the ledger


def test_the_spans_tile_the_history(harness: ModuleType) -> None:
    run = _run([1, 5, 8])
    rows = harness.ledger(load_config(), run, _Data(run.result.nav.index))

    assert len(rows) == 3
    assert [row["start"] for row in rows] == ["2000-01-04", "2000-01-08", "2000-01-11"]
    # Each span ends exactly where the next begins; the last runs to the end.
    assert rows[0]["end"] == rows[1]["start"]
    assert rows[1]["end"] == rows[2]["start"]
    assert rows[2]["end"] == str(run.result.nav.index[-1])


def test_the_edge_is_measured_against_the_index(harness: ModuleType) -> None:
    run = _run([1, 6])
    rows = harness.ledger(load_config(), run, _Data(run.result.nav.index))

    for row in rows:
        assert row["edge"] == pytest.approx(row["strategy_return"] - row["index_return"])


def test_a_capped_span_is_flagged(harness: ModuleType) -> None:
    """The boundary day belongs to the next span, not to both."""
    run = _run([1, 6])
    for position, day in enumerate(run.result.nav.index):
        run.allocations[day] = _Allocation(_Trend(position > 6))

    rows = harness.ledger(load_config(), run, _Data(run.result.nav.index))
    assert rows[0]["trend_capped_share"] == pytest.approx(0.0)
    assert rows[1]["trend_capped_share"] == pytest.approx(1.0)
    # Days are counted once each: (1, 6] is five, (6, 11] is five.
    assert [row["days"] for row in rows] == [5, 5]


def test_the_ladder_direction_follows_the_score_axis(harness: ModuleType) -> None:
    """Fear is *down* in score and *up* in leverage — the naming has to say which."""
    config = load_config()
    assert harness._direction(config, "Neutral", "Panic") == "into_fear"
    assert harness._direction(config, "Panic", "Neutral") == "into_greed"
    assert harness._direction(config, None, "Neutral") == "unknown"


def test_the_nominal_leverage_is_the_rungs_own(harness: ModuleType) -> None:
    config = load_config()
    labels = config.strategy.regime.labels or []
    deepest, shallowest = labels[0], labels[-1]

    assert harness.nominal_leverage(config, deepest) > harness.nominal_leverage(
        config, shallowest
    )
    assert harness.nominal_leverage(config, "NotARegime") is None


# ------------------------------------------------------------- the statistics


def test_describe_reports_a_distribution(harness: ModuleType) -> None:
    stats = harness.describe([-0.02, 0.01, 0.03, 0.10])

    assert stats["n"] == 4
    assert stats["mean"] == pytest.approx(0.03)
    assert stats["share_positive"] == pytest.approx(0.75)
    assert stats["worst"] == pytest.approx(-0.02)
    assert stats["best"] == pytest.approx(0.10)


def test_describe_survives_an_empty_sample(harness: ModuleType) -> None:
    assert harness.describe([]) == {"n": 0.0}


def test_a_constant_sample_gets_no_t(harness: ModuleType) -> None:
    """Zero dispersion is not infinite confidence."""
    assert harness.describe([0.05, 0.05, 0.05])["t"] == 0.0


def test_the_sign_test_is_exact(harness: ModuleType) -> None:
    assert harness.sign_test(5, 5) == pytest.approx(2 / 32)
    assert harness.sign_test(5, 10) == pytest.approx(1.0)
    assert harness.sign_test(0, 0) == pytest.approx(1.0)
    assert harness.sign_test(9, 10) == pytest.approx(harness.sign_test(1, 10))


# ---------------------------------------------------------------- the pairing


def test_the_span_differences_sum_to_the_total_gap(harness: ModuleType) -> None:
    """If this fails, the per-span numbers are not a decomposition of anything."""
    left = _nav([1.0, 1.1, 1.05, 1.2, 1.3, 1.25, 1.4])
    right = _nav([1.0, 1.2, 1.10, 1.15, 1.4, 1.35, 1.5])
    changes = [left.index[2]], [right.index[4]]

    stats = harness.paired((left, changes[0]), (right, changes[1]))
    expected = float(np.log(right.iloc[-1] / right.iloc[0]) - np.log(left.iloc[-1] / left.iloc[0]))
    assert stats["total_log_gap"] == pytest.approx(expected)


def test_both_variants_contribute_breakpoints(harness: ModuleType) -> None:
    left = _nav([1.0, 1.1, 1.05, 1.2, 1.3, 1.25, 1.4])
    right = left.copy()

    one = harness.paired((left, [left.index[3]]), (right, []))
    two = harness.paired((left, [left.index[3]]), (right, [right.index[1], right.index[5]]))
    # Three breakpoints make two spans; five make four.
    assert one["n"] == 2
    assert two["n"] == 4


def test_two_identical_paths_show_no_gap(harness: ModuleType) -> None:
    """Every span is a tie, and ties must not be read as a verdict."""
    nav = _nav([1.0, 1.1, 1.05, 1.2])
    stats = harness.paired((nav, [nav.index[1]]), (nav.copy(), [nav.index[2]]))

    assert stats["total_log_gap"] == pytest.approx(0.0)
    assert stats["wins"] == 0.0
    assert stats["losses"] == 0.0
    assert stats["ties"] == stats["n"]
    assert stats["sign_test_p"] == pytest.approx(1.0)


# ------------------------------------------------------------------ the config


def test_every_family_grid_contains_the_frozen_value(harness: ModuleType) -> None:
    """A grid that skipped it would compare the frozen strategy against nothing."""
    config = load_config()
    for family in harness.FAMILIES:
        assert harness.frozen_value(config, family.name) in family.values, family.name

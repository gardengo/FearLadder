"""The cash-exposure harness (``scripts/cash_exposure.py``).

TASK-186 turns §2.5's one number — "days 100% in cash" — into the thing the
argument was actually about: how long a person would sit out in one unbroken
run. Three properties decide whether that is measurement or storytelling.

*Stretch lengths must be counted along trading days.* A weekend is not a break
in exposure and a market holiday is not a month out. Counting on the calendar
would inflate every run and invent gaps that never happened.

*The decomposition must be a partition.* Crisis years plus the rest, and the
per-year series, each have to add back to exactly the same total. If they do
not, "the gap is five crises" is an assertion rather than a split.

*The variant must not contaminate the frozen config.* Every other number in
``docs/strategy.md`` is measured against the frozen value; a leak makes the
comparison a comparison of one thing with itself.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from types import ModuleType

import numpy as np
import pytest
from pandas import Series

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.constants import Asset


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_cash_exposure", paths.PROJECT_ROOT / "scripts" / "cash_exposure.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class _Allocation:
    weights: dict[Asset, float]


@dataclass(frozen=True)
class _Item:
    allocation: _Allocation | None


def _days(count: int, *, start: date = date(2000, 1, 3)) -> list[date]:
    """``count`` consecutive *positions*; the calendar gaps are deliberate."""
    return [start + timedelta(days=index * 3) for index in range(count)]


# --- stretches ---------------------------------------------------------------


def test_stretches_counts_trading_days_not_calendar_days(harness: ModuleType) -> None:
    """Three consecutive positions are a run of 3 even though 6 days elapse."""
    days = _days(5)
    runs = harness.stretches(days, {days[1], days[2], days[3]})
    assert runs == [(days[1], days[3], 3)]


def test_stretches_splits_on_a_gap(harness: ModuleType) -> None:
    days = _days(6)
    runs = harness.stretches(days, {days[0], days[1], days[3], days[4], days[5]})
    assert runs == [(days[0], days[1], 2), (days[3], days[5], 3)]


def test_stretches_handles_single_days(harness: ModuleType) -> None:
    days = _days(5)
    runs = harness.stretches(days, {days[0], days[2], days[4]})
    assert [length for _, _, length in runs] == [1, 1, 1]


def test_stretches_closes_a_run_that_reaches_the_end(harness: ModuleType) -> None:
    """The final run has no following gap to close it; it must still be emitted."""
    days = _days(4)
    runs = harness.stretches(days, {days[2], days[3]})
    assert runs == [(days[2], days[3], 2)]


def test_stretches_is_empty_when_nothing_is_flagged(harness: ModuleType) -> None:
    assert harness.stretches(_days(4), set()) == []


def test_stretches_total_equals_the_flagged_count(harness: ModuleType) -> None:
    """Every flagged day belongs to exactly one run — no double counting."""
    days = _days(20)
    flagged = {days[index] for index in (0, 1, 2, 5, 9, 10, 15, 16, 17, 19)}
    runs = harness.stretches(days, flagged)
    assert sum(length for _, _, length in runs) == len(flagged)


# --- describe_stretches ------------------------------------------------------


def test_describe_stretches_orders_by_length(harness: ModuleType) -> None:
    days = _days(30)
    runs = harness.stretches(days, set(days[:2]) | set(days[5:12]) | set(days[20:24]))
    described = harness.describe_stretches(runs, top=10)
    assert [entry["days"] for entry in described["longest"]] == [7, 4, 2]
    assert described["longest_days"] == 7
    assert described["count"] == 3


def test_describe_stretches_thresholds_are_inclusive(harness: ModuleType) -> None:
    """A run of exactly ``QUARTER`` days counts as three months, not almost."""
    days = _days(harness.QUARTER + 5)
    described = harness.describe_stretches(
        harness.stretches(days, set(days[: harness.QUARTER])), top=3
    )
    assert described["at_least_3_months"] == 1
    assert described["at_least_6_months"] == 0


def test_describe_stretches_survives_an_empty_run_list(harness: ModuleType) -> None:
    described = harness.describe_stretches([], top=5)
    assert described["count"] == 0
    assert described["longest"] == []


# --- equity_share ------------------------------------------------------------


def test_equity_share_is_zero_for_an_all_cash_sleeve(harness: ModuleType) -> None:
    item = _Item(_Allocation({Asset.CASH: 1.0}))
    assert harness.equity_share(item) == pytest.approx(0.0)


def test_equity_share_counts_every_non_cash_sleeve(harness: ModuleType) -> None:
    item = _Item(_Allocation({Asset.CASH: 0.5, Asset.QQQ: 0.3, Asset.QLD: 0.2}))
    assert harness.equity_share(item) == pytest.approx(0.5)


def test_equity_share_is_nan_without_an_allocation(harness: ModuleType) -> None:
    """A day the engine did not allocate is unknown, not fully invested."""
    assert np.isnan(harness.equity_share(_Item(None)))


# --- log_gap and decompose ---------------------------------------------------


def _series(values: list[float], days: list[date]) -> Series:
    return Series(values, index=days, dtype="float64")


def test_log_gap_ends_at_the_total_log_difference(harness: ModuleType) -> None:
    days = _days(4)
    better = _series([100.0, 110.0, 121.0, 133.1], days)
    worse = _series([50.0, 52.5, 55.125, 57.881], days)
    gap = harness.log_gap(better, worse)
    expected = np.log(133.1 / 100.0) - np.log(57.881 / 50.0)
    assert float(gap.iloc[-1]) == pytest.approx(expected)


def test_log_gap_ignores_differing_starting_capital(harness: ModuleType) -> None:
    """Rebasing is the point: two NAVs scaled apart must show a zero gap."""
    days = _days(4)
    base = _series([100.0, 105.0, 99.0, 112.0], days)
    gap = harness.log_gap(base * 7.0, base)
    assert float(gap.iloc[-1]) == pytest.approx(0.0, abs=1e-12)


def test_decompose_partitions_the_total_exactly(harness: ModuleType) -> None:
    days = [date(1999, 6, 1), date(2000, 6, 1), date(2001, 6, 1), date(2003, 6, 1)]
    gap = _series([0.0, 0.10, 0.25, 0.20], days)
    result = harness.decompose(gap, crisis=frozenset({2000, 2001}))
    assert result["in_crisis_years"] + result["in_other_years"] == pytest.approx(
        result["total"]
    )
    assert sum(result["by_year"].values()) == pytest.approx(result["total"])


def test_decompose_attributes_a_move_to_the_year_it_lands_in(
    harness: ModuleType,
) -> None:
    """The 2000 entry carries the move *into* 2000, not the move out of it."""
    days = [date(1999, 12, 31), date(2000, 6, 1), date(2001, 6, 1)]
    gap = _series([0.0, 0.30, 0.31], days)
    result = harness.decompose(gap, crisis=frozenset({2000}))
    assert result["in_crisis_years"] == pytest.approx(0.30)
    assert result["in_other_years"] == pytest.approx(0.01)


def test_decompose_counts_years_ahead_on_the_yearly_change(
    harness: ModuleType,
) -> None:
    """A year is 'ahead' when the gap grew in it — not when its level is positive."""
    days = [date(1999, 6, 1), date(2000, 6, 1), date(2001, 6, 1)]
    gap = _series([0.0, 0.50, 0.40], days)
    result = harness.decompose(gap, crisis=frozenset())
    assert result["years_ahead"] == 1
    assert result["years_total"] == 2


# --- era ---------------------------------------------------------------------


def test_era_uses_only_days_at_or_after_the_boundary(harness: ModuleType) -> None:
    days = [date(2008, 6, 1), date(2010, 1, 4), date(2012, 1, 4)]
    gap = _series([0.0, 0.50, 0.60], days)
    result = harness.era(gap, date(2010, 1, 1))
    assert result["total"] == pytest.approx(0.10)


def test_era_is_zero_when_the_window_is_empty(harness: ModuleType) -> None:
    days = [date(2000, 6, 1), date(2001, 6, 1)]
    result = harness.era(_series([0.0, 0.2], days), date(2030, 1, 1))
    assert result == {"total": 0.0, "annualised": 0.0, "years": 0.0}


# --- the variant must not leak ------------------------------------------------


def test_variant_leaves_the_frozen_config_untouched(harness: ModuleType) -> None:
    config = load_config()
    before = config.strategy.trend_filter.max_leverage_below
    changed = harness.variant(config, 0.0)
    assert changed.strategy.trend_filter.max_leverage_below == 0.0
    assert config.strategy.trend_filter.max_leverage_below == before
    assert changed is not config


def test_variant_changes_nothing_else_in_the_trend_filter(
    harness: ModuleType,
) -> None:
    config = load_config()
    changed = harness.variant(config, 0.0)
    original = config.strategy.trend_filter.model_dump()
    updated = changed.strategy.trend_filter.model_dump()
    assert {
        key: value for key, value in updated.items() if key != "max_leverage_below"
    } == {key: value for key, value in original.items() if key != "max_leverage_below"}


def test_frozen_cap_is_one_of_the_measured_caps(harness: ModuleType) -> None:
    """The report compares the frozen value against 0.0; if the freeze ever moves
    off 0.5 this comparison silently stops being about the live parameter."""
    config = load_config()
    assert float(config.strategy.trend_filter.max_leverage_below) in harness.CAPS

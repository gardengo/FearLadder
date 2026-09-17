"""The cost-model harness (``scripts/cost_model.py``).

TASK-185 sets ``spread_bps`` and ``slippage_bps``, which have been zero since
the project started. Two things have to hold for its table to mean anything.

*The arithmetic has to be the arithmetic.* The whole cost model is linear in
traded notional, and the script's claim is that ``turnover × bps × (1 + CAGR)``
predicts the measurement exactly — the last factor converting a log-return
reduction into a difference between two simple rates. A wrong rule that happens
to look right on one run would be worse than no rule.

*The crash attribution must not look ahead.* It decides whether a trade happened
"in a crash" from a running peak. Judging it against a peak set years later
would move trades into and out of the expensive bucket on the strength of the
future, which is the one thing ``BACKTEST_SPEC.md`` §24 forbids everywhere else.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from types import ModuleType

import pandas as pd
import pytest
from pandas import Series

from fear_ladder import paths
from fear_ladder.backtest.costs import CostModel
from fear_ladder.config.loader import load_config

START = date(2020, 1, 2)


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_cost_model", paths.PROJECT_ROOT / "scripts" / "cost_model.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _series(values: list[float]) -> Series:
    index = pd.Index(
        [START + timedelta(days=offset) for offset in range(len(values))],
        name="observation_date",
    )
    return Series(values, index=index, dtype="float64")


# ------------------------------------------------------------------ the config


def test_a_scenario_leaves_the_frozen_config_alone(harness: ModuleType) -> None:
    config = load_config()
    before = config.strategy.cost_model.spread_bps

    variant = harness.with_costs(config, harness.Scenario("t", 5.0, 5.0, "test"))
    assert variant.strategy.cost_model.spread_bps == 5.0
    assert config.strategy.cost_model.spread_bps == before


def test_the_commission_is_not_disturbed(harness: ModuleType) -> None:
    """The scenarios add to the frozen commission; they do not replace it."""
    config = load_config()
    variant = harness.with_costs(config, harness.Scenario("t", 5.0, 5.0, "test"))

    assert (
        variant.strategy.cost_model.commission_bps
        == config.strategy.cost_model.commission_bps
    )
    assert variant.strategy.cost_model.total_bps == pytest.approx(
        float(config.strategy.cost_model.commission_bps) + 10.0
    )


def test_the_frozen_scenario_is_the_frozen_config(harness: ModuleType) -> None:
    """The first row has to be the baseline, or every drag is measured off nothing."""
    config = load_config()
    frozen = next(row for row in harness.SCENARIOS if row.name == "frozen")

    assert frozen is harness.SCENARIOS[0]
    assert frozen.spread_bps == config.strategy.cost_model.spread_bps
    assert frozen.slippage_bps == config.strategy.cost_model.slippage_bps
    assert frozen.extra_bps == 0.0


def test_a_scenario_is_a_cost_model_the_engine_will_build(harness: ModuleType) -> None:
    variant = harness.with_costs(
        load_config(), harness.Scenario("wide", 12.5, 12.5, "test")
    )
    model = CostModel.from_spec(variant.strategy.cost_model)

    assert model.spread_bps == 12.5
    assert model.total_bps == pytest.approx(35.0)


# -------------------------------------------------------------------- the rule


def test_the_rule_is_turnover_times_bps_in_log_terms(harness: ModuleType) -> None:
    """3.34 turns a year at 10bps is 33.4bps of log drag, before the conversion."""
    assert harness.predicted_drag(3.34, 10.0, 0.0) == pytest.approx(0.00334)
    assert harness.predicted_drag(3.34, 0.0, 0.19) == pytest.approx(0.0)


def test_the_rule_is_linear_in_turnover_and_bps(harness: ModuleType) -> None:
    assert harness.predicted_drag(2.0, 20.0, 0.1) == pytest.approx(
        harness.predicted_drag(4.0, 10.0, 0.1)
    )
    assert harness.predicted_drag(1.0, 50.0, 0.1) == pytest.approx(
        5 * harness.predicted_drag(1.0, 10.0, 0.1)
    )


def test_the_cagr_conversion_is_what_closes_the_gap(harness: ModuleType) -> None:
    """A cost cuts the *log* return; CAGR is a simple rate, so it scales by 1+g."""
    naive = harness.predicted_drag(3.26, 10.0, 0.0)
    converted = harness.predicted_drag(3.26, 10.0, 0.1888)

    assert converted == pytest.approx(naive * 1.1888)
    # Same turnover, a losing window: the drag shrinks rather than grows.
    assert harness.predicted_drag(3.26, 10.0, -0.0313) < naive


def test_the_rule_agrees_with_the_engines_own_cost(harness: ModuleType) -> None:
    """The prediction must be the model, not a parallel guess at it."""
    model = CostModel(commission_bps=10.0, spread_bps=5.0, slippage_bps=5.0)
    turnover_per_year = 3.34

    assert harness.predicted_drag(turnover_per_year, 10.0, 0.0) == pytest.approx(
        model.cost_of(turnover_per_year) - CostModel(commission_bps=10.0).cost_of(
            turnover_per_year
        )
    )


# ------------------------------------------------------------- the attribution


def test_turnover_is_split_by_the_running_drawdown(harness: ModuleType) -> None:
    index = _series([100.0, 100.0, 70.0, 70.0, 100.0])
    turnover = _series([1.0, 0.0, 3.0, 1.0, 5.0])

    split = harness.attribute(turnover, index, threshold=0.20)
    assert split["total_turnover"] == pytest.approx(10.0)
    assert split["crash_turnover"] == pytest.approx(4.0)
    assert split["crash_share"] == pytest.approx(0.4)
    assert split["crash_days_share"] == pytest.approx(0.4)


def test_the_peak_is_the_one_known_at_the_time(harness: ModuleType) -> None:
    """A later high must not reclassify an earlier trade as a crash trade."""
    # Flat, then a climb. Nothing here is ever 20% below a peak *so far*.
    index = _series([100.0, 100.0, 100.0, 200.0])
    turnover = _series([1.0, 1.0, 1.0, 1.0])

    split = harness.attribute(turnover, index, threshold=0.20)
    assert split["crash_turnover"] == pytest.approx(0.0)

    # With a full-sample peak of 200, the first three days would all read as
    # -50% and the whole of that turnover would be misfiled.
    assert split["crash_share"] == pytest.approx(0.0)


def test_a_quiet_history_attributes_nothing(harness: ModuleType) -> None:
    index = _series([100.0, 101.0, 102.0])
    split = harness.attribute(_series([2.0, 2.0, 2.0]), index, threshold=0.20)

    assert split["crash_share"] == pytest.approx(0.0)
    assert split["total_turnover"] == pytest.approx(6.0)


def test_no_turnover_does_not_divide_by_zero(harness: ModuleType) -> None:
    index = _series([100.0, 50.0])
    split = harness.attribute(_series([0.0, 0.0]), index, threshold=0.20)

    assert split["crash_share"] == 0.0

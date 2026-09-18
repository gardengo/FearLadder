"""The reconstruction-accuracy harness (``scripts/reconstruction_accuracy.py``).

The harness answers TASK-181: is the price model still trustworthy inside a
crash? Its numbers only mean something if three properties hold.

*It must not touch observed prices.* The stress correction exists to re-price
the *modelled* era. If it bled past a fund's inception it would be silently
editing quotes, and the comparison against real prices would be circular.

*It must be conservative in the right direction.* A fund that bore more carry
than modelled reached its known inception price from a higher start, so the
corrected history starts higher and falls further. A correction that flattered
the drawdown would be worse than none.

(The third property every one of these harnesses rests on — a variant must not
contaminate the frozen configuration, because ``config/`` is the freeze's
evidence — is checked once, in ``test_measurement.py``.)
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from types import ModuleType
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame, Series

from fear_ladder import paths
from fear_ladder.data.collectors.synthetic import INCEPTION, leveraged_returns

START = date(2000, 1, 3)
INCEPTION_OFFSET = 200


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_reconstruction_accuracy", paths.PROJECT_ROOT / "scripts" / "reconstruction_accuracy.py"
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


def _walk(count: int, seed: int = 5, drift: float = 0.0003, vol: float = 0.012) -> Series:
    values = 100.0 * np.cumprod(1 + np.random.default_rng(seed).normal(drift, vol, count))
    return Series(values, index=_days(count), dtype="float64")


def _prices(count: int = 400) -> DataFrame:
    """A QQQ path plus two sleeves that are exact 2x/3x of it, unlevered costs aside."""
    qqq = _walk(count)
    growth = qqq / qqq.iloc[0]
    return DataFrame({"QQQ": qqq, "QLD": 50.0 * growth**2, "TQQQ": 20.0 * growth**3})


# ------------------------------------------------------------------ the model


def test_the_model_is_anchored_on_the_windows_first_real_price(harness: ModuleType) -> None:
    underlying = _walk(120)
    real = 2.0 * underlying.loc[underlying.index[20] :]
    financing = Series(0.0, index=underlying.index)

    model = harness.model_path(underlying, real, leverage=2.0, financing=financing, drag=0.0)
    assert model.index[0] == real.index[0]
    assert model.iloc[0] == pytest.approx(float(real.iloc[0]))
    assert model.index[-1] == real.index[-1]


def test_the_solver_recovers_a_drag_it_was_given(harness: ModuleType) -> None:
    """``implied_drag`` is only worth quoting if it inverts the model exactly."""
    underlying = _walk(500)
    financing = Series(0.0, index=underlying.index)
    returns = leveraged_returns(underlying, leverage=2.0, financing=financing, drag=0.05)
    real = (100.0 * (1 + returns.fillna(0.0)).cumprod()).iloc[1:]

    solved = harness.implied_drag(underlying, real, leverage=2.0, financing=financing)
    assert solved == pytest.approx(0.05, abs=1e-4)


def test_an_implausible_divergence_is_reported_as_unexplained(harness: ModuleType) -> None:
    """A gap no carry assumption can close must not be dressed up as one."""
    underlying = _walk(300)
    real = Series(
        np.linspace(100.0, 1.0, len(underlying)), index=underlying.index, dtype="float64"
    )
    financing = Series(0.0, index=underlying.index)

    assert harness.implied_drag(underlying, real, leverage=2.0, financing=financing) is None


def test_the_comparison_names_the_optimistic_direction(harness: ModuleType) -> None:
    """Positive ``drawdown_error_pp`` has to mean the model understated the loss."""
    index = _days(5)
    real = Series([100.0, 80.0, 60.0, 70.0, 90.0], index=index)
    shallow = Series([100.0, 85.0, 75.0, 80.0, 95.0], index=index)

    report = harness.compare(real, shallow)
    assert report["real_max_drawdown"] == pytest.approx(-0.40)
    assert report["model_max_drawdown"] == pytest.approx(-0.25)
    assert report["drawdown_error_pp"] == pytest.approx(15.0)
    assert report["optimistic"] is True

    assert harness.compare(real, real.copy())["optimistic"] is False


def test_drift_makes_windows_of_different_length_comparable(harness: ModuleType) -> None:
    """The point of the annualised column: the same 2% means different things."""
    def drifting(days: int) -> tuple[Series, Series]:
        real = Series(np.linspace(100.0, 110.0, days), index=_days(days))
        return real, real * np.linspace(1.0, 1.02, days)

    short = harness.compare(*drifting(253))
    long = harness.compare(*drifting(1009))
    assert short["cumulative_error"] == pytest.approx(long["cumulative_error"], abs=1e-9)
    assert short["annualised_drift"] > 3 * long["annualised_drift"]


def test_the_worst_drawdown_is_the_deepest_one(harness: ModuleType) -> None:
    index = _days(7)
    prices = Series([100.0, 90.0, 100.0, 120.0, 60.0, 70.0, 80.0], index=index)
    trough, depth = harness.worst_drawdown(prices)

    assert trough == index[4]
    assert depth == pytest.approx(-0.50)


# ----------------------------------------------------------------- the stress


def test_the_stress_never_reaches_observed_prices(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correction re-prices the model. Past inception there is nothing to re-price."""
    prices = _prices()
    inception = prices.index[INCEPTION_OFFSET]
    monkeypatch.setitem(INCEPTION, "QLD", inception)
    monkeypatch.setitem(INCEPTION, "TQQQ", inception)

    stressed = harness.stressed_prices(prices, extra={"QLD": 0.06, "TQQQ": 0.12}, threshold=0.0)
    for symbol in ("QLD", "TQQQ"):
        observed = prices.loc[inception:, symbol]
        pd.testing.assert_series_equal(stressed.loc[inception:, symbol], observed)


def test_the_underlying_is_left_alone(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every indicator reads QQQ, so moving it would move the regime path too.

    The whole claim being tested in TASK-181 is that sleeve mispricing changes
    what the portfolio was *worth* and not which rung it was on.
    """
    prices = _prices()
    monkeypatch.setitem(INCEPTION, "QLD", prices.index[INCEPTION_OFFSET])
    monkeypatch.setitem(INCEPTION, "TQQQ", prices.index[INCEPTION_OFFSET])

    stressed = harness.stressed_prices(prices, extra={"QLD": 0.06, "TQQQ": 0.12}, threshold=0.0)
    pd.testing.assert_series_equal(stressed["QQQ"], prices["QQQ"])


def test_the_stress_deepens_the_modelled_decline(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fund that bled more carry started higher, so it must fall further."""
    prices = _prices()
    inception = prices.index[INCEPTION_OFFSET]
    monkeypatch.setitem(INCEPTION, "QLD", inception)
    monkeypatch.setitem(INCEPTION, "TQQQ", inception)

    stressed = harness.stressed_prices(prices, extra={"QLD": 0.06, "TQQQ": 0.12}, threshold=0.0)
    modelled = prices.index < inception
    assert (stressed.loc[modelled, "QLD"] >= prices.loc[modelled, "QLD"]).all()

    before = prices.loc[modelled, "QLD"]
    after = stressed.loc[modelled, "QLD"]
    assert harness.worst_drawdown(after)[1] <= harness.worst_drawdown(before)[1]


def test_a_higher_leverage_sleeve_is_charged_more(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    prices = _prices()
    inception = prices.index[INCEPTION_OFFSET]
    monkeypatch.setitem(INCEPTION, "QLD", inception)
    monkeypatch.setitem(INCEPTION, "TQQQ", inception)

    stressed = harness.stressed_prices(prices, extra={"QLD": 0.06, "TQQQ": 0.12}, threshold=0.0)
    first = prices.index[0]
    two = stressed.loc[first, "QLD"] / prices.loc[first, "QLD"]
    three = stressed.loc[first, "TQQQ"] / prices.loc[first, "TQQQ"]
    assert three > two > 1.0


def test_the_threshold_decides_which_days_are_charged(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calm windows imply a *negative* drag, so charging every day is not conservative."""
    prices = _prices()
    inception = prices.index[INCEPTION_OFFSET]
    monkeypatch.setitem(INCEPTION, "QLD", inception)

    always = harness.stressed_prices(prices, extra={"QLD": 0.06}, threshold=0.0)
    never = harness.stressed_prices(prices, extra={"QLD": 0.06}, threshold=0.99)
    first = prices.index[0]

    assert always.loc[first, "QLD"] > prices.loc[first, "QLD"]
    assert never.loc[first, "QLD"] == pytest.approx(float(prices.loc[first, "QLD"]))


def test_the_borrowed_exposure_split_matches_the_sleeves(harness: ModuleType) -> None:
    class _Provider:
        leverages: ClassVar[dict[str, float]] = {"QQQ": 1.0, "QLD": 2.0, "TQQQ": 3.0}

    drags = harness.stress_drags(0.06, _Provider())
    assert drags["QQQ"] == pytest.approx(0.0)
    assert drags["QLD"] == pytest.approx(0.06)
    assert drags["TQQQ"] == pytest.approx(0.12)


# ------------------------------------------------------------------ the config


def test_leverage_scaling_skips_a_window_with_only_one_sleeve(harness: ModuleType) -> None:
    """2008 has QLD and no TQQQ; pairing it with nothing would invent a ratio."""
    windows = [
        {
            "window": "gfc 2007-2009",
            "kind": "crash",
            "funds": [{"symbol": "QLD", "tracking_error_annual": 0.05}],
        }
    ]
    assert harness.leverage_scaling(windows) == []


def test_find_measurement_returns_the_named_row(harness: ModuleType) -> None:
    windows = [
        {"window": "calm", "funds": [{"symbol": "QLD", "drag_shortfall": 0.01}]},
        {"window": "crash", "funds": [{"symbol": "QLD", "drag_shortfall": 0.06}]},
    ]
    found = harness.find_measurement(windows, "crash", "QLD")
    assert found is not None
    assert found["drag_shortfall"] == 0.06
    assert harness.find_measurement(windows, "crash", "TQQQ") is None

"""The cross-market harness (``scripts/cross_market.py``).

TASK-182 runs the frozen strategy on SPY/SSO/UPRO by relabelling them as the
engine's own sleeves. That substitution is the whole experiment, so the tests
here are mostly about it not leaking:

*The S&P run must not read a Nasdaq price.* The sleeves come from the relabelled
matrix and everything else from the database; if a price symbol were requested
from the database too, half the portfolio would silently be the wrong market.

*A variant must not contaminate the frozen configuration*, the same rule as
``test_tail_risk.py`` and ``test_reconstruction_accuracy.py``.

*The monotone / sawtooth verdict must be exact.* It is the finding the whole
script exists to produce, and "almost monotone" is not a category §2.11 has.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from types import ModuleType

import pandas as pd
import pytest
from pandas import DataFrame

from fear_ladder import paths
from fear_ladder.config.loader import load_config

START = date(2010, 1, 4)


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_cross_market", paths.PROJECT_ROOT / "scripts" / "cross_market.py"
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


def _prices(harness: ModuleType, count: int = 40):
    index = _days(count)
    base = DataFrame(
        {
            "QQQ": [100.0 + n for n in range(count)],
            "QLD": [50.0 + 2 * n for n in range(count)],
            "TQQQ": [20.0 + 3 * n for n in range(count)],
        },
        index=index,
    )
    return harness.Prices(closes=base, opens=base * 0.99, drags={"SSO": -0.012, "UPRO": -0.029})


class _Repository:
    """Records what was asked for, and answers with scalars only."""

    def __init__(self, index: pd.Index) -> None:
        self.asked: list[str] = []
        self.index = index

    def get_series_frame(self, symbols, **_):
        self.asked = list(symbols)
        return DataFrame(
            {symbol: [20.0] * len(self.index) for symbol in symbols}, index=self.index
        )


# --------------------------------------------------------------- the relabelling


def test_the_sp_run_never_asks_the_database_for_a_price(harness: ModuleType) -> None:
    """The prices come from the relabelled matrix. Asking for QQQ would mix markets."""
    config = load_config()
    prices = _prices(harness)
    repository = _Repository(prices.closes.index)

    harness.relabelled_data(repository, config, prices)
    for symbol in config.data_sources.price.symbols:
        assert symbol not in repository.asked, symbol
    assert "VIX" in repository.asked
    assert config.data_sources.cash_rate_series in repository.asked


def test_the_relabelled_sleeves_are_the_ones_supplied(harness: ModuleType) -> None:
    config = load_config()
    prices = _prices(harness)

    data = harness.relabelled_data(_Repository(prices.closes.index), config, prices)
    pd.testing.assert_frame_equal(data.closes, prices.closes)
    for column in prices.closes.columns:
        pd.testing.assert_series_equal(
            data.series[column], prices.closes[column], check_names=False
        )


def test_the_cash_sleeve_still_earns(harness: ModuleType) -> None:
    """A missing cash rate would quietly make every defensive rule look worse."""
    config = load_config()
    prices = _prices(harness)

    data = harness.relabelled_data(_Repository(prices.closes.index), config, prices)
    assert data.cash_rates is not None
    assert len(data.cash_rates) == len(prices.closes)


def test_the_spec_separates_the_underlying_from_the_sleeves(harness: ModuleType) -> None:
    assert harness.SP500.underlying == "SPY"
    assert harness.SP500.sleeves == {"QLD": "SSO", "TQQQ": "UPRO"}
    # UPRO listed after the crisis; recording 2008 would invent a real 3x sleeve.
    assert harness.SP500.inceptions["UPRO"] == date(2009, 6, 25)


# ------------------------------------------------------------------- the config


def test_a_variant_leaves_the_frozen_config_alone(harness: ModuleType) -> None:
    config = load_config()
    before = config.strategy.trend_filter.max_leverage_below

    variant = harness.variant(config, "max_leverage_below", 1.5)
    assert variant.strategy.trend_filter.max_leverage_below == 1.5
    assert config.strategy.trend_filter.max_leverage_below == before
    assert variant.strategy.transition == config.strategy.transition


def test_a_duration_variant_stays_an_integer(harness: ModuleType) -> None:
    """The grid is floats for uniformity; the schema's field is not."""
    variant = harness.variant(load_config(), "minimum_duration_days", 120.0)
    assert variant.strategy.transition.minimum_duration_days == 120
    assert isinstance(variant.strategy.transition.minimum_duration_days, int)


def test_a_hysteresis_variant_changes_nothing_else(harness: ModuleType) -> None:
    config = load_config()
    variant = harness.variant(config, "hysteresis", 12.0)

    assert variant.strategy.transition.hysteresis == 12.0
    assert (
        variant.strategy.transition.minimum_duration_days
        == config.strategy.transition.minimum_duration_days
    )
    assert variant.strategy.trend_filter == config.strategy.trend_filter


def test_an_unknown_family_is_refused(harness: ModuleType) -> None:
    with pytest.raises(ValueError, match="unknown parameter family"):
        harness.variant(load_config(), "confirmation_days", 3.0)


def test_the_frozen_value_is_read_from_the_right_block(harness: ModuleType) -> None:
    config = load_config()
    assert harness.frozen_value(config, "max_leverage_below") == pytest.approx(
        config.strategy.trend_filter.max_leverage_below
    )
    assert harness.frozen_value(config, "minimum_duration_days") == pytest.approx(
        config.strategy.transition.minimum_duration_days
    )


# ------------------------------------------------------------------- the verdict


def test_monotone_means_every_step_agrees(harness: ModuleType) -> None:
    assert harness.direction([1.0, 2.0, 3.0, 4.0]) == "increasing"
    assert harness.direction([-0.4, -0.5, -0.6]) == "decreasing"
    # One step out of line is a sawtooth, not "nearly monotone".
    assert harness.direction([1.0, 2.0, 1.9, 4.0]) == "none"
    # A flat step is not an agreement either.
    assert harness.direction([1.0, 1.0, 2.0]) == "none"


def test_rank_agreement_is_signed(harness: ModuleType) -> None:
    ordered = [1.0, 2.0, 3.0, 4.0]
    assert harness.spearman(ordered, [10.0, 20.0, 30.0, 40.0]) == pytest.approx(1.0)
    assert harness.spearman(ordered, list(reversed(ordered))) == pytest.approx(-1.0)


def test_agreement_reports_one_row_per_metric(harness: ModuleType) -> None:
    sweep = harness.Sweep("max_leverage_below", (0.0, 0.5, 1.0), "monotone")
    measured = {
        "nasdaq100": {
            0.0: {"cagr": 0.19, "max_drawdown": -0.59, "sharpe": 0.67},
            0.5: {"cagr": 0.18, "max_drawdown": -0.62, "sharpe": 0.64},
            1.0: {"cagr": 0.17, "max_drawdown": -0.83, "sharpe": 0.57},
        },
        "sp500": {
            0.0: {"cagr": 0.11, "max_drawdown": -0.50, "sharpe": 0.45},
            0.5: {"cagr": 0.10, "max_drawdown": -0.58, "sharpe": 0.41},
            1.0: {"cagr": 0.09, "max_drawdown": -0.70, "sharpe": 0.35},
        },
    }
    rows = harness._agreement(sweep, "full", measured)

    assert [row["metric"] for row in rows] == ["cagr", "max_drawdown", "sharpe"]
    drawdown = next(row for row in rows if row["metric"] == "max_drawdown")
    assert drawdown["direction"] == {"nasdaq100": "decreasing", "sp500": "decreasing"}
    assert drawdown["spearman"] == pytest.approx(1.0)


# --------------------------------------------------------------------- the cache


def test_cached_prices_round_trip(harness: ModuleType, tmp_path) -> None:
    """A cached matrix has to remember the drags that priced it, or it cannot say how."""
    prices = _prices(harness)
    path = tmp_path / "sp500.json"
    prices.write(path)

    restored = harness.Prices.read(path)
    pd.testing.assert_frame_equal(restored.closes, prices.closes)
    pd.testing.assert_frame_equal(restored.opens, prices.opens)
    assert restored.drags == prices.drags
    assert restored.closes.index[0] == prices.closes.index[0]

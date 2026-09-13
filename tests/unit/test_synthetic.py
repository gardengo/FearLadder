"""Reconstructed price history — the model has to be measured, not asserted.

Two kinds of test here. The offline ones check the algebra and the seam. The
``network`` ones re-measure the reconstruction against the real funds, so a
change that quietly degrades tracking fails rather than silently producing a
worse backtest.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame, Series

from fear_ladder.data.collectors.base import SourceDescription, standardize_prices
from fear_ladder.data.collectors.synthetic import (
    INCEPTION,
    MEASURED_DRAG,
    TRADING_DAYS_PER_YEAR,
    ReconstructionError,
    SplicedIndexProvider,
    SyntheticLeveragedProvider,
    daily_financing,
    is_reconstructed,
    leveraged_returns,
    rebase,
    tracking_report,
)

START = date(2000, 1, 3)


def _prices(values: list[float], start: date = START) -> Series:
    index = pd.Index(
        [start + timedelta(days=offset) for offset in range(len(values))],
        name="observation_date",
    )
    return Series(values, index=index, dtype="float64")


class _FakeProvider:
    """Serves canned frames, so the algebra is tested without a network."""

    def __init__(self, frames: dict[str, Series]) -> None:
        self.frames = frames

    def fetch(self, symbol: str, start: date, end: date) -> DataFrame:
        series = self.frames[symbol]
        window = series[(series.index >= start) & (series.index <= end)]
        if window.empty:
            from fear_ladder.data.collectors.base import DataUnavailableError

            raise DataUnavailableError(f"{symbol}: nothing in {start}..{end}")
        return standardize_prices(window.to_frame("close"), symbol=symbol)

    def describe(self, symbol: str) -> SourceDescription:
        return SourceDescription("FinanceDataReader", "0.9.202", "test feed", f"fdr:{symbol}")


# ------------------------------------------------------------------- algebra


def test_the_replication_identity_is_exact_without_costs() -> None:
    underlying = _prices([100.0, 110.0, 99.0])
    financing = Series(0.0, index=underlying.index)
    returns = leveraged_returns(underlying, leverage=3.0, financing=financing, drag=0.0)

    # +10% then -10% at 3x is +30% then -30%.
    assert returns.iloc[1] == pytest.approx(0.30)
    assert returns.iloc[2] == pytest.approx(-0.30)


def test_financing_is_charged_on_the_borrowed_portion_only() -> None:
    underlying = _prices([100.0, 100.0])
    financing = Series(0.01 / TRADING_DAYS_PER_YEAR, index=underlying.index)

    # A flat market: the whole move is the cost of carry.
    at_2x = leveraged_returns(underlying, leverage=2.0, financing=financing, drag=0.0)
    at_3x = leveraged_returns(underlying, leverage=3.0, financing=financing, drag=0.0)
    assert at_3x.iloc[1] == pytest.approx(2 * at_2x.iloc[1])

    # 1x borrows nothing.
    at_1x = leveraged_returns(underlying, leverage=1.0, financing=financing, drag=0.0)
    assert at_1x.iloc[1] == pytest.approx(0.0)


def test_drag_compounds_at_the_stated_annual_rate() -> None:
    underlying = _prices([100.0] * (TRADING_DAYS_PER_YEAR + 1))
    financing = Series(0.0, index=underlying.index)
    returns = leveraged_returns(underlying, leverage=2.0, financing=financing, drag=0.01)

    path = (1 + returns.fillna(0.0)).cumprod()
    assert path.iloc[-1] == pytest.approx(0.99, abs=1e-4)


def test_leverage_below_one_is_rejected() -> None:
    with pytest.raises(ReconstructionError, match="leverage must be"):
        leveraged_returns(_prices([1.0, 2.0]), leverage=0.5, financing=Series(dtype=float), drag=0)


def test_financing_forward_fills_onto_the_trading_calendar() -> None:
    rates = Series([5.0], index=pd.Index([date(2000, 1, 3)]))
    index = pd.Index([date(2000, 1, 3), date(2000, 1, 4), date(2000, 1, 5)])
    daily = daily_financing(rates, index)

    assert daily.iloc[0] == pytest.approx(0.05 / TRADING_DAYS_PER_YEAR)
    assert daily.iloc[2] == pytest.approx(daily.iloc[0]), "a stale rate carries forward"


def test_rebasing_pins_the_path_to_its_anchor() -> None:
    returns = Series(
        [np.nan, 0.10, -0.05, 0.02],
        index=pd.Index([START + timedelta(days=n) for n in range(4)]),
    )
    anchor = START + timedelta(days=2)
    path = rebase(returns, anchor_value=50.0, anchor_date=anchor)
    assert path.loc[anchor] == pytest.approx(50.0)


def test_rebasing_requires_a_real_anchor() -> None:
    returns = Series([0.01], index=pd.Index([START]))
    with pytest.raises(ReconstructionError, match="anchor"):
        rebase(returns, anchor_value=1.0, anchor_date=date(1990, 1, 1))


# ---------------------------------------------------------------- the seam


def _synthetic_setup() -> tuple[_FakeProvider, date]:
    """QQQ from 2000, a fund that 'launched' in 2001."""
    days = 400
    rng = np.random.default_rng(3)
    qqq = _prices(list(100.0 * np.cumprod(1 + rng.normal(0.0004, 0.011, days))))
    inception = qqq.index[200]
    fund = 2.0 * qqq.loc[inception:] / qqq.loc[inception]  # arbitrary level
    return _FakeProvider({"QQQ": qqq, "QLD": fund}), inception


def test_the_reconstruction_joins_the_real_series_without_a_step(monkeypatch) -> None:
    provider, inception = _synthetic_setup()
    monkeypatch.setitem(INCEPTION, "QLD", inception)

    synthetic = SyntheticLeveragedProvider(base=provider, drags={"QLD": 0.0})
    frame = synthetic.fetch("QLD", START, START + timedelta(days=399))

    real_first = provider.frames["QLD"].loc[inception]
    assert frame.loc[inception, "close"] == pytest.approx(real_first, rel=1e-9)

    # The day before the seam must be continuous with it, not a jump.
    before = frame.loc[frame.index < inception, "close"].iloc[-1]
    assert abs(before / real_first - 1.0) < 0.15


def test_reconstruction_only_covers_days_before_inception(monkeypatch) -> None:
    provider, inception = _synthetic_setup()
    monkeypatch.setitem(INCEPTION, "QLD", inception)

    frame = SyntheticLeveragedProvider(base=provider).fetch(
        "QLD", START, START + timedelta(days=399)
    )
    real = provider.frames["QLD"]
    for day in real.index:
        assert frame.loc[day, "close"] == pytest.approx(real.loc[day], rel=1e-9), day


def test_asking_only_for_real_history_skips_reconstruction(monkeypatch) -> None:
    provider, inception = _synthetic_setup()
    monkeypatch.setitem(INCEPTION, "QLD", inception)

    frame = SyntheticLeveragedProvider(base=provider).fetch(
        "QLD", inception, START + timedelta(days=399)
    )
    assert frame.index.min() == inception


def test_provenance_says_the_series_is_reconstructed(monkeypatch) -> None:
    provider, inception = _synthetic_setup()
    monkeypatch.setitem(INCEPTION, "QLD", inception)

    description = SyntheticLeveragedProvider(base=provider).describe("QLD")
    assert "reconstructed" in description.underlying_source
    assert "2x" in description.underlying_source
    assert description.source_ref == "synthetic:QLD"


def test_an_untouched_symbol_keeps_its_own_provenance() -> None:
    provider, _ = _synthetic_setup()
    description = SyntheticLeveragedProvider(base=provider).describe("QQQ")
    assert "reconstructed" not in description.underlying_source


def test_is_reconstructed_marks_the_right_days() -> None:
    assert is_reconstructed("TQQQ", date(2008, 10, 1))
    assert not is_reconstructed("TQQQ", date(2020, 3, 16))
    assert not is_reconstructed("QQQ", date(2020, 3, 16))


class _FakeSeriesProvider:
    """A SeriesProvider: FRED publishes one column, not OHLCV."""

    def __init__(self, series: Series) -> None:
        self.series = series

    def fetch(self, start: date, end: date) -> DataFrame:
        window = self.series[(self.series.index >= start) & (self.series.index <= end)]
        return window.to_frame("value")

    def describe(self) -> SourceDescription:
        return SourceDescription("FinanceDataReader", "0.9.202", "FRED index")


def test_the_splice_extends_the_index_backwards(monkeypatch) -> None:
    days = 400
    rng = np.random.default_rng(11)
    ndx = _prices(list(1000.0 * np.cumprod(1 + rng.normal(0.0004, 0.011, days))))
    inception = ndx.index[250]
    qqq = ndx.loc[inception:] / 40.0

    provider = _FakeProvider({"QQQ": qqq})
    index_provider = _FakeSeriesProvider(ndx)
    monkeypatch.setitem(INCEPTION, "QQQ", inception)

    frame = SplicedIndexProvider(base=provider, index_provider=index_provider).fetch(
        "QQQ", START, START + timedelta(days=399)
    )
    assert frame.index.min() == START
    assert frame.loc[inception, "close"] == pytest.approx(qqq.loc[inception], rel=1e-9)
    # The spliced segment should track the index it was built from.
    spliced = frame.loc[frame.index < inception, "close"]
    reference = ndx.loc[spliced.index]
    assert spliced.pct_change().corr(reference.pct_change()) == pytest.approx(1.0, abs=1e-6)


def test_tracking_report_measures_what_it_claims() -> None:
    base = _prices(list(100.0 * np.cumprod(1 + np.random.default_rng(7).normal(0, 0.01, 300))))
    identical = tracking_report(base, base.copy())
    assert identical["r_squared"] == pytest.approx(1.0)
    assert identical["cumulative_error"] == pytest.approx(0.0, abs=1e-12)

    drifting = base * pd.Series(
        np.linspace(1.0, 1.10, len(base)), index=base.index
    )
    report = tracking_report(base, drifting)
    assert report["cumulative_error"] == pytest.approx(0.10, abs=1e-9)


def test_tracking_needs_an_overlap() -> None:
    left = _prices([1.0, 2.0])
    right = _prices([1.0, 2.0], start=date(2010, 1, 1))
    with pytest.raises(ReconstructionError, match="overlap"):
        tracking_report(left, right)


# --------------------------------------------------------------- the real check


@pytest.mark.network
@pytest.mark.parametrize(("symbol", "leverage"), [("QLD", 2.0), ("TQQQ", 3.0)])
def test_the_reconstruction_still_tracks_the_real_fund(symbol: str, leverage: float) -> None:
    """Re-measure against the actual fund over its whole life.

    The thresholds are deliberately close to the measured values, so a change
    that degrades the model fails here rather than quietly producing a worse
    backtest.
    """
    import FinanceDataReader as fdr  # noqa: N813

    def close(sym: str, start: str) -> Series:
        frame = fdr.DataReader(sym, start, "2026-09-12")
        series = frame["Close"].dropna()
        series.index = pd.to_datetime(series.index).normalize()
        return series[~series.index.duplicated(keep="last")]

    qqq = close("QQQ", "1999-03-10")
    actual = close(symbol, INCEPTION[symbol].isoformat())
    rates = fdr.DataReader("FRED:DFF", "1999-01-01", "2026-09-12").iloc[:, 0].dropna()
    rates.index = pd.to_datetime(rates.index).normalize()

    returns = leveraged_returns(
        qqq,
        leverage=leverage,
        financing=daily_financing(rates, qqq.index),
        drag=MEASURED_DRAG[symbol],
    )
    shared = returns.dropna().index.intersection(actual.index)
    synthetic = (1 + returns.loc[shared]).cumprod()

    report = tracking_report(actual.loc[shared], synthetic)
    assert report["r_squared"] > 0.98, report
    assert abs(report["cumulative_error"]) < 0.15, report
    assert report["days"] > 4000, report

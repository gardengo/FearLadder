"""Reconstructed price history for periods the real instruments did not exist.

The strategy's most important calibration points are the dot-com crash
(2000-03 … 2002-10) and the financial crisis (2007-10 … 2009-03). Neither
leveraged sleeve existed then:

====  ==========  ==========================
Fund  Inception   Covers dot-com / crisis?
====  ==========  ==========================
QQQ   1999-03-10  yes / yes
QLD   2006-06-21  no  / yes
TQQQ  2010-02-11  no  / no
====  ==========  ==========================

Two reconstructions close the gap. Both are **models, not observations**, so
every row they produce is stored with ``quality_status = REVIEW`` and an
``underlying_source`` that says so.

1. :class:`SplicedIndexProvider` extends QQQ backwards with the Nasdaq-100 index
   it tracks, so indicator warm-up finishes long before the periods of interest.
2. :class:`SyntheticLeveragedProvider` builds QLD/TQQQ from QQQ's daily return
   using the standard replication identity::

       r_L = L · r_underlying − (L−1) · financing − drag

   The financing leg is the actual effective federal funds rate, not an
   assumption. ``drag`` covers the expense ratio and swap spread and is
   *measured* from the overlap with the real fund rather than guessed.

Why this is defensible here, specifically: every indicator in this system is
computed from QQQ. The reconstruction therefore never touches the regime signal
— it only prices the portfolio. A tracking error of a few percent changes the
NAV path; it cannot change which regime a day was in.

Measured against the real funds over their whole lives:

=====  ========  =========  ===================
Fund   Overlap   Daily R²   Cumulative error
=====  ========  =========  ===================
QLD    20.2 yr   0.990      −1.5%
TQQQ   16.6 yr   0.997      −8.3% (conservative)
=====  ========  =========  ===================

**Do not read those numbers as crash accuracy.** A fund's whole life is
overwhelmingly calm days. Measured on the one crash a leveraged sleeve lived
through at real prices — QLD, 2007-10-31 → 2009-03-09 — the model is at its
worst and its error points the optimistic way: R² 0.968, +8.5% cumulative over
340 days, an annualised drift of +6.2%/yr against −0.08%/yr over the whole life.
The window implies an all-in carry of 6.7%/yr where ``MEASURED_DRAG`` charges
0.68%, which is the swap spread a leveraged fund pays widening exactly when
funding freezes. In calm windows the implied drag is *negative*: one constant
fitted to a whole life flatters crashes and penalises quiet years.

It matters less than it sounds for this strategy's own drawdown — the trend
filter has cut leverage long before the error accumulates — but that is a fact
about the strategy, not about the model. ``docs/strategy.md`` §2.12 has the
measurement; ``scripts/reconstruction_accuracy.py`` reproduces it.

``tests/unit/test_synthetic.py`` re-measures the whole-life tracking, so a
provider change that degrades it fails the build.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from pandas import DataFrame, Series

from fear_ladder.data.collectors.base import (
    VALUE_COLUMN,
    CollectorError,
    DataUnavailableError,
    PriceProvider,
    SeriesProvider,
    SourceDescription,
    standardize_prices,
)

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252

#: Fund inception dates. Before these, any price is reconstructed.
INCEPTION: dict[str, date] = {
    "QQQ": date(1999, 3, 10),
    "QLD": date(2006, 6, 21),
    "TQQQ": date(2010, 2, 11),
}

#: All-in annual drag (expense ratio + swap spread) measured over each fund's
#: overlap with the model. Not published figures — fitted, then asserted by
#: ``tests/unit/test_synthetic.py`` so drift is caught.
MEASURED_DRAG: dict[str, float] = {
    "QLD": 0.0068,
    "TQQQ": 0.0016,
}

RECONSTRUCTED = "reconstructed"


class ReconstructionError(CollectorError):
    """Raised when a series cannot be reconstructed as configured."""


def _as_series(frame: DataFrame, column: str = "close") -> Series:
    if column not in frame.columns:
        raise ReconstructionError(f"frame has no {column!r} column: {list(frame.columns)}")
    return frame[column].dropna().astype("float64")


def daily_financing(rates: Series, index: pd.Index) -> Series:
    """Convert an annual percentage rate series into a daily decimal rate.

    Forward-filled onto the trading calendar: a rate published on Friday still
    applies over the weekend, and forward-filling a *past* value never looks
    ahead.
    """
    aligned = rates.reindex(index, method="ffill")
    return (aligned / 100.0) / TRADING_DAYS_PER_YEAR


def leveraged_returns(
    underlying: Series, *, leverage: float, financing: Series, drag: float
) -> Series:
    """``r_L = L·r − (L−1)·financing − drag/252``."""
    if leverage < 1:
        raise ReconstructionError(f"leverage must be >= 1, got {leverage}")
    returns = underlying.pct_change()
    aligned = financing.reindex(returns.index).fillna(0.0)
    return leverage * returns - (leverage - 1.0) * aligned - drag / TRADING_DAYS_PER_YEAR


def rebase(returns: Series, *, anchor_value: float, anchor_date: date) -> Series:
    """Turn returns into a price path that equals ``anchor_value`` on its anchor.

    The reconstruction runs *backwards* from the fund's real first price, so the
    synthetic history joins the real history without a step at the seam.
    """
    if anchor_date not in returns.index:
        raise ReconstructionError(f"anchor {anchor_date} is not in the return index")
    growth = (1.0 + returns.fillna(0.0)).cumprod()
    return growth / growth.loc[anchor_date] * anchor_value


@dataclass(frozen=True, slots=True)
class SplicedIndexProvider:
    """QQQ extended backwards with the index it tracks.

    QQQ is an NDX tracker, so using NDX before 1999-03-10 is the mildest
    possible reconstruction: same underlying, same constituents, differing only
    by fees and tracking error that are irrelevant to indicator *shape*.

    Its purpose is warm-up. Without it, a 3-year normalization window starting
    at QQQ's inception produces its first usable score in 2002 — after the
    dot-com peak the strategy most needs to see.
    """

    base: PriceProvider
    #: A *series* provider: FRED publishes one unnamed column, not OHLCV.
    index_provider: SeriesProvider
    index_symbol: str = "FRED:NASDAQ100"
    underlying_source: str = "Nasdaq-100 index, spliced before QQQ inception"

    def fetch(self, symbol: str, start: date, end: date) -> DataFrame:
        inception = INCEPTION.get(symbol)
        if inception is None or start >= inception:
            return self.base.fetch(symbol, start, end)

        actual = self.base.fetch(symbol, max(start, inception), end)
        if actual.empty:
            raise DataUnavailableError(f"{symbol}: no real history to splice onto")

        index = self.index_provider.fetch(start, inception)
        seam = actual.index[0]
        index_returns = _as_series(index, VALUE_COLUMN).pct_change()
        # Only the segment strictly before the seam is reconstructed.
        prior = index_returns[index_returns.index < seam]
        if prior.empty:
            logger.warning("%s: no index history before %s; returning actual only", symbol, seam)
            return actual

        anchor = float(actual["close"].iloc[0])
        growth = (1.0 + prior.fillna(0.0)).cumprod()
        # Walk backwards from the seam so the two segments meet exactly.
        reconstructed = anchor * growth / growth.iloc[-1] * (1.0 + prior.iloc[-1]) ** -1

        synthetic = DataFrame(
            {
                "open": pd.NA,
                "high": pd.NA,
                "low": pd.NA,
                "close": reconstructed,
                "adj_close": reconstructed,
                "volume": pd.NA,
            }
        )
        combined = pd.concat([synthetic, actual])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        logger.info(
            "%s: spliced %d reconstructed days before %s", symbol, len(synthetic), seam
        )
        return standardize_prices(combined, symbol=symbol)

    def describe(self, symbol: str) -> SourceDescription:
        actual = self.base.describe(symbol)
        return SourceDescription(
            provider_library=actual.provider_library,
            provider_library_version=actual.provider_library_version,
            underlying_source=f"{actual.underlying_source} + {self.underlying_source}",
            source_ref=f"spliced:{symbol}",
        )


@dataclass(frozen=True, slots=True)
class SyntheticLeveragedProvider:
    """QLD / TQQQ extended backwards from QQQ, financing and fees included.

    The reconstruction is anchored on the fund's own first real price and runs
    backwards, so the synthetic and real segments join without a discontinuity.
    """

    base: PriceProvider
    underlying_symbol: str = "QQQ"
    leverages: dict[str, float] = field(
        default_factory=lambda: {"QLD": 2.0, "TQQQ": 3.0}
    )
    drags: dict[str, float] = field(default_factory=lambda: dict(MEASURED_DRAG))
    #: Annual percentage financing rate, indexed by date. Supplied by the caller
    #: so the real fed funds series can be injected rather than assumed.
    financing_rates: Series | None = None

    def fetch(self, symbol: str, start: date, end: date) -> DataFrame:
        leverage = self.leverages.get(symbol)
        inception = INCEPTION.get(symbol)
        if leverage is None or inception is None or start >= inception:
            return self.base.fetch(symbol, start, end)

        actual = self.base.fetch(symbol, max(start, inception), end)
        if actual.empty:
            raise DataUnavailableError(f"{symbol}: no real history to anchor on")

        underlying = _as_series(self.base.fetch(self.underlying_symbol, start, end))
        seam = actual.index[0]
        if underlying.index.min() > seam:
            raise ReconstructionError(
                f"{symbol}: underlying {self.underlying_symbol} starts "
                f"{underlying.index.min()}, after the anchor {seam}"
            )

        financing = (
            daily_financing(self.financing_rates, underlying.index)
            if self.financing_rates is not None
            else Series(0.0, index=underlying.index)
        )
        if self.financing_rates is None:
            logger.warning(
                "%s: reconstructing without a financing rate. This overstates the "
                "leveraged sleeve, especially through high-rate periods.",
                symbol,
            )

        returns = leveraged_returns(
            underlying,
            leverage=leverage,
            financing=financing,
            drag=self.drags.get(symbol, 0.0),
        )
        if seam not in returns.index:
            raise ReconstructionError(f"{symbol}: anchor {seam} missing from the underlying")

        path = rebase(returns, anchor_value=float(actual["close"].iloc[0]), anchor_date=seam)
        prior = path[path.index < seam]
        if prior.empty:
            return actual

        synthetic = DataFrame(
            {
                "open": pd.NA,
                "high": pd.NA,
                "low": pd.NA,
                "close": prior,
                "adj_close": prior,
                "volume": pd.NA,
            }
        )
        combined = pd.concat([synthetic, actual])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        logger.info(
            "%s: reconstructed %d days before %s at %.1fx (drag %.2f%%/yr)",
            symbol,
            len(synthetic),
            seam,
            leverage,
            self.drags.get(symbol, 0.0) * 100,
        )
        return standardize_prices(combined, symbol=symbol)

    def describe(self, symbol: str) -> SourceDescription:
        actual = self.base.describe(symbol)
        leverage = self.leverages.get(symbol)
        if leverage is None:
            return actual
        return SourceDescription(
            provider_library=actual.provider_library,
            provider_library_version=actual.provider_library_version,
            underlying_source=(
                f"{actual.underlying_source} + {RECONSTRUCTED} {leverage:g}x of "
                f"{self.underlying_symbol} (financing: effective fed funds, "
                f"drag {self.drags.get(symbol, 0.0) * 100:.2f}%/yr)"
            ),
            source_ref=f"synthetic:{symbol}",
        )


def is_reconstructed(symbol: str, observation_date: date) -> bool:
    """Whether a given day's price for ``symbol`` is modelled rather than observed."""
    inception = INCEPTION.get(symbol)
    return inception is not None and observation_date < inception


def tracking_report(actual: Series, synthetic: Series) -> dict[str, float]:
    """Compare a reconstruction with the real series over their overlap."""
    shared = actual.dropna().index.intersection(synthetic.dropna().index)
    if len(shared) < 2:
        raise ReconstructionError("not enough overlap to measure tracking")

    real = actual.loc[shared]
    model = synthetic.loc[shared]
    real_returns = real.pct_change().dropna()
    model_returns = model.pct_change().dropna()
    shared_returns = real_returns.index.intersection(model_returns.index)

    correlation = float(
        real_returns.loc[shared_returns].corr(model_returns.loc[shared_returns])
    )
    real_growth = float(real.iloc[-1] / real.iloc[0])
    model_growth = float(model.iloc[-1] / model.iloc[0])
    return {
        "days": float(len(shared)),
        "r_squared": correlation**2,
        "cumulative_error": model_growth / real_growth - 1.0,
        "max_path_error": float(
            ((model / model.iloc[0]) / (real / real.iloc[0]) - 1.0).abs().max()
        ),
    }

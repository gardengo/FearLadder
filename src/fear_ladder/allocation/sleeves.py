"""Turning a target leverage into a portfolio, and back.

This is a domain rule, not a research choice, so it lives on the production side
of the research/production boundary (``BACKTEST_SPEC.md`` §28). Both the
allocation ladder (research) and the trend filter (production) need it.

The mapping uses **adjacent sleeves** — never more than two, always neighbours
on the leverage ladder:

===============  =========================
Target leverage  Portfolio
===============  =========================
0 ≤ L ≤ 1        QQQ L, Cash 1−L
1 ≤ L ≤ 2        QLD L−1, QQQ 2−L
2 ≤ L ≤ 3        TQQQ L−2, QLD 3−L
===============  =========================

Any target leverage has infinitely many portfolios; this rule picks one, and it
picks the one a person would describe in a sentence. 1.5x is "half QQQ, half
QLD", not "a bit of TQQQ and a lot of cash" — even though both reach 1.5x.
Explainability is the whole reason: an alert has to justify itself
(``CONTRIBUTING.md`` §12).
"""

from __future__ import annotations

from collections.abc import Mapping

from fear_ladder.constants import ASSET_LEVERAGE, Asset

WEIGHT_TOLERANCE = 1e-9
MIN_LEVERAGE = 0.0
MAX_LEVERAGE = 3.0


class SleeveError(ValueError):
    """Raised when a leverage target cannot be expressed as a portfolio."""


def portfolio_for(leverage: float) -> dict[Asset, float]:
    """The unique adjacent-sleeve portfolio with this target leverage."""
    if not MIN_LEVERAGE <= leverage <= MAX_LEVERAGE:
        raise SleeveError(
            f"target leverage {leverage} is outside [{MIN_LEVERAGE}, {MAX_LEVERAGE}]"
        )

    if leverage <= 1.0:
        weights = {Asset.QQQ: leverage, Asset.CASH: 1.0 - leverage}
    elif leverage <= 2.0:
        weights = {Asset.QLD: leverage - 1.0, Asset.QQQ: 2.0 - leverage}
    else:
        weights = {Asset.TQQQ: leverage - 2.0, Asset.QLD: 3.0 - leverage}

    return {
        asset: round(weight, 10)
        for asset, weight in weights.items()
        if weight > WEIGHT_TOLERANCE
    }


def leverage_of(weights: Mapping[Asset, float]) -> float:
    """``L = 1·w_QQQ + 2·w_QLD + 3·w_TQQQ`` (``BACKTEST_SPEC.md`` §14)."""
    return sum(ASSET_LEVERAGE[asset] * weight for asset, weight in weights.items())


def market_exposure(weights: Mapping[Asset, float]) -> float:
    """Share of capital not held in cash."""
    return 1.0 - weights.get(Asset.CASH, 0.0)

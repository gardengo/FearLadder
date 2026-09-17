"""Measuring a reconstructed price series against the fund it models.

:mod:`fear_ladder.data.collectors.synthetic` *builds* the reconstruction. This
module asks how well it did, which is a research question and lives on the
research path (``BACKTEST_SPEC.md`` §28 — nothing under
:mod:`fear_ladder.pipeline` may import this).

Two pieces, both used by ``scripts/reconstruction_accuracy.py`` (TASK-181) and
``scripts/cross_market.py`` (TASK-182):

:func:`model_path`
    The model rebuilt over a window and anchored on that window's first real
    price, so the error measured is the one accumulated *inside* the window
    rather than one inherited from before it.

:func:`implied_drag`
    The inverse. ``MEASURED_DRAG`` is one constant fitted over a fund's whole
    life; this solves for the constant a single window implies. The gap between
    the two is what turns an unexplained tracking error into a statement about
    carry — and it is also how a fund the project has never modelled before
    (SSO, UPRO) gets a drag at all.
"""

from __future__ import annotations

from pandas import Series

from fear_ladder.data.collectors.synthetic import leveraged_returns, rebase

#: Widest carry either direction that is worth calling a carry story at all.
DRAG_BRACKET = (-0.50, 1.50)


def model_path(
    underlying: Series, real: Series, *, leverage: float, financing: Series, drag: float
) -> Series:
    """The reconstruction of ``real``, anchored on its first observed price.

    ``underlying`` must extend at least one trading day before ``real`` begins,
    or the first daily return is undefined and the anchor has nothing to stand
    on.
    """
    returns = leveraged_returns(underlying, leverage=leverage, financing=financing, drag=drag)
    anchor = real.index[0]
    path = rebase(returns, anchor_value=float(real.iloc[0]), anchor_date=anchor)
    return path.loc[anchor : real.index[-1]]


def implied_drag(
    underlying: Series,
    real: Series,
    *,
    leverage: float,
    financing: Series,
    bracket: tuple[float, float] = DRAG_BRACKET,
    iterations: int = 60,
) -> float | None:
    """The constant annual drag that makes the model end where the fund did.

    Growth falls monotonically as drag rises, so a bisection is exact to
    whatever precision ``iterations`` buys. ``None`` means the answer lies
    outside ``bracket`` — the divergence is not a carry story and should not be
    reported as one.
    """
    target = float(real.iloc[-1] / real.iloc[0])

    def growth(drag: float) -> float:
        path = model_path(underlying, real, leverage=leverage, financing=financing, drag=drag)
        return float(path.iloc[-1] / path.iloc[0])

    low, high = bracket
    if growth(low) < target or growth(high) > target:
        return None
    for _ in range(iterations):
        middle = (low + high) / 2.0
        if growth(middle) > target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0

"""Build a regime → allocation ladder from its two endpoints.

The operator states the ladder as two anchors and lets the stages in between be
filled mechanically:

* most fearful regime — the dot-com / financial-crisis kind of decline
* most greedy regime — peak mania

Everything between is a straight line **in target leverage**, which is the
quantity the strategy actually reasons about. Interpolating portfolio weights
instead would let a blend invent positions nobody chose (TQQQ appearing in a
mildly fearful regime, say).

Turning a leverage number back into a portfolio uses the *adjacent sleeve* rule:

===============  =========================
Target leverage  Portfolio
===============  =========================
0 ≤ L ≤ 1        QQQ L, Cash 1−L
1 ≤ L ≤ 2        QLD L−1, QQQ 2−L
2 ≤ L ≤ 3        TQQQ L−2, QLD 3−L
===============  =========================

One sleeve up, one sleeve down, never more. That makes the ladder monotone,
unique, and explainable in a sentence — which matters when an alert has to
justify itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from regime_monitor.constants import ASSET_LEVERAGE, Asset

WEIGHT_TOLERANCE = 1e-9
MAX_LEVERAGE = 3.0


class LadderError(ValueError):
    """Raised when a ladder cannot be built as specified."""


def portfolio_for(leverage: float) -> dict[Asset, float]:
    """The unique adjacent-sleeve portfolio with this target leverage."""
    if not 0.0 <= leverage <= MAX_LEVERAGE:
        raise LadderError(f"target leverage {leverage} is outside [0, {MAX_LEVERAGE}]")

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


def leverage_of(weights: dict[Asset, float]) -> float:
    return sum(ASSET_LEVERAGE[asset] * weight for asset, weight in weights.items())


def market_exposure(weights: dict[Asset, float]) -> float:
    return 1.0 - weights.get(Asset.CASH, 0.0)


@dataclass(frozen=True, slots=True)
class LadderSpec:
    """The operator's two anchors."""

    #: Leverage at the most fearful regime.
    fear_leverage: float = 3.0
    #: Leverage at the most greedy regime.
    greed_leverage: float = 0.5

    def __post_init__(self) -> None:
        if self.fear_leverage <= self.greed_leverage:
            raise LadderError(
                "the fearful end must carry more leverage than the greedy end; "
                f"got {self.fear_leverage} vs {self.greed_leverage}"
            )
        for value in (self.fear_leverage, self.greed_leverage):
            if not 0.0 <= value <= MAX_LEVERAGE:
                raise LadderError(f"leverage {value} is outside [0, {MAX_LEVERAGE}]")
        if self.greed_leverage <= 0.0:
            # PRD.md 2.1 — never a full exit, however overheated.
            raise LadderError(
                "the greedy end must keep some market exposure (PRD.md 2.1)"
            )


def build_ladder(
    labels: Sequence[str], spec: LadderSpec | None = None
) -> dict[str, dict[Asset, float]]:
    """Regime → weights, interpolating leverage linearly between the anchors.

    ``labels`` runs fear → greed, matching ``regime.labels``.
    """
    resolved = spec or LadderSpec()
    count = len(labels)
    if count < 2:
        raise LadderError("a ladder needs at least two regimes")

    span = resolved.fear_leverage - resolved.greed_leverage
    ladder: dict[str, dict[Asset, float]] = {}
    for index, label in enumerate(labels):
        position = index / (count - 1)  # 0 at the fearful end
        leverage = resolved.fear_leverage - span * position
        ladder[label] = portfolio_for(round(leverage, 10))
    return ladder


def describe(ladder: dict[str, dict[Asset, float]]) -> str:
    rows = []
    for label, weights in ladder.items():
        holdings = ", ".join(
            f"{asset.value} {weight:.0%}"
            for asset, weight in sorted(weights.items(), key=lambda item: -item[1])
        )
        rows.append(f"  {label:16s} {leverage_of(weights):.2f}x  {holdings}")
    return "\n".join(rows)


def default_labels(count: int) -> tuple[str, ...]:
    """Conventional fear → greed names for the supported stage counts.

    Only the *names* are fixed here; how many stages to use and where the
    boundaries sit remain research parameters (TASK-082, TASK-083).
    """
    names = {
        3: ("Fear", "Neutral", "Greed"),
        5: ("Capitulation", "Fear", "Neutral", "Bull", "Overheated"),
        7: (
            "Capitulation",
            "ExtremeFear",
            "Fear",
            "Neutral",
            "Bull",
            "Overheated",
            "Bubble",
        ),
        9: (
            "Capitulation",
            "Panic",
            "ExtremeFear",
            "Fear",
            "Neutral",
            "Bull",
            "Overheated",
            "Euphoria",
            "Bubble",
        ),
    }
    try:
        return names[count]
    except KeyError as exc:
        raise LadderError(f"no conventional labels for {count} stages") from exc

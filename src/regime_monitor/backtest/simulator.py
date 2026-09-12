"""Daily NAV simulator (TASK-070, TASK-071, TASK-073).

The execution rule is the part worth reading carefully, because
``BACKTEST_SPEC.md`` 5 makes it a hard constraint rather than a preference::

    t일 시장 종료 → t일 지표 계산 → t일 Regime 결정 → t+1 거래일 실행

So a target produced *using data through day t's close* can only be traded on
day t+1. The simulator never reads a target on the day it was produced, and
``ExecutionTiming`` chooses where on t+1 the trade lands:

``NEXT_OPEN``
    Rebalance at t+1's open. The overnight move is earned at the old weights and
    the rest of t+1 at the new ones.
``NEXT_CLOSE``
    Rebalance at t+1's close, so the whole of t+1 is earned at the old weights.

Between rebalances the book **drifts** with the market rather than being held at
constant weights. That is what actually happens to a portfolio nobody trades,
and it matters here: ``BACKTEST_SPEC.md`` 16 only rebalances on a regime change,
so the book can drift for months.

Cash earns nothing. Modelling a cash rate would be a strategy assumption the
documents do not make, and leaving it at zero is the conservative direction for
a strategy whose overheated regimes hold cash.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from pandas import DataFrame, Series

from regime_monitor.backtest.costs import CostModel, turnover_between
from regime_monitor.constants import ASSET_LEVERAGE, Asset, ExecutionTiming

logger = logging.getLogger(__name__)

Weights = Mapping[Asset, float]
WEIGHT_TOLERANCE = 1e-9


class SimulationError(ValueError):
    """Raised when a simulation cannot be run as specified."""


@dataclass(frozen=True, slots=True)
class Trade:
    """One executed rebalance."""

    trade_date: date
    reason: str
    turnover: float
    cost: float
    weights_before: dict[Asset, float]
    weights_after: dict[Asset, float]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Everything one run produced (``BACKTEST_SPEC.md`` 26)."""

    name: str
    nav: Series
    weights: DataFrame
    trades: tuple[Trade, ...]
    turnover: Series
    costs: Series
    target_leverage: Series
    execution_timing: ExecutionTiming
    cost_model: CostModel

    @property
    def returns(self) -> Series:
        return self.nav.pct_change().dropna()

    @property
    def total_turnover(self) -> float:
        return float(self.turnover.sum())

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    def trades_frame(self) -> DataFrame:
        if not self.trades:
            return DataFrame(
                columns=["trade_date", "reason", "turnover", "cost", "target_leverage"]
            ).set_index("trade_date")
        return DataFrame(
            [
                {
                    "trade_date": trade.trade_date,
                    "reason": trade.reason,
                    "turnover": trade.turnover,
                    "cost": trade.cost,
                    "target_leverage": _leverage(trade.weights_after),
                    **{
                        f"w_{asset.value}": trade.weights_after.get(asset, 0.0)
                        for asset in Asset
                    },
                }
                for trade in self.trades
            ]
        ).set_index("trade_date")


@dataclass(frozen=True, slots=True)
class PortfolioSimulator:
    """Simulates daily NAV for a stream of target allocations."""

    closes: DataFrame
    opens: DataFrame | None = None
    cost_model: CostModel = field(default_factory=CostModel.zero)
    execution_timing: ExecutionTiming = ExecutionTiming.NEXT_OPEN
    initial_nav: float = 1.0
    #: Annualised percent (5.0 = 5%) earned by the cash sleeve, by date. None
    #: leaves cash at zero.
    cash_rates: Series | None = None

    def __post_init__(self) -> None:
        if self.closes.empty:
            raise SimulationError("no price history to simulate")
        if not self.closes.index.is_monotonic_increasing:
            raise SimulationError("close prices must be sorted by date")
        if self.closes.index.has_duplicates:
            raise SimulationError("close prices contain duplicate dates")
        if self.execution_timing is ExecutionTiming.NEXT_OPEN and self.opens is None:
            raise SimulationError(
                "NEXT_OPEN execution needs open prices; pass opens= or use NEXT_CLOSE"
            )
        if self.opens is not None and not self.opens.index.equals(self.closes.index):
            raise SimulationError("opens and closes must share an index")
        if self.cash_rates is not None and not self.cash_rates.index.is_monotonic_increasing:
            raise SimulationError("cash rates must be sorted by date")

    # -- public API --------------------------------------------------------
    def run(
        self,
        targets: Mapping[date, Weights],
        *,
        name: str = "strategy",
        force_rebalance_dates: Iterable[date] = (),
    ) -> BacktestResult:
        """Simulate NAV for targets keyed by the date they were *decided*.

        ``force_rebalance_dates`` re-executes an unchanged target, which is how
        a calendar-rebalanced benchmark is expressed without a second code path.
        """
        self._reject_unknown_assets(targets)
        forced = set(force_rebalance_dates)
        days = list(self.closes.index)

        nav = self.initial_nav
        book: dict[Asset, float] | None = None
        last_target: dict[Asset, float] | None = None
        trades: list[Trade] = []

        nav_history: list[float] = []
        weight_history: list[dict[Asset, float]] = []
        turnover_history: list[float] = []
        cost_history: list[float] = []

        for position, day in enumerate(days):
            day_turnover = 0.0
            day_cost = 0.0

            # The only target readable today is one decided on an earlier day.
            pending = self._pending_target(targets, days, position)

            if self.execution_timing is ExecutionTiming.NEXT_OPEN:
                if position > 0 and book is not None:
                    overnight = self._returns(
                        self.opens, days[position], self.closes, days[position - 1]
                    )
                    nav, book = _apply(nav, book, overnight)

                if pending is not None and self._should_trade(
                    pending, last_target, day, forced
                ):
                    book, day_turnover, day_cost, trade = self._rebalance(
                        day, book, pending, nav
                    )
                    nav *= 1.0 - day_cost
                    trades.append(trade)
                    last_target = dict(pending)

                if book is not None:
                    intraday = self._returns(
                        self.closes, days[position], self.opens, days[position]
                    )
                    nav, book = _apply(nav, book, intraday)
            else:
                if position > 0 and book is not None:
                    daily = self._returns(
                        self.closes, days[position], self.closes, days[position - 1]
                    )
                    nav, book = _apply(nav, book, daily)

                if pending is not None and self._should_trade(
                    pending, last_target, day, forced
                ):
                    book, day_turnover, day_cost, trade = self._rebalance(
                        day, book, pending, nav
                    )
                    nav *= 1.0 - day_cost
                    trades.append(trade)
                    last_target = dict(pending)

            nav_history.append(nav)
            weight_history.append(dict(book) if book else {})
            turnover_history.append(day_turnover)
            cost_history.append(day_cost)

        index = pd.Index(days, name="observation_date")
        weights = DataFrame(weight_history, index=index).fillna(0.0)
        for asset in Asset:
            if asset not in weights.columns:
                weights[asset] = 0.0
        weights = weights[list(Asset)]

        return BacktestResult(
            name=name,
            nav=Series(nav_history, index=index, name="nav"),
            weights=weights,
            trades=tuple(trades),
            turnover=Series(turnover_history, index=index, name="turnover"),
            costs=Series(cost_history, index=index, name="cost"),
            target_leverage=Series(
                [_leverage(row) for row in weight_history], index=index, name="leverage"
            ),
            execution_timing=self.execution_timing,
            cost_model=self.cost_model,
        )

    # -- internals ---------------------------------------------------------
    def _pending_target(
        self,
        targets: Mapping[date, Weights],
        days: list[date],
        position: int,
    ) -> Weights | None:
        """The most recent target decided strictly before ``days[position]``.

        This single lookup is what enforces ``BACKTEST_SPEC.md`` 5: today's own
        target is invisible today, so same-day execution is not expressible.
        """
        if position == 0:
            return None
        for earlier in reversed(days[:position]):
            if earlier in targets:
                return targets[earlier]
        return None

    def _should_trade(
        self,
        pending: Weights,
        last_target: dict[Asset, float] | None,
        day: date,
        forced: set[date],
    ) -> bool:
        if day in forced:
            return True
        if last_target is None:
            return True
        # BACKTEST_SPEC.md 16: no regime change, no trade.
        return not _same_weights(pending, last_target)

    def _rebalance(
        self,
        day: date,
        book: dict[Asset, float] | None,
        target: Weights,
        nav: float,
    ) -> tuple[dict[Asset, float], float, float, Trade]:
        before = dict(book or {})
        turnover = turnover_between(before, target)
        cost = self.cost_model.cost_of(turnover)
        after = {asset: float(weight) for asset, weight in target.items()}
        trade = Trade(
            trade_date=day,
            reason="initial" if book is None else "target_change",
            turnover=turnover,
            cost=cost,
            weights_before=before,
            weights_after=after,
        )
        logger.debug("rebalance %s turnover=%.4f cost=%.6f nav=%.4f", day, turnover, cost, nav)
        return after, turnover, cost, trade

    def _returns(
        self, to_frame: DataFrame | None, to_day: date, from_frame: DataFrame | None, from_day: date
    ) -> dict[Asset, float]:
        assert to_frame is not None and from_frame is not None
        result: dict[Asset, float] = {}
        for column in self.closes.columns:
            asset = Asset(str(column))
            end = to_frame.at[to_day, column]
            begin = from_frame.at[from_day, column]
            if pd.isna(end) or pd.isna(begin) or begin == 0:
                # A gap in a sleeve's history is not a return; treat it as flat
                # rather than inventing a move.
                result[asset] = 0.0
            else:
                result[asset] = float(end) / float(begin) - 1.0
        result[Asset.CASH] = self._cash_return(from_day, to_day)
        return result

    def _cash_return(self, from_day: date, to_day: date) -> float:
        """What the cash sleeve earned between two trading days.

        The rate is the one quoted on ``from_day`` - the last one knowable when
        the period began - so no return is ever earned at a rate published
        after the fact. Accrual is ACT/365 over calendar days, which pays the
        weekend a bill actually earns and returns exactly the quoted rate over
        a full year.
        """
        if self.cash_rates is None:
            return 0.0
        quoted = self.cash_rates.get(from_day)
        if quoted is None or pd.isna(quoted):
            # An unknown rate earns nothing rather than carrying a stale one.
            return 0.0
        days = (to_day - from_day).days
        if days <= 0:
            return 0.0
        return float(quoted) / 100.0 * days / 365.0

    def _reject_unknown_assets(self, targets: Mapping[date, Weights]) -> None:
        priced = {Asset(str(column)) for column in self.closes.columns} | {Asset.CASH}
        for day, weights in targets.items():
            unknown = set(weights) - priced
            if unknown:
                raise SimulationError(
                    f"target on {day} references unpriced sleeves: "
                    f"{sorted(asset.value for asset in unknown)}"
                )
            total = sum(weights.values())
            if abs(total - 1.0) > 1e-6:
                raise SimulationError(f"target on {day} sums to {total!r}, not 1.0")


def _apply(
    nav: float, book: dict[Asset, float], returns: Mapping[Asset, float]
) -> tuple[float, dict[Asset, float]]:
    """Grow NAV by the book's return and let the weights drift."""
    growth = sum(book.get(asset, 0.0) * returns.get(asset, 0.0) for asset in book)
    factor = 1.0 + growth
    if factor <= 0:
        # A total wipeout is not representable as drifted weights; stop here
        # rather than producing nonsense.
        raise SimulationError("portfolio value reached zero or below")
    drifted = {
        asset: weight * (1.0 + returns.get(asset, 0.0)) / factor
        for asset, weight in book.items()
    }
    return nav * factor, drifted


def _same_weights(left: Weights, right: Weights) -> bool:
    assets = set(left) | set(right)
    return all(
        abs(left.get(asset, 0.0) - right.get(asset, 0.0)) <= WEIGHT_TOLERANCE
        for asset in assets
    )


def _leverage(weights: Mapping[Asset, float]) -> float:
    return sum(ASSET_LEVERAGE[asset] * weight for asset, weight in weights.items())

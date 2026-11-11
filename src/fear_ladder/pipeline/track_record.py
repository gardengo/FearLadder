"""The paper track record: the stored targets, followed, next to QQQ (TASK-187).

The worker already stores a target allocation every day and the closing prices
of the three sleeves. What it did not store is the one number those two imply —
what a portfolio that simply did as it was told would be worth — so the history
tab could show the score against QQQ but never the strategy against QQQ.

Three decisions shape this module.

**It is the backtest's accounting, not a second one.**
Every day is run through :class:`~fear_ladder.backtest.simulator.PortfolioSimulator`
with the frozen execution timing and cost model: a target decided at one close
is traded at the next open, the book drifts between regime changes, and each
trade pays the configured cost. The benchmark is the same simulator holding
100% QQQ, so both sides pay to get in and both enter at the same open. A number
here means what the same number means in ``reports/performance.json``.

**It is appended, never replayed.**
Everything else the worker computes is re-derived from the start of history on
every run (``ARCHITECTURE.md`` §4.1.1). This is the deliberate exception. Raw
prices are pruned to a five-year window (:mod:`fear_ladder.data.retention`), so
a record that was replayed would lose its beginning the day the prices behind
it were pruned. Instead each row carries what the next one needs — the drifted
book and the target it was last traded to — and a new day is computed from the
last stored row. Re-running a day is still a no-op: a stored day is skipped.

It also means the record cannot be improved after the fact. A state corrected
by a later re-run does not travel back into rows already written, which is the
property that makes it a record. :meth:`TrackRecorder.rebuild` exists for the
one script that rewrites history on purpose.

**A missing signal is a held position.**
A day with no stored target — the worker did not run, or recorded UNKNOWN —
changes nothing: the book is carried at whatever it last held. That is what a
person following the alerts would have done.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta

from pandas import DataFrame, Index, Series

from fear_ladder.backtest.costs import CostModel
from fear_ladder.backtest.simulator import PortfolioSimulator
from fear_ladder.config.schema import AppConfig
from fear_ladder.constants import TRADABLE_SYMBOLS, Asset
from fear_ladder.data.models import TrackRecordEntry

logger = logging.getLogger(__name__)

#: What "doing nothing clever" means here: QQQ, bought once and held.
BENCHMARK: dict[Asset, float] = {Asset.QQQ: 1.0}

#: How far back to look for the last published cash rate. The rate arrives a
#: day or two behind prices and skips holidays the market does not.
RATE_LOOKBACK_DAYS = 14


class TrackRecordError(RuntimeError):
    """Raised when the track record cannot be extended or rebuilt honestly."""


@dataclass(frozen=True, slots=True)
class TrackRecordUpdate:
    """What one call added to the record."""

    appended: int
    through: date | None
    note: str = ""

    def summary(self) -> str:
        if self.appended:
            return f"track record: +{self.appended} day(s), through {self.through}"
        return f"track record: unchanged ({self.note or 'nothing new'})"


@dataclass(frozen=True, slots=True)
class TrackRecorder:
    """Extends the paper track record from its last stored day."""

    config: AppConfig

    # -- public API --------------------------------------------------------
    def extend(self, uow: object, *, through: date) -> TrackRecordUpdate:
        """Append every trading day after the last stored one, up to ``through``.

        ``uow`` is an active unit of work. An empty record starts on the first
        day that has both a stored target and a price for every sleeve.
        """
        version = self.config.strategy.strategy_version
        tail: TrackRecordEntry | None = uow.track_record.latest(  # type: ignore[attr-defined]
            strategy_version=version
        )
        if tail is not None and tail.observation_date >= through:
            return TrackRecordUpdate(0, tail.observation_date, "already recorded")

        targets: dict[date, dict[Asset, float]] = uow.states.get_allocations(  # type: ignore[attr-defined]
            strategy_version=version,
            start=None if tail is None else tail.observation_date,
            end=through,
        )
        if tail is None and not targets:
            return TrackRecordUpdate(0, None, "no stored target to follow yet")

        start = min(targets) if tail is None else tail.observation_date
        closes, opens = _prices(uow.observations, start, through)  # type: ignore[attr-defined]

        if tail is None:
            inception = _inception(targets, closes)
            if inception is None:
                return TrackRecordUpdate(
                    0, None, "no day has both a target and a price for every sleeve"
                )
            closes = closes[closes.index >= inception]
        elif start not in closes.index:
            raise TrackRecordError(
                f"the record ends on {start}, but that day no longer has a price "
                "for every sleeve, so the next day's return cannot be measured "
                "from it"
            )
        elif len(closes) < 2:
            return TrackRecordUpdate(0, tail.observation_date, "no new trading day")
        opens = opens.reindex(closes.index)

        entries = self._simulate(uow, tail, targets, closes, opens)
        appended = uow.track_record.append(entries)  # type: ignore[attr-defined]
        return TrackRecordUpdate(appended, closes.index[-1])

    def rebuild(self, uow: object, *, through: date) -> TrackRecordUpdate:
        """Discard the stored record and derive it again from stored prices.

        For ``scripts/backfill_history.py``, which rewrites the targets the
        record was built from. Refuses when the raw prices no longer reach back
        to where the stored record begins: past that point the stored rows are
        the only copy, and a rebuild would silently shorten the record.
        """
        version = self.config.strategy.strategy_version
        stored: list[TrackRecordEntry] = uow.track_record.history(  # type: ignore[attr-defined]
            strategy_version=version
        )
        if stored:
            targets = uow.states.get_allocations(  # type: ignore[attr-defined]
                strategy_version=version, end=through
            )
            reachable = None
            if targets:
                closes, _ = _prices(uow.observations, min(targets), through)  # type: ignore[attr-defined]
                reachable = _inception(targets, closes)
            begins = stored[0].observation_date
            if reachable is None or reachable > begins:
                raise TrackRecordError(
                    f"the stored record begins {begins}, but stored prices and "
                    f"targets can only rebuild it from {reachable or 'nowhere'}. "
                    "Rebuilding would discard days that cannot be derived again."
                )
            uow.track_record.clear(strategy_version=version)  # type: ignore[attr-defined]
        return self.extend(uow, through=through)

    # -- internals ---------------------------------------------------------
    def _simulate(
        self,
        uow: object,
        tail: TrackRecordEntry | None,
        targets: dict[date, dict[Asset, float]],
        closes: DataFrame,
        opens: DataFrame,
    ) -> list[TrackRecordEntry]:
        strategy = self.config.strategy
        days = closes.index
        simulator = PortfolioSimulator(
            closes=closes,
            opens=opens,
            cost_model=CostModel.from_spec(strategy.cost_model),
            execution_timing=strategy.execution.timing,
            cash_rates=_cash_rates(uow.observations, self.config, days),  # type: ignore[attr-defined]
            initial_nav=1.0 if tail is None else tail.strategy_nav,
        )
        followed = simulator.run(
            targets,
            name="strategy",
            initial_book=None if tail is None else tail.holdings,
            initial_target=None if tail is None else tail.executed_target,
        )
        # The benchmark's one decision is made on the first day and executed on
        # the second, exactly like the strategy's first. Past that it is simply
        # carried, which the seeded book expresses.
        invested = tail is not None and not tail.is_inception
        held = replace(
            simulator, initial_nav=1.0 if tail is None else tail.benchmark_nav
        ).run(
            {days[0]: BENCHMARK},
            name="benchmark",
            initial_book=BENCHMARK if invested else None,
            initial_target=BENCHMARK if invested else None,
        )

        executed = None if tail is None else tail.executed_target
        traded = {trade.trade_date: trade.weights_after for trade in followed.trades}
        entries: list[TrackRecordEntry] = []
        for position, day in enumerate(days):
            executed = traded.get(day, executed)
            if tail is not None and position == 0:
                continue  # the stored day the run resumed from
            entries.append(
                TrackRecordEntry(
                    observation_date=day,
                    strategy_version=strategy.strategy_version,
                    strategy_nav=float(followed.nav.loc[day]),
                    benchmark_nav=float(held.nav.loc[day]),
                    holdings={
                        Asset(str(asset)): float(weight)
                        for asset, weight in followed.weights.loc[day].items()
                        if weight > 0
                    },
                    executed_target=None if executed is None else dict(executed),
                    turnover=float(followed.turnover.loc[day]),
                    cost=float(followed.costs.loc[day]),
                )
            )
        return entries


def _inception(targets: dict[date, dict[Asset, float]], closes: DataFrame) -> date | None:
    """The first day that has both something to follow and prices to follow it at."""
    return next((day for day in closes.index if day in targets), None)


def _prices(observations: object, start: date, end: date) -> tuple[DataFrame, DataFrame]:
    """Closes and opens for the three sleeves, on the days all three traded."""
    closes: dict[str, dict[date, float]] = {}
    opens: dict[str, dict[date, float]] = {}
    for symbol in TRADABLE_SYMBOLS:
        rows = observations.get_observations(symbol, start=start, end=end)  # type: ignore[attr-defined]
        for row in rows:
            if row.close is None:
                continue
            closes.setdefault(symbol, {})[row.observation_date] = float(row.close)
            # Not every source carries an open. Falling back to the close turns
            # that day's NEXT_OPEN trade into a NEXT_CLOSE one rather than
            # inventing a price — the same rule the backtest loader applies.
            opens.setdefault(symbol, {})[row.observation_date] = float(
                row.close if row.open is None else row.open
            )
    columns = list(TRADABLE_SYMBOLS)
    # A day one sleeve is missing is left out entirely: a return measured
    # across it is still correct, and a half-priced book is not.
    close_frame = DataFrame(closes).reindex(columns=columns).sort_index().dropna(how="any")
    open_frame = DataFrame(opens).reindex(columns=columns).reindex(close_frame.index)
    return close_frame, open_frame


def _cash_rates(observations: object, config: AppConfig, days: Index) -> Series | None:
    """The short rate the cash sleeve earns, as last published before each day.

    Carried forward on purpose. The backtest looks the rate up by exact date
    because its history is complete; here the latest rate is routinely a day or
    two behind the latest price, and an exact lookup would pay the cash sleeve
    nothing on precisely the days being recorded. The last published rate is
    still knowable when the period begins, so nothing leaks.
    """
    name = config.data_sources.cash_rate_series
    if name is None or len(days) == 0:
        return None
    frame = observations.get_series_frame(  # type: ignore[attr-defined]
        [name], start=days[0] - timedelta(days=RATE_LOOKBACK_DAYS), end=days[-1]
    )
    if name not in frame.columns:
        return None
    quoted = frame[name].dropna()
    if quoted.empty:
        logger.warning("cash rate series %s has no recent value; cash earns nothing", name)
        return None
    carried = quoted.reindex(quoted.index.union(days)).sort_index().ffill()
    return carried.reindex(days).dropna()

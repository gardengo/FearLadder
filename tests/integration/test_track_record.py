"""TASK-187 — the paper track record: the stored targets, followed, next to QQQ.

What is pinned here is the set of properties that make the record worth
reading: it is the backtest's accounting, it grows a day at a time without
changing what is already written, and it survives the pruning of the prices it
was built from.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from fear_ladder.constants import Asset
from fear_ladder.data.models import MarketState, Provenance, TargetAllocation
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork, create_database
from fear_ladder.pipeline.track_record import TrackRecorder, TrackRecordError
from tests.conftest import make_price_observation, make_scalar_observation

VERSION = "v0.0-placeholder"
START = date(2024, 3, 4)  # a Monday

QQQ_ONLY = {Asset.QQQ: 1.0}
LEVERED = {Asset.QLD: 0.75, Asset.QQQ: 0.25}
DEFENSIVE = {Asset.QQQ: 0.5, Asset.CASH: 0.5}


def _weekdays(count: int) -> list[date]:
    days: list[date] = []
    day = START
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _seed_prices(
    uow: SQLiteUnitOfWork, provenance: Provenance, days: list[date], *, seed: int = 11
) -> dict[date, float]:
    """Three sleeves that move together, 1x / 2x / 3x. Returns the QQQ closes."""
    daily = np.random.default_rng(seed).normal(0.0005, 0.012, len(days))
    series = {
        "QQQ": 400.0 * np.cumprod(1 + daily),
        "QLD": 80.0 * np.cumprod(1 + 2 * daily),
        "TQQQ": 60.0 * np.cumprod(1 + 3 * daily),
    }
    uow.observations.save_observations(
        [
            make_price_observation(symbol, day, float(values[index]), provenance)
            for symbol, values in series.items()
            for index, day in enumerate(days)
        ]
    )
    return {day: float(series["QQQ"][index]) for index, day in enumerate(days)}


def _decide(uow: SQLiteUnitOfWork, day: date, weights: dict[Asset, float]) -> None:
    """Store a day's state and target, as the worker's persistence stage would."""
    state = MarketState(
        observation_date=day, composite_score=50.0, regime="Neutral", strategy_version=VERSION
    )
    allocation = TargetAllocation(
        observation_date=day, weights=weights, strategy_version=VERSION, regime="Neutral"
    )
    uow.states.save_state(state, allocation)


@pytest.fixture
def recorder(placeholder_config) -> TrackRecorder:
    return TrackRecorder(placeholder_config)


@pytest.fixture
def cost_rate(placeholder_config) -> float:
    spec = placeholder_config.strategy.cost_model
    return (spec.commission_bps + spec.spread_bps + spec.slippage_bps) * 1e-4


# ------------------------------------------------------------------ starting


def test_nothing_is_recorded_before_there_is_a_target(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder
) -> None:
    days = _weekdays(5)
    _seed_prices(uow, provenance, days)

    update = recorder.extend(uow, through=days[-1])

    assert update.appended == 0
    assert uow.track_record.history(strategy_version=VERSION) == []


def test_the_record_starts_at_one_on_the_first_day_with_a_target(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder
) -> None:
    days = _weekdays(6)
    _seed_prices(uow, provenance, days)
    _decide(uow, days[2], QQQ_ONLY)  # two priced days before anything to follow

    recorder.extend(uow, through=days[-1])
    history = uow.track_record.history(strategy_version=VERSION)

    assert [entry.observation_date for entry in history] == days[2:]
    first = history[0]
    assert (first.strategy_nav, first.benchmark_nav) == (1.0, 1.0)
    assert first.is_inception
    assert first.holdings == {}, "a target decided today cannot be held today"


def test_the_first_trade_is_at_the_next_open_and_pays_its_cost(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder, cost_rate: float
) -> None:
    days = _weekdays(3)
    _seed_prices(uow, provenance, days)
    _decide(uow, days[0], QQQ_ONLY)

    recorder.extend(uow, through=days[-1])
    second = uow.track_record.history(strategy_version=VERSION)[1]

    # The fixture opens every sleeve 1% under its close, so buying at the open
    # and holding to the close earns 1/0.99, less the cost of the whole book.
    expected = (1.0 - cost_rate) / 0.99
    assert second.strategy_nav == pytest.approx(expected)
    assert second.turnover == pytest.approx(1.0)
    assert second.cost == pytest.approx(cost_rate)
    assert second.executed_target == QQQ_ONLY
    # Following a 100% QQQ target *is* the benchmark, entry cost included.
    assert second.benchmark_nav == pytest.approx(second.strategy_nav)


def test_the_benchmark_is_qqq_bought_once_and_held(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder
) -> None:
    days = _weekdays(12)
    closes = _seed_prices(uow, provenance, days)
    _decide(uow, days[0], LEVERED)
    _decide(uow, days[5], DEFENSIVE)

    recorder.extend(uow, through=days[-1])
    history = uow.track_record.history(strategy_version=VERSION)

    # From the day after it is bought, whatever the strategy trades.
    for previous, entry in pairwise(history[1:]):
        held = closes[entry.observation_date] / closes[previous.observation_date]
        assert entry.benchmark_nav / previous.benchmark_nav == pytest.approx(held)


# ----------------------------------------------------------------- extending


def _scripted(db: Path, provenance: Provenance, days: list[date]) -> None:
    """Prices for every day and targets on most of them, changing twice."""
    with SQLiteUnitOfWork(db) as uow:
        _seed_prices(uow, provenance, days)
        for index, day in enumerate(days):
            if index in {4, 5, 17}:
                continue  # the worker did not run
            weights = QQQ_ONLY if index < 8 else LEVERED if index < 20 else DEFENSIVE
            _decide(uow, day, weights)


def test_a_day_at_a_time_is_the_same_record_as_all_at_once(
    tmp_path: Path, provenance: Provenance, recorder: TrackRecorder
) -> None:
    """The property the design rests on: how often the worker runs is invisible."""
    days = _weekdays(30)
    at_once, daily = (create_database(tmp_path / name) for name in ("once.db", "daily.db"))
    for db in (at_once, daily):
        _scripted(db, provenance, days)

    with SQLiteUnitOfWork(at_once) as uow:
        recorder.extend(uow, through=days[-1])
        expected = uow.track_record.history(strategy_version=VERSION)
    with SQLiteUnitOfWork(daily) as uow:
        for day in days:
            recorder.extend(uow, through=day)
        stepped = uow.track_record.history(strategy_version=VERSION)

    assert len(expected) == len(days)
    assert stepped == expected
    assert sum(entry.turnover > 0 for entry in expected) == 3


def test_recording_a_day_twice_changes_nothing(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder
) -> None:
    days = _weekdays(8)
    _seed_prices(uow, provenance, days)
    _decide(uow, days[0], LEVERED)

    recorder.extend(uow, through=days[-1])
    before = uow.track_record.history(strategy_version=VERSION)
    again = recorder.extend(uow, through=days[-1])
    earlier = recorder.extend(uow, through=days[3])

    assert (again.appended, earlier.appended) == (0, 0)
    assert uow.track_record.history(strategy_version=VERSION) == before


def test_a_written_day_is_not_rewritten_by_a_later_correction(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder
) -> None:
    """A track record that later data can improve is not a track record."""
    days = _weekdays(10)
    _seed_prices(uow, provenance, days)
    _decide(uow, days[0], LEVERED)
    recorder.extend(uow, through=days[5])
    written = uow.track_record.history(strategy_version=VERSION)

    _decide(uow, days[2], DEFENSIVE)  # hindsight arrives for a day already recorded
    recorder.extend(uow, through=days[-1])
    history = uow.track_record.history(strategy_version=VERSION)

    assert history[: len(written)] == written
    assert len(history) == len(days)
    assert history[-1].executed_target == LEVERED, "the old target is still the one held"


def test_a_day_without_a_signal_holds_the_position(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder
) -> None:
    days = _weekdays(9)
    _seed_prices(uow, provenance, days)
    _decide(uow, days[0], LEVERED)  # and then the worker goes quiet

    recorder.extend(uow, through=days[-1])
    history = uow.track_record.history(strategy_version=VERSION)

    assert [entry.observation_date for entry in history] == days
    assert [entry.turnover > 0 for entry in history] == [False, True] + [False] * 7
    assert all(entry.executed_target == LEVERED for entry in history[1:])
    # Held, not rebalanced: the levered sleeve's share moves with the market.
    assert history[-1].holdings != history[1].holdings


def test_cash_earns_the_last_published_rate(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder
) -> None:
    """The rate arrives behind the price; the newest day must not earn nothing."""
    days = _weekdays(4)  # Monday to Thursday
    _seed_prices(uow, provenance, days)
    uow.observations.save_observations(
        [make_scalar_observation("CASH_RATE", days[0], 5.0, provenance)]
    )
    _decide(uow, days[0], {Asset.CASH: 1.0})

    recorder.extend(uow, through=days[-1])
    history = uow.track_record.history(strategy_version=VERSION)

    overnight = 0.05 / 365.0
    # In cash from Tuesday's open: Tuesday→Wednesday and Wednesday→Thursday.
    assert history[-1].strategy_nav == pytest.approx((1 + overnight) ** 2)
    assert history[-1].turnover == 0.0


# ------------------------------------------------- outliving the raw prices


def _prune_before(uow: SQLiteUnitOfWork, cutoff: date) -> None:
    uow.connection.execute(
        "DELETE FROM market_observations WHERE observation_date < ?", (cutoff.isoformat(),)
    )


def test_the_record_keeps_growing_after_its_prices_are_pruned(
    tmp_path: Path, provenance: Provenance, recorder: TrackRecorder
) -> None:
    """Why it is appended rather than replayed (data/retention.py)."""
    days = _weekdays(30)
    kept, pruned = (create_database(tmp_path / name) for name in ("kept.db", "pruned.db"))
    for db in (kept, pruned):
        _scripted(db, provenance, days)

    with SQLiteUnitOfWork(kept) as uow:
        recorder.extend(uow, through=days[-1])
        expected = uow.track_record.history(strategy_version=VERSION)
    with SQLiteUnitOfWork(pruned) as uow:
        recorder.extend(uow, through=days[14])
        _prune_before(uow, days[14])
        recorder.extend(uow, through=days[-1])
        survived = uow.track_record.history(strategy_version=VERSION)

    assert survived == expected


def test_a_rebuild_reproduces_the_stored_record(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder
) -> None:
    days = _weekdays(12)
    _seed_prices(uow, provenance, days)
    _decide(uow, days[0], LEVERED)
    _decide(uow, days[6], DEFENSIVE)
    recorder.extend(uow, through=days[-1])
    stored = uow.track_record.history(strategy_version=VERSION)

    update = recorder.rebuild(uow, through=days[-1])

    assert update.appended == len(days)
    assert uow.track_record.history(strategy_version=VERSION) == stored


def test_a_rebuild_refuses_to_shorten_the_record(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder
) -> None:
    days = _weekdays(12)
    _seed_prices(uow, provenance, days)
    for day in days:
        _decide(uow, day, LEVERED)
    recorder.extend(uow, through=days[-1])
    stored = uow.track_record.history(strategy_version=VERSION)
    _prune_before(uow, days[6])

    with pytest.raises(TrackRecordError, match="cannot be derived again"):
        recorder.rebuild(uow, through=days[-1])

    assert uow.track_record.history(strategy_version=VERSION) == stored


def test_a_stalled_record_whose_last_day_is_gone_says_so(
    uow: SQLiteUnitOfWork, provenance: Provenance, recorder: TrackRecorder
) -> None:
    days = _weekdays(12)
    _seed_prices(uow, provenance, days)
    _decide(uow, days[0], LEVERED)
    recorder.extend(uow, through=days[3])
    _prune_before(uow, days[6])

    # Guessing a return across the gap would be inventing one.
    with pytest.raises(TrackRecordError, match="no longer has a price"):
        recorder.extend(uow, through=days[-1])

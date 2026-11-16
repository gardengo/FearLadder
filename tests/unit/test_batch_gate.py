"""TASK-189 — which scheduled attempt gets to run the daily worker.

The workflow is scheduled several times a morning because GitHub starts
scheduled workflows hours late. These pin what must stay true once there is
more than one attempt: a day is run once, never before its inputs are all
there, and never not at all.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from pandas import DataFrame

from fear_ladder.config.loader import load_config
from fear_ladder.data.collectors.base import CollectorError
from fear_ladder.data.collectors.registry import ProviderRegistry
from fear_ladder.data.models import MarketState
from fear_ladder.data.repositories.connection import connect
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.pipeline.gate import (
    FeedProbe,
    decide,
    last_session_before,
    probe_feeds,
    recorded_through,
    same_day_sources,
)

MONDAY = date(2026, 11, 9)
TUESDAY = date(2026, 11, 10)
WEDNESDAY = date(2026, 11, 11)

VERSION = "v-test"


def _never() -> FeedProbe:
    raise AssertionError("this decision must not need the feeds")


def _feeds(**latest: date) -> FeedProbe:
    return FeedProbe(latest=latest)


# ------------------------------------------------------------ the calendar


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 11, 11), date(2026, 11, 10)),  # Wednesday -> Tuesday
        (date(2026, 11, 14), date(2026, 11, 13)),  # Saturday -> Friday
        (date(2026, 11, 15), date(2026, 11, 13)),  # Sunday -> Friday
        (date(2026, 11, 16), date(2026, 11, 13)),  # Monday -> Friday
    ],
)
def test_the_last_session_is_the_latest_weekday_before_today(
    today: date, expected: date
) -> None:
    assert last_session_before(today) == expected


def test_only_inputs_published_the_same_day_are_waited_for() -> None:
    """Waiting for a lagged series would mean never running early."""
    sources = same_day_sources(load_config().data_sources)

    assert set(sources) == {"QQQ", "QLD", "TQQQ", "VIX", "CNN_FEAR_GREED"}
    # AAII is weekly and the bill rate arrives a day late; breadth is disabled.
    assert not {"AAII_SENTIMENT", "CASH_RATE", "BREADTH_NDX"} & set(sources)


# ---------------------------------------------------------------- the rules


def test_a_manual_run_always_runs() -> None:
    decision = decide(
        today=WEDNESDAY, recorded=TUESDAY, manual=True, last_attempt=False, probe=_never
    )

    assert decision.run


@pytest.mark.parametrize("last_attempt", [False, True])
def test_a_recorded_day_is_not_run_again(last_attempt: bool) -> None:
    """Not even by the last attempt.

    The inputs for a day keep moving after it is first read — CNN's reading for
    2026-11-10 was 33.5 at 04:46 UTC and 37.1 at 13:19 — and alerts are
    deduplicated by date. A second run could change an announced signal with
    nobody told.
    """
    decision = decide(
        today=WEDNESDAY,
        recorded=TUESDAY,
        manual=False,
        last_attempt=last_attempt,
        probe=_never,
    )

    assert not decision.run
    assert str(TUESDAY) in decision.reason


def test_an_early_attempt_runs_once_every_same_day_input_has_the_new_day() -> None:
    decision = decide(
        today=WEDNESDAY,
        recorded=MONDAY,
        manual=False,
        last_attempt=False,
        # CNN stamps its live value with the current UTC date, so it can be a
        # day ahead of the prices. The day that counts is the one all carry.
        probe=lambda: _feeds(QQQ=TUESDAY, VIX=TUESDAY, CNN_FEAR_GREED=WEDNESDAY),
    )

    assert decision.run
    assert str(TUESDAY) in decision.reason


def test_an_early_attempt_waits_while_one_input_is_behind() -> None:
    """QQQ alone clears the coverage floor, so the pipeline would score the day.

    It would score it without VIX, and the day would then count as recorded.
    """
    decision = decide(
        today=WEDNESDAY,
        recorded=MONDAY,
        manual=False,
        last_attempt=False,
        probe=lambda: _feeds(QQQ=TUESDAY, VIX=MONDAY, CNN_FEAR_GREED=TUESDAY),
    )

    assert not decision.run
    assert "VIX" in decision.reason


def test_an_early_attempt_waits_when_a_feed_cannot_be_read() -> None:
    decision = decide(
        today=WEDNESDAY,
        recorded=MONDAY,
        manual=False,
        last_attempt=False,
        probe=lambda: FeedProbe(
            latest={"QQQ": TUESDAY}, unreadable={"CNN_FEAR_GREED": "HTTP 503"}
        ),
    )

    assert not decision.run
    assert "CNN_FEAR_GREED" in decision.reason


def test_the_last_attempt_runs_without_asking_the_feeds() -> None:
    """The run that happens on a holiday or with a feed down.

    Skipping it too would mean a feed that stopped publishing is never noticed:
    the staleness check lives in the pipeline and only speaks when it runs.
    """
    decision = decide(
        today=WEDNESDAY, recorded=MONDAY, manual=False, last_attempt=True, probe=_never
    )

    assert decision.run


def test_a_database_with_no_state_yet_runs_as_soon_as_the_feeds_agree() -> None:
    decision = decide(
        today=WEDNESDAY,
        recorded=None,
        manual=False,
        last_attempt=False,
        probe=lambda: _feeds(QQQ=TUESDAY, VIX=TUESDAY),
    )

    assert decision.run


def test_missed_days_do_not_confuse_it() -> None:
    """Recorded through Monday, asked on Saturday: Friday's data is new."""
    saturday = date(2026, 11, 14)
    friday = date(2026, 11, 13)

    decision = decide(
        today=saturday,
        recorded=MONDAY,
        manual=False,
        last_attempt=False,
        probe=lambda: _feeds(QQQ=friday, VIX=friday),
    )

    assert decision.run


# ---------------------------------------------------------------- the probe


class _Prices:
    def __init__(self, latest: dict[str, date]) -> None:
        self.latest = latest
        self.asked: list[tuple[str, date, date]] = []

    def fetch(self, symbol: str, start: date, end: date) -> DataFrame:
        self.asked.append((symbol, start, end))
        newest = self.latest[symbol]
        days = [day.date() for day in pd.bdate_range(end=newest, periods=3)]
        return DataFrame({"close": [1.0, 2.0, 3.0]}, index=days)


class _Series:
    def __init__(self, newest: date | None, error: Exception | None = None) -> None:
        self.newest = newest
        self.error = error

    def fetch(self, start: date, end: date) -> DataFrame:
        if self.error is not None:
            raise self.error
        return DataFrame({"value": [20.0]}, index=[self.newest])


def test_the_probe_reads_each_same_day_input_up_to_today() -> None:
    config = load_config().data_sources
    prices = _Prices({"QQQ": TUESDAY, "QLD": TUESDAY, "TQQQ": TUESDAY})
    registry = ProviderRegistry(
        price=prices,  # type: ignore[arg-type]
        series={"VIX": _Series(TUESDAY), "CNN_FEAR_GREED": _Series(WEDNESDAY)},  # type: ignore[dict-item]
        disabled={},
    )

    feeds = probe_feeds(config, registry, today=WEDNESDAY)

    assert feeds.complete_through == TUESDAY
    assert feeds.latest["CNN_FEAR_GREED"] == WEDNESDAY
    # The same end date the pipeline collects with, or the two could disagree
    # about whether the day is there.
    assert {end for _, _, end in prices.asked} == {WEDNESDAY}


def test_a_feed_that_fails_is_reported_not_raised() -> None:
    config = load_config().data_sources
    registry = ProviderRegistry(
        price=_Prices({"QQQ": TUESDAY, "QLD": TUESDAY, "TQQQ": TUESDAY}),  # type: ignore[arg-type]
        series={
            "VIX": _Series(TUESDAY),
            "CNN_FEAR_GREED": _Series(None, CollectorError("CNN endpoint returned HTTP 503")),
        },  # type: ignore[dict-item]
        disabled={},
    )

    feeds = probe_feeds(config, registry, today=WEDNESDAY)

    assert feeds.complete_through is None
    assert "503" in feeds.unreadable["CNN_FEAR_GREED"]


# ------------------------------------------------------ what counts as done


def test_a_data_failure_row_does_not_count_as_a_recorded_day(db_path: Path) -> None:
    """A failed run stores UNKNOWN dated the day it ran, which is after any session.

    Counting it would make every later attempt of that day stand down.
    """
    with SQLiteUnitOfWork(db_path) as uow:
        uow.states.save_state(
            MarketState(
                observation_date=TUESDAY,
                composite_score=40.0,
                regime="Fear",
                strategy_version=VERSION,
            ),
            None,
        )
        uow.states.save_state(
            MarketState.unknown(
                WEDNESDAY, strategy_version=VERSION, reason_codes=("STALE:QQQ",)
            ),
            None,
        )

    connection = connect(db_path, read_only=True)
    try:
        assert recorded_through(connection, VERSION) == TUESDAY
        assert recorded_through(connection, "some-other-version") is None
    finally:
        connection.close()


def test_a_database_without_the_table_has_nothing_recorded() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        assert recorded_through(connection, VERSION) is None
    finally:
        connection.close()

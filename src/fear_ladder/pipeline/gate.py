"""Whether a scheduled attempt has anything to do (TASK-189).

The daily workflow is scheduled several times each morning rather than once.
GitHub starts a scheduled workflow hours late — 3.9 to 8.4 hours across this
repository's 45 runs from 2026-09-15 to 2026-11-14, late enough that the
2026-11-12 run finished five minutes before the next US open — and the delay cannot be
configured away, only out-waited. More attempts is what makes one of them land
early.

More attempts must not mean more runs. A second run of a day that is already
recorded re-reads inputs that are still moving (CNN revises a day's reading for
a day or two afterwards) and could change a signal that has already been
announced, with no second alert because alerts are deduplicated by date. A run
before the feeds carry the new day is worse: QQQ alone clears the 60% coverage
floor, so a day could be scored without VIX and then counted as done. So every
attempt asks this module first.

The rules, in order:

1. A manual run always runs. Someone asked for it.
2. The last session is already recorded: nothing to do, whichever attempt this
   is.
3. The last attempt of the day runs regardless of what the feeds carry. This is
   the one run that happens on a holiday or when a feed is down, and it is what
   keeps a stuck feed visible: the pipeline's own freshness check only speaks
   when the pipeline runs.
4. An earlier attempt runs only when every same-day input carries a day newer
   than the recorded one. Otherwise it leaves the day to a later attempt.

"The last session" is the latest weekday before today. That is wrong on market
holidays, and deliberately so: the mistake costs one run that finds nothing new
(rule 3), which is what the single daily run did before this module existed. A
holiday calendar would save those few runs a year and add a table that goes
stale.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from fear_ladder.config.schema import DataSourcesConfig
from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.data.collectors.registry import ProviderRegistry

logger = logging.getLogger(__name__)

#: How far back a probe asks each feed. It only needs the newest row; ten
#: calendar days clears any run of holidays without downloading a history.
PROBE_LOOKBACK_DAYS = 10

_SATURDAY = 5


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether to run, and the sentence that goes in the workflow log."""

    run: bool
    reason: str


@dataclass(frozen=True, slots=True)
class FeedProbe:
    """The newest day each same-day input carries right now."""

    latest: Mapping[str, date] = field(default_factory=dict)
    unreadable: Mapping[str, str] = field(default_factory=dict)

    @property
    def complete_through(self) -> date | None:
        """The newest day every input carries, or ``None`` if one could not be read."""
        if self.unreadable or not self.latest:
            return None
        return min(self.latest.values())

    @property
    def laggard(self) -> str | None:
        """The input holding ``complete_through`` back."""
        if not self.latest:
            return None
        return min(self.latest, key=lambda name: self.latest[name])


def last_session_before(today: date) -> date:
    """The latest weekday strictly before ``today``.

    Strictly before, because that is all the price feed can return: it is asked
    for bars up to today's date and answers with those that began before
    00:00 UTC of it.
    """
    day = today - timedelta(days=1)
    while day.weekday() >= _SATURDAY:
        day -= timedelta(days=1)
    return day


def same_day_sources(config: DataSourcesConfig) -> tuple[str, ...]:
    """Inputs that are published for a session on the day of that session.

    Prices, and the series with no publication lag (VIX, CNN). The lagged ones
    — the weekly AAII survey, the bill rate — never carry the newest day, so
    waiting for them would mean never running early.
    """
    series = tuple(
        name
        for name, spec in config.series.items()
        if spec.enabled and spec.availability_lag_days == 0
    )
    return (*config.price.symbols, *series)


def probe_feeds(
    config: DataSourcesConfig,
    registry: ProviderRegistry,
    *,
    today: date,
    lookback_days: int = PROBE_LOOKBACK_DAYS,
) -> FeedProbe:
    """Ask each same-day input for its newest row, through the collectors.

    The same providers and the same end date the pipeline will use, so "the
    probe saw the day" and "the run will see the day" are one statement rather
    than two that happen to agree.
    """
    start = today - timedelta(days=lookback_days)
    latest: dict[str, date] = {}
    unreadable: dict[str, str] = {}
    for name in same_day_sources(config):
        try:
            if name in config.price.symbols:
                frame = registry.price.fetch(name, start, today)
            else:
                frame = registry.series_provider(name).fetch(start, today)
        except Exception as exc:
            # Deliberately broad. Whatever went wrong, the answer is the same:
            # this attempt cannot vouch for the day, and a later one will ask
            # again. Letting it propagate would turn a flaky feed into a run.
            unreadable[name] = str(exc)
            continue
        if frame.empty:
            unreadable[name] = "no rows"
            continue
        newest = frame.index.max()
        latest[name] = newest.date() if hasattr(newest, "date") else newest
    return FeedProbe(latest=latest, unreadable=unreadable)


def recorded_through(connection: sqlite3.Connection, strategy_version: str) -> date | None:
    """The latest day this strategy has a real state for.

    A DATA_FAILURE is stored as an UNKNOWN row dated the day of the run, not of
    a session. It records that a run failed, not that a day is done, so it does
    not count.
    """
    try:
        row = connection.execute(
            "SELECT MAX(observation_date) FROM market_states "
            "WHERE strategy_version = ? AND regime != ?",
            (strategy_version, UNKNOWN_REGIME),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # a database from before the first run
    return date.fromisoformat(row[0]) if row and row[0] else None


def decide(
    *,
    today: date,
    recorded: date | None,
    manual: bool,
    last_attempt: bool,
    probe: Callable[[], FeedProbe],
) -> GateDecision:
    """Apply the four rules. ``probe`` is only called when rule 4 needs it."""
    if manual:
        return GateDecision(True, "manual run")

    expected = last_session_before(today)
    if recorded is not None and recorded >= expected:
        return GateDecision(False, f"{recorded} is already recorded; nothing to do")

    if last_attempt:
        return GateDecision(
            True,
            f"last attempt of the day and {expected} is not recorded; "
            "running on whatever the feeds carry",
        )

    feeds = probe()
    if feeds.unreadable:
        names = ", ".join(sorted(feeds.unreadable))
        return GateDecision(False, f"could not read {names}; leaving it to a later attempt")
    complete = feeds.complete_through
    if complete is None:
        return GateDecision(
            False, "no same-day input is configured; leaving it to the last attempt"
        )
    if recorded is not None and complete <= recorded:
        return GateDecision(
            False,
            f"{feeds.laggard} still ends at {complete}, which is already recorded; "
            "leaving it to a later attempt",
        )
    return GateDecision(True, f"every same-day input carries {complete}")

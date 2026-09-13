"""Replaying a past day must not look like corrupted data (TASK-133 follow-up).

``daily_runner.py --date`` promises a re-run of an earlier day is safe and
idempotent. It was not: the freshness check asked the repository for the newest
row in the table, saw rows dated after the day being replayed, and recorded a
DATA_FAILURE for every source. The cap is what makes an as-of run honest — it
may only know what was already observed on the day it is standing on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from regime_monitor.config.schema import (
    DataSourcesConfig,
    PriceSourceSpec,
    SymbolSpec,
)
from regime_monitor.constants import DataQualityStatus
from regime_monitor.data.validators.freshness import FreshnessValidator

TODAY = date(2026, 9, 11)
PAST = date(2022, 6, 15)


@dataclass
class FakeRepository:
    """Only the one method the freshness check uses."""

    dates: list[date]

    def latest_observation_date(
        self, symbol: str, *, on_or_before: date | None = None
    ) -> date | None:
        usable = [d for d in self.dates if on_or_before is None or d <= on_or_before]
        return max(usable) if usable else None


def _validator() -> FreshnessValidator:
    config = DataSourcesConfig(
        version=1,
        price=PriceSourceSpec(
            symbols={"QQQ": SymbolSpec(role="base", leverage=1.0)},
            start_date=date(1999, 3, 10),
            max_staleness_days=4,
            mandatory=True,
        ),
        series={},
    )
    return FreshnessValidator(config)


def _status(report, name: str = "QQQ") -> DataQualityStatus:
    return next(s.status for s in report.sources if s.name == name)


def test_replaying_a_past_day_is_not_corruption() -> None:
    repo = FakeRepository([PAST, TODAY])
    report = _validator().validate(repo, as_of=PAST, replaying=True)
    assert _status(report) is DataQualityStatus.OK
    assert report.usable


def test_future_dated_rows_are_still_corruption_for_todays_run() -> None:
    """The protection this replaces must survive: a source that hands back
    tomorrow's date on a live run is a clock or source error."""
    repo = FakeRepository([TODAY, date(2026, 9, 30)])
    report = _validator().validate(repo, as_of=TODAY)
    assert _status(report) is DataQualityStatus.CORRUPT
    assert not report.usable


def test_staleness_is_measured_from_the_newest_row_the_day_could_see() -> None:
    repo = FakeRepository([date(2022, 5, 1), PAST, TODAY])
    stale = _validator().validate(repo, as_of=date(2022, 6, 1), replaying=True)
    assert _status(stale) is DataQualityStatus.STALE  # 31 days behind 2022-05-01

    fresh = _validator().validate(repo, as_of=PAST, replaying=True)
    assert _status(fresh) is DataQualityStatus.OK


def test_a_source_with_nothing_yet_is_missing_not_corrupt() -> None:
    report = _validator().validate(FakeRepository([]), as_of=TODAY)
    assert _status(report) is DataQualityStatus.MISSING


def test_a_source_whose_history_starts_later_is_missing_on_a_replay() -> None:
    """Capped to a day before the source existed, there is nothing to read."""
    repo = FakeRepository([TODAY])
    report = _validator().validate(repo, as_of=PAST, replaying=True)
    assert _status(report) is DataQualityStatus.MISSING

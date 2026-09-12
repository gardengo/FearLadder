"""Data freshness and completeness checks (TASK-112).

``CLAUDE_CODE_INITIAL_PROMPT.md`` §14 lists the situations in which the system
must not produce a normal investment signal::

    mandatory data missing / stale data / corrupt data / date mismatch /
    indicator calculation failure

The judgement this module has to get right is "how old is too old?". Markets
close at weekends and on holidays, so a Monday run legitimately sees Friday's
data. Counting *calendar* days against a configured tolerance handles both
without needing a holiday calendar: the tolerance simply has to exceed a long
weekend, which the shipped configuration does.

Being wrong in the tolerant direction produces a stale signal; being wrong in
the strict direction produces a spurious DATA_FAILURE. The first is dangerous
and the second is merely noisy, so the thresholds here are deliberately tight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from regime_monitor.config.schema import DataSourcesConfig
from regime_monitor.constants import DataQualityStatus
from regime_monitor.data.interfaces import MarketObservationRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    """One source's verdict."""

    name: str
    mandatory: bool
    latest: date | None
    age_days: int | None
    max_staleness_days: int
    status: DataQualityStatus

    @property
    def ok(self) -> bool:
        return self.status is DataQualityStatus.OK

    def describe(self) -> str:
        if self.latest is None:
            return f"{self.name}: no data at all"
        return (
            f"{self.name}: latest {self.latest} ({self.age_days}d old, "
            f"limit {self.max_staleness_days}d) -> {self.status.value}"
        )


@dataclass(frozen=True, slots=True)
class FreshnessReport:
    """Whether today's inputs are good enough to act on."""

    as_of: date
    sources: tuple[SourceFreshness, ...] = ()

    @property
    def failures(self) -> tuple[SourceFreshness, ...]:
        return tuple(source for source in self.sources if not source.ok)

    @property
    def mandatory_failures(self) -> tuple[SourceFreshness, ...]:
        return tuple(source for source in self.failures if source.mandatory)

    @property
    def usable(self) -> bool:
        """A normal signal may only be produced when every mandatory input is fresh."""
        return not self.mandatory_failures

    def reason_codes(self) -> tuple[str, ...]:
        return tuple(
            f"{source.status.value}:{source.name}" for source in self.failures
        )

    def summary(self) -> str:
        if self.usable and not self.failures:
            return f"{self.as_of}: all {len(self.sources)} sources fresh"
        parts = [source.describe() for source in self.failures]
        verdict = "USABLE" if self.usable else "NOT USABLE"
        return f"{self.as_of}: {verdict}; " + "; ".join(parts)


@dataclass(frozen=True, slots=True)
class FreshnessValidator:
    """Checks every configured source against its staleness tolerance."""

    config: DataSourcesConfig
    #: Extra slack beyond the configured tolerance, in calendar days. Zero by
    #: default: the per-source values already account for weekends.
    grace_days: int = 0
    expected_sources: tuple[str, ...] = field(default_factory=tuple)

    def validate(
        self, repository: MarketObservationRepository, *, as_of: date
    ) -> FreshnessReport:
        results: list[SourceFreshness] = []

        for symbol in self.config.price.symbols:
            results.append(
                self._check(
                    repository,
                    symbol,
                    as_of=as_of,
                    mandatory=self.config.price.mandatory,
                    limit=self.config.price.max_staleness_days,
                )
            )

        for name, spec in self.config.series.items():
            if not spec.enabled:
                continue
            results.append(
                self._check(
                    repository,
                    name,
                    as_of=as_of,
                    mandatory=spec.mandatory,
                    limit=spec.max_staleness_days,
                )
            )

        report = FreshnessReport(as_of=as_of, sources=tuple(results))
        log = logger.warning if not report.usable else logger.info
        log("%s", report.summary())
        return report

    def _check(
        self,
        repository: MarketObservationRepository,
        name: str,
        *,
        as_of: date,
        mandatory: bool,
        limit: int,
    ) -> SourceFreshness:
        latest = repository.latest_observation_date(name)
        if latest is None:
            return SourceFreshness(
                name=name,
                mandatory=mandatory,
                latest=None,
                age_days=None,
                max_staleness_days=limit,
                status=DataQualityStatus.MISSING,
            )

        if latest > as_of:
            # A value dated in the future means the clock, the source or the
            # requested date is wrong. Acting on it would be acting on a
            # date mismatch (CLAUDE_CODE_INITIAL_PROMPT.md 14).
            return SourceFreshness(
                name=name,
                mandatory=mandatory,
                latest=latest,
                age_days=(latest - as_of).days * -1,
                max_staleness_days=limit,
                status=DataQualityStatus.CORRUPT,
            )

        age = (as_of - latest).days
        status = (
            DataQualityStatus.OK
            if age <= limit + self.grace_days
            else DataQualityStatus.STALE
        )
        return SourceFreshness(
            name=name,
            mandatory=mandatory,
            latest=latest,
            age_days=age,
            max_staleness_days=limit,
            status=status,
        )

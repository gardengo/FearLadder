"""Collection service — fetch configured sources into the repository.

Shared by the daily worker and by historical backfill, so both store data with
identical provenance and availability rules. What differs between them is only
the date window.

A failure here is reported, never papered over: the caller decides whether a
missing optional series costs one indicator or a missing mandatory one costs the
whole day (``BACKTEST_SPEC.md`` 7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from regime_monitor.config.schema import DataSourcesConfig
from regime_monitor.constants import DataQualityStatus
from regime_monitor.data.collectors.base import Collector, CollectorError
from regime_monitor.data.collectors.registry import ProviderRegistry, build_registry
from regime_monitor.data.interfaces import MarketObservationRepository
from regime_monitor.data.models import MarketObservation

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CollectionReport:
    """What one collection pass managed to fetch."""

    collected: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    def failed_mandatory(self, mandatory: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(name for name in mandatory if name in self.failures)

    def summary(self) -> str:
        rows = [f"{name}={count}" for name, count in sorted(self.collected.items())]
        text = f"collected {', '.join(rows) or 'nothing'}"
        if self.failures:
            text += f"; failed: {', '.join(sorted(self.failures))}"
        if self.skipped:
            text += f"; skipped: {', '.join(sorted(self.skipped))}"
        return text


@dataclass(frozen=True, slots=True)
class CollectionService:
    """Fetches every enabled source and writes it to the repository."""

    config: DataSourcesConfig
    registry: ProviderRegistry
    collector: Collector = field(default_factory=Collector)

    @classmethod
    def from_config(cls, config: DataSourcesConfig, **kwargs: object) -> CollectionService:
        return cls(config=config, registry=build_registry(config), **kwargs)  # type: ignore[arg-type]

    def collect(
        self,
        repository: MarketObservationRepository,
        *,
        start: date,
        end: date,
    ) -> CollectionReport:
        collected: dict[str, int] = {}
        failures: dict[str, str] = {}
        skipped: dict[str, str] = dict(self.registry.disabled)

        for symbol in self.config.price.symbols:
            try:
                observations = self._collect_prices(symbol, start, end)
            except CollectorError as exc:
                logger.warning("price collection failed for %s: %s", symbol, exc)
                failures[symbol] = str(exc)
                continue
            collected[symbol] = repository.save_observations(observations)

        for name, spec in self.config.series.items():
            if not spec.enabled:
                continue
            try:
                observations = self._collect_series(name, start, end)
            except CollectorError as exc:
                logger.warning("series collection failed for %s: %s", name, exc)
                failures[name] = str(exc)
                continue
            collected[name] = repository.save_observations(observations)

        report = CollectionReport(collected=collected, failures=failures, skipped=skipped)
        logger.info("%s", report.summary())
        return report

    # -- internals ---------------------------------------------------------
    def _collect_prices(self, symbol: str, start: date, end: date) -> list[MarketObservation]:
        spec = self.config.price
        inception = spec.symbols[symbol].inception
        # Asking before a fund existed is not a failure, it is arithmetic.
        effective_start = max(start, inception) if inception else start
        if effective_start > end:
            raise CollectorError(
                f"{symbol} did not exist before {inception}; nothing to collect "
                f"for {start}..{end}"
            )
        return self.collector.price_observations(
            self.registry.price,
            symbol,
            effective_start,
            end,
            attempts=spec.retry.attempts,
            backoff_seconds=spec.retry.backoff_seconds,
        )

    def _collect_series(self, name: str, start: date, end: date) -> list[MarketObservation]:
        spec = self.config.series[name]
        provider = self.registry.series_provider(name)
        quality = getattr(provider, "quality_status", None)
        return self.collector.series_observations(
            provider,
            name,
            start,
            end,
            availability_lag_days=spec.availability_lag_days,
            attempts=spec.retry.attempts,
            backoff_seconds=spec.retry.backoff_seconds,
            quality_status=(
                quality if isinstance(quality, DataQualityStatus) else spec.quality_status
            ),
        )

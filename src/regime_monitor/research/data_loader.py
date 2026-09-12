"""Load stored observations into backtest inputs.

The ``available_at`` cutoff is passed straight through to the repository, so a
walk-forward window can ask "what was knowable on this date?" and get exactly
that (``BACKTEST_SPEC.md`` 4, 19).
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from pandas import DataFrame

from regime_monitor.config.schema import AppConfig
from regime_monitor.constants import Asset
from regime_monitor.data.interfaces import MarketObservationRepository
from regime_monitor.research.backtest_runner import MarketData

logger = logging.getLogger(__name__)


def required_symbols(config: AppConfig) -> list[str]:
    """Every source an enabled indicator reads, plus the tradable sleeves."""
    sources = {spec.source for spec in config.indicators.enabled_indicators.values()}
    return sorted(sources | set(config.data_sources.price.symbols))


def load_market_data(
    repository: MarketObservationRepository,
    config: AppConfig,
    *,
    start: date | None = None,
    end: date | None = None,
    available_at: datetime | None = None,
) -> MarketData:
    """Build :class:`MarketData` from stored observations."""
    symbols = required_symbols(config)
    series = repository.get_series_frame(
        symbols, start=start, end=end, available_at=available_at
    )
    if series.empty:
        raise ValueError(
            f"no observations for {symbols} in {start}..{end}; run the collector first"
        )

    tradable = [
        symbol
        for symbol in config.data_sources.price.symbols
        if symbol in {asset.value for asset in Asset} and symbol in series.columns
    ]
    closes = _price_field(repository, tradable, "close", start, end, available_at)
    opens = _price_field(repository, tradable, "open", start, end, available_at)

    # Simulation needs a complete price matrix; a sleeve that has not launched
    # yet (TQQQ before 2010) would otherwise silently become a zero return.
    usable = closes.dropna(how="any")
    if usable.empty:
        raise ValueError(
            "no date has prices for every tradable sleeve; "
            f"coverage starts at {[str(closes[c].first_valid_index()) for c in closes]}"
        )
    closes = usable
    opens = opens.reindex(closes.index)
    series = series.reindex(series.index)

    return MarketData(series=series, closes=closes, opens=opens)


def _price_field(
    repository: MarketObservationRepository,
    symbols: list[str],
    field: str,
    start: date | None,
    end: date | None,
    available_at: datetime | None,
) -> DataFrame:
    columns: dict[str, dict[date, float]] = {}
    for symbol in symbols:
        observations = repository.get_observations(
            symbol, start=start, end=end, available_at=available_at
        )
        values: dict[date, float] = {}
        for observation in observations:
            value = getattr(observation, field)
            if value is None and field == "open":
                # Not every source carries an open; fall back to the close so a
                # NEXT_OPEN simulation degrades to NEXT_CLOSE for that day
                # rather than failing outright.
                value = observation.close
            if value is not None:
                values[observation.observation_date] = float(value)
        columns[symbol] = values
    frame = DataFrame(columns)
    frame.index.name = "observation_date"
    return frame.sort_index()

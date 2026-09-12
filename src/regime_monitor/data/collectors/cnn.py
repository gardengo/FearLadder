"""CNN Fear & Greed adapter (TASK-022).

``PRD.md`` 6.5 splits this source in two:

``Live``
    CNN's own endpoint. It answers with the current reading plus roughly one
    year of history — enough to keep the daily worker current, not enough to
    backtest.
``Historical``
    A reconstructed secondary dataset, loaded from a file by
    :mod:`regime_monitor.data.collectors.files`. Every such row is stored with
    ``quality_status = REVIEW`` so a backtest can never silently treat
    reconstructed sentiment as if CNN had published it.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd
from pandas import DataFrame

from regime_monitor.data.collectors.base import (
    CollectorError,
    DataUnavailableError,
    SourceDescription,
    standardize_series,
)

logger = logging.getLogger(__name__)

CNN_ENDPOINT = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

#: CNN rejects non-browser clients with HTTP 418, so a realistic header set is
#: required simply to read a public page. Nothing here identifies a user.
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://edition.cnn.com/",
    "Origin": "https://edition.cnn.com",
}


def _http_get_json(url: str, *, timeout: float, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise CollectorError(f"CNN endpoint returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CollectorError(f"CNN endpoint unreachable or unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise CollectorError("CNN endpoint returned an unexpected payload shape")
    return payload


@dataclass(frozen=True, slots=True)
class CnnFearGreedProvider:
    """Live CNN Fear & Greed, on the published 0-100 greed scale."""

    endpoint: str = CNN_ENDPOINT
    timeout_seconds: float = 20.0
    headers: dict[str, str] = field(default_factory=lambda: dict(BROWSER_HEADERS))

    def fetch(self, start: date, end: date) -> DataFrame:
        payload = _http_get_json(
            self.endpoint, timeout=self.timeout_seconds, headers=self.headers
        )
        rows = self._historical_rows(payload)
        rows.extend(self._current_row(payload))
        if not rows:
            raise DataUnavailableError("CNN endpoint carried neither history nor a current value")

        frame = standardize_series(
            DataFrame(rows, columns=["observation_date", "value"]).set_index("observation_date"),
            name="CNN_FEAR_GREED",
            column="value",
        )
        window = frame.loc[(frame.index >= start) & (frame.index <= end)]
        if window.empty:
            raise DataUnavailableError(
                f"CNN has no Fear & Greed values between {start} and {end}; "
                "its endpoint only serves roughly the last year (PRD.md 6.5)"
            )
        return window

    @staticmethod
    def _historical_rows(payload: dict[str, Any]) -> list[tuple[pd.Timestamp, float]]:
        data = payload.get("fear_and_greed_historical", {}).get("data", [])
        rows: list[tuple[pd.Timestamp, float]] = []
        for point in data:
            try:
                # CNN reports epoch milliseconds, UTC.
                moment = pd.to_datetime(int(point["x"]), unit="ms", utc=True)
                rows.append((moment.normalize().tz_localize(None), float(point["y"])))
            except (KeyError, TypeError, ValueError):
                logger.warning("skipping malformed CNN history point: %r", point)
        return rows

    @staticmethod
    def _current_row(payload: dict[str, Any]) -> list[tuple[pd.Timestamp, float]]:
        current = payload.get("fear_and_greed") or {}
        timestamp = current.get("timestamp")
        score = current.get("score")
        if timestamp is None or score is None:
            return []
        try:
            moment = pd.Timestamp(datetime.fromisoformat(str(timestamp))).tz_convert(None)
        except (TypeError, ValueError):
            logger.warning("skipping malformed CNN current timestamp: %r", timestamp)
            return []
        return [(moment.normalize(), float(score))]

    def describe(self) -> SourceDescription:
        return SourceDescription(
            provider_library="urllib",
            provider_library_version="stdlib",
            underlying_source="CNN Business Fear & Greed Index (live endpoint)",
            source_ref=self.endpoint,
        )

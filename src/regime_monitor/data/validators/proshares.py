"""ProShares cross-validation for QLD and TQQQ (TASK-028).

``BACKTEST_SPEC.md`` 5.5 is the governing rule::

    불일치가 발생하면 자동으로 정상 데이터로 간주하지 않고
    quality_status = REVIEW 로 분류한다.

So this module never repairs anything. It compares the collected series against
a ProShares reference export and emits :class:`DataQualityFinding` records plus
REVIEW-flagged copies of the affected observations. A human decides what the
truth is.

Four checks, matching the task:

``trading_dates``
    Days one series has and the other does not.
``split_events``
    Overnight moves that look like an unadjusted share split.
``price_continuity``
    Overnight moves too large to be a real market move.
``price_discrepancy``
    Same day, materially different price level.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

from regime_monitor.constants import DataQualityStatus
from regime_monitor.data.models import DataQualityFinding, MarketObservation

logger = logging.getLogger(__name__)

REFERENCE_SOURCE = "proshares"

#: Ratios a leveraged ETF share split/reverse-split realistically lands on.
KNOWN_SPLIT_RATIOS: tuple[float, ...] = (2.0, 3.0, 4.0, 5.0, 10.0)
#: How close to one of those ratios an overnight jump must be to be called a split.
SPLIT_RATIO_TOLERANCE = 0.05


@dataclass(frozen=True, slots=True)
class CrossValidationReport:
    """What the comparison found. Empty ``findings`` means the series agree."""

    symbol: str
    checked_days: int
    findings: tuple[DataQualityFinding, ...] = ()
    flagged: tuple[MarketObservation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        if self.ok:
            return f"{self.symbol}: {self.checked_days} days agree with ProShares"
        by_check: dict[str, int] = {}
        for finding in self.findings:
            by_check[finding.check_name] = by_check.get(finding.check_name, 0) + 1
        detail = ", ".join(f"{name}={count}" for name, count in sorted(by_check.items()))
        return (
            f"{self.symbol}: {len(self.findings)} finding(s) over {self.checked_days} "
            f"days needing REVIEW ({detail})"
        )


@dataclass(frozen=True, slots=True)
class ProSharesCrossValidator:
    """Compares a collected price series against a ProShares reference.

    ``price_tolerance_pct``
        Same-day level disagreement that counts as material.
    ``continuity_threshold_pct``
        Overnight move that is implausible for a real session and therefore
        suggests an adjustment or data error rather than a market move. Both are
        data-quality thresholds, not strategy parameters, so they are ordinary
        configuration rather than research parameters.
    """

    price_tolerance_pct: float = 1.0
    continuity_threshold_pct: float = 60.0
    known_split_ratios: tuple[float, ...] = KNOWN_SPLIT_RATIOS
    split_ratio_tolerance: float = SPLIT_RATIO_TOLERANCE
    reference_source: str = REFERENCE_SOURCE
    #: Splits ProShares has published, as ``symbol -> {date: ratio}``. A jump on
    #: one of these dates is expected, not a finding.
    known_splits: Mapping[str, Mapping[date, float]] = field(default_factory=dict)

    # -- public API --------------------------------------------------------
    def validate(
        self,
        symbol: str,
        observations: Sequence[MarketObservation],
        reference: Mapping[date, float],
    ) -> CrossValidationReport:
        if not observations:
            return CrossValidationReport(symbol=symbol, checked_days=0)

        collected = {obs.observation_date: obs for obs in observations}
        findings: list[DataQualityFinding] = []
        findings.extend(self._check_trading_dates(symbol, collected, reference))
        findings.extend(self._check_price_discrepancy(symbol, collected, reference))
        findings.extend(self._check_continuity_and_splits(symbol, observations))

        flagged_dates = {finding.observation_date for finding in findings}
        flagged = tuple(
            collected[day].flagged(
                DataQualityStatus.REVIEW,
                "; ".join(
                    finding.detail for finding in findings if finding.observation_date == day
                ),
            )
            for day in sorted(flagged_dates)
            if day in collected
        )
        report = CrossValidationReport(
            symbol=symbol,
            checked_days=len(collected),
            findings=tuple(findings),
            flagged=flagged,
        )
        logger.info("%s", report.summary())
        return report

    # -- individual checks -------------------------------------------------
    def _check_trading_dates(
        self,
        symbol: str,
        collected: Mapping[date, MarketObservation],
        reference: Mapping[date, float],
    ) -> Iterable[DataQualityFinding]:
        if not reference:
            return []
        # Only compare the overlapping span: a reference export that starts later
        # or ends earlier is a coverage difference, not a data disagreement.
        low = max(min(collected), min(reference))
        high = min(max(collected), max(reference))
        if low > high:
            return []

        collected_days = {day for day in collected if low <= day <= high}
        reference_days = {day for day in reference if low <= day <= high}

        findings = [
            self._finding(
                symbol,
                day,
                "trading_dates",
                "present in the collected series but absent from the ProShares reference",
                primary=collected[day].close,
            )
            for day in sorted(collected_days - reference_days)
        ]
        findings.extend(
            self._finding(
                symbol,
                day,
                "trading_dates",
                "present in the ProShares reference but missing from the collected series",
                reference_value=reference[day],
            )
            for day in sorted(reference_days - collected_days)
        )
        return findings

    def _check_price_discrepancy(
        self,
        symbol: str,
        collected: Mapping[date, MarketObservation],
        reference: Mapping[date, float],
    ) -> Iterable[DataQualityFinding]:
        findings = []
        for day in sorted(set(collected) & set(reference)):
            observed = collected[day].close
            expected = reference[day]
            if observed is None or not expected:
                continue
            difference_pct = abs(observed - expected) / abs(expected) * 100.0
            if difference_pct > self.price_tolerance_pct:
                findings.append(
                    self._finding(
                        symbol,
                        day,
                        "price_discrepancy",
                        f"close {observed:.4f} differs from ProShares {expected:.4f} "
                        f"by {difference_pct:.2f}% (tolerance {self.price_tolerance_pct:.2f}%)",
                        primary=observed,
                        reference_value=expected,
                    )
                )
        return findings

    def _check_continuity_and_splits(
        self, symbol: str, observations: Sequence[MarketObservation]
    ) -> Iterable[DataQualityFinding]:
        published = self.known_splits.get(symbol, {})
        findings: list[DataQualityFinding] = []
        ordered = sorted(observations, key=lambda obs: obs.observation_date)

        for previous, current in pairwise(ordered):
            before, after = previous.close, current.close
            if not before or not after:
                continue
            ratio = before / after
            move_pct = abs(after - before) / before * 100.0
            day = current.observation_date

            # A split-shaped jump is checked on its own terms, not only when it
            # is large enough to trip the continuity limit: the smallest ratio
            # worth recognising (2:1) is a 50% move, which sits below it.
            split_ratio = self._matching_split_ratio(ratio)
            if split_ratio is not None:
                if day in published:
                    continue  # a split ProShares documented: expected, not a finding
                findings.append(
                    self._finding(
                        symbol,
                        day,
                        "split_events",
                        f"overnight ratio {ratio:.3f} looks like an undocumented "
                        f"{split_ratio:g}:1 split ({before:.4f} -> {after:.4f})",
                        primary=after,
                    )
                )
                continue

            if move_pct > self.continuity_threshold_pct:
                findings.append(
                    self._finding(
                        symbol,
                        day,
                        "price_continuity",
                        f"overnight move of {move_pct:.1f}% ({before:.4f} -> {after:.4f}) "
                        f"exceeds the {self.continuity_threshold_pct:.0f}% continuity limit",
                        primary=after,
                    )
                )
        return findings

    def _matching_split_ratio(self, ratio: float) -> float | None:
        for candidate in self.known_split_ratios:
            for value in (candidate, 1.0 / candidate):
                if abs(ratio - value) <= self.split_ratio_tolerance * value:
                    return candidate
        return None

    def _finding(
        self,
        symbol: str,
        day: date,
        check_name: str,
        detail: str,
        *,
        primary: float | None = None,
        reference_value: float | None = None,
    ) -> DataQualityFinding:
        return DataQualityFinding(
            symbol=symbol,
            observation_date=day,
            check_name=check_name,
            status=DataQualityStatus.REVIEW,
            detail=detail,
            primary_value=primary,
            reference_value=reference_value,
            reference_source=self.reference_source,
        )


def load_reference_prices(path: object) -> dict[date, float]:
    """Read a ProShares reference export into ``{date: close}``.

    The export is a file the operator downloads from ProShares; there is no
    public API. ``docs/operations.md`` records where to put it.
    """
    from pathlib import Path

    import pandas as pd

    target = Path(str(path))
    if not target.is_file():
        raise FileNotFoundError(
            f"ProShares reference not found: {target}. "
            "Cross-validation cannot run without it (TASK-028)."
        )
    frame = pd.read_csv(target) if target.suffix.lower() == ".csv" else pd.read_excel(target)
    columns = {str(column).strip().lower(): column for column in frame.columns}
    date_key = columns.get("date")
    close_key = columns.get("close") or columns.get("nav") or columns.get("price")
    if date_key is None or close_key is None:
        raise ValueError(
            f"{target.name}: expected 'date' and one of 'close'/'nav'/'price'; "
            f"found {list(frame.columns)}"
        )
    parsed = pd.to_datetime(frame[date_key], errors="coerce")
    values = pd.to_numeric(frame[close_key], errors="coerce")
    return {
        moment.date(): float(value)
        for moment, value in zip(parsed, values, strict=True)
        if pd.notna(moment) and pd.notna(value)
    }

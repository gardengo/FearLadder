"""TASK-028 — ProShares cross-validation.

The governing rule is that a mismatch is *recorded*, never repaired
(``BACKTEST_SPEC.md`` 5.5), so these tests check both the detection and the
refusal to auto-correct.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from regime_monitor.constants import DataQualityStatus
from regime_monitor.data.models import MarketObservation, Provenance
from regime_monitor.data.validators.proshares import (
    ProSharesCrossValidator,
    load_reference_prices,
)
from tests.conftest import make_price_observation

START = date(2024, 1, 2)


def _series(prices: list[float], provenance: Provenance) -> list[MarketObservation]:
    return [
        make_price_observation("QLD", START + timedelta(days=index), price, provenance)
        for index, price in enumerate(prices)
    ]


def _reference(observations: list[MarketObservation]) -> dict[date, float]:
    return {obs.observation_date: float(obs.close) for obs in observations}  # type: ignore[arg-type]


# ------------------------------------------------------------- agreeing data


def test_matching_series_produce_no_findings(provenance: Provenance) -> None:
    observations = _series([70.0, 71.0, 72.0], provenance)
    report = ProSharesCrossValidator().validate("QLD", observations, _reference(observations))
    assert report.ok
    assert report.checked_days == 3
    assert report.flagged == ()
    assert "agree with ProShares" in report.summary()


def test_small_differences_stay_inside_tolerance(provenance: Provenance) -> None:
    observations = _series([70.0, 71.0], provenance)
    reference = {obs.observation_date: float(obs.close) * 1.005 for obs in observations}  # type: ignore[arg-type]
    report = ProSharesCrossValidator(price_tolerance_pct=1.0).validate(
        "QLD", observations, reference
    )
    assert report.ok


# ---------------------------------------------------------- price discrepancy


def test_material_price_difference_is_flagged_for_review(provenance: Provenance) -> None:
    observations = _series([70.0, 71.0], provenance)
    reference = _reference(observations)
    reference[START] = 67.9  # ~3.2% away

    report = ProSharesCrossValidator(price_tolerance_pct=1.0).validate(
        "QLD", observations, reference
    )
    assert not report.ok
    finding = report.findings[0]
    assert finding.check_name == "price_discrepancy"
    assert finding.status is DataQualityStatus.REVIEW
    assert finding.reference_source == "proshares"
    assert finding.primary_value == pytest.approx(70.0)
    assert finding.reference_value == pytest.approx(67.9)


def test_the_observation_is_flagged_but_never_rewritten(provenance: Provenance) -> None:
    # BACKTEST_SPEC.md 5.5 — no silent repair.
    observations = _series([70.0], provenance)
    report = ProSharesCrossValidator().validate("QLD", observations, {START: 60.0})

    assert len(report.flagged) == 1
    flagged = report.flagged[0]
    assert flagged.quality_status is DataQualityStatus.REVIEW
    assert flagged.close == pytest.approx(70.0), "the collected price must be preserved"
    assert observations[0].quality_status is DataQualityStatus.OK, "input is not mutated"


# -------------------------------------------------------------- trading dates


def test_a_day_missing_from_the_reference_is_flagged(provenance: Provenance) -> None:
    observations = _series([70.0, 71.0, 72.0], provenance)
    reference = _reference(observations)
    del reference[observations[1].observation_date]

    report = ProSharesCrossValidator().validate("QLD", observations, reference)
    dates_findings = [f for f in report.findings if f.check_name == "trading_dates"]
    assert len(dates_findings) == 1
    assert "absent from the ProShares reference" in dates_findings[0].detail


def test_a_day_missing_from_the_collected_series_is_flagged(provenance: Provenance) -> None:
    observations = _series([70.0, 71.0, 72.0], provenance)
    reference = _reference(observations)
    reference[date(2024, 1, 3)] = 70.5  # a day FDR did not return

    collected = [obs for obs in observations if obs.observation_date != date(2024, 1, 3)]
    report = ProSharesCrossValidator().validate("QLD", collected, reference)
    details = [f.detail for f in report.findings if f.check_name == "trading_dates"]
    assert any("missing from the collected series" in detail for detail in details)


def test_only_the_overlapping_span_is_compared(provenance: Provenance) -> None:
    # A reference export that simply starts later is a coverage gap, not a
    # disagreement, and must not raise hundreds of spurious findings.
    observations = _series([70.0, 71.0, 72.0, 73.0], provenance)
    reference = {
        obs.observation_date: float(obs.close)  # type: ignore[arg-type]
        for obs in observations[2:]
    }
    report = ProSharesCrossValidator().validate("QLD", observations, reference)
    assert report.ok


# ----------------------------------------------------------------- continuity


def test_an_undocumented_split_is_flagged_as_a_split(provenance: Provenance) -> None:
    # TQQQ's price halving overnight looks like a 2:1 split.
    observations = _series([120.0, 60.0], provenance)
    report = ProSharesCrossValidator().validate("QLD", observations, {})

    split_findings = [f for f in report.findings if f.check_name == "split_events"]
    assert len(split_findings) == 1
    assert "2:1 split" in split_findings[0].detail


def test_a_documented_split_is_not_a_finding(provenance: Provenance) -> None:
    observations = _series([120.0, 60.0], provenance)
    validator = ProSharesCrossValidator(
        known_splits={"QLD": {observations[1].observation_date: 2.0}}
    )
    report = validator.validate("QLD", observations, {})
    assert report.ok


def test_an_implausible_overnight_move_is_a_continuity_finding(
    provenance: Provenance,
) -> None:
    # -70% in one session is neither a market move nor a recognisable split.
    observations = _series([100.0, 30.0], provenance)
    report = ProSharesCrossValidator().validate("QLD", observations, {})

    continuity = [f for f in report.findings if f.check_name == "price_continuity"]
    assert len(continuity) == 1
    assert "continuity limit" in continuity[0].detail


def test_a_violent_but_real_move_is_not_flagged(provenance: Provenance) -> None:
    # 2020-03-16: TQQQ fell roughly 35% in a day. That is real, and must survive.
    observations = _series([100.0, 65.0], provenance)
    report = ProSharesCrossValidator().validate("QLD", observations, {})
    assert report.ok


# ------------------------------------------------------------------ plumbing


def test_empty_input_is_handled(provenance: Provenance) -> None:
    report = ProSharesCrossValidator().validate("QLD", [], {})
    assert report.ok
    assert report.checked_days == 0


def test_summary_counts_each_check(provenance: Provenance) -> None:
    observations = _series([100.0, 30.0], provenance)
    report = ProSharesCrossValidator().validate("QLD", observations, {START: 50.0})
    summary = report.summary()
    assert "price_continuity=1" in summary
    assert "price_discrepancy=1" in summary


def test_reference_loader_reads_a_proshares_export(tmp_path: Path) -> None:
    path = tmp_path / "qld.csv"
    path.write_text("Date,NAV\n2024-01-02,70.12\n2024-01-03,71.30\n", encoding="utf-8")
    prices = load_reference_prices(path)
    assert prices == {date(2024, 1, 2): 70.12, date(2024, 1, 3): 71.30}


def test_reference_loader_explains_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="TASK-028"):
        load_reference_prices(tmp_path / "absent.csv")


def test_reference_loader_rejects_unknown_columns(tmp_path: Path) -> None:
    path = tmp_path / "qld.csv"
    path.write_text("when,how_much\n2024-01-02,70.12\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected 'date'"):
        load_reference_prices(path)

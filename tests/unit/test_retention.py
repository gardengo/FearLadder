"""The retention floor that keeps pruning from degrading the next signal.

Pruning exists to bound the repository, but the database it prunes is the same
one tomorrow's score is computed from. A window shorter than the longest
indicator lookback would not fail — it would return a number computed from a
half-filled window, which is worse. These pin the floor and where it comes from.
"""

from __future__ import annotations

from datetime import date

import pytest

from fear_ladder.config.loader import load_config
from fear_ladder.data.retention import (
    SAFETY_FACTOR,
    TRADING_DAYS_PER_YEAR,
    minimum_years,
    plan,
    required_trading_days,
)

AS_OF = date(2026, 9, 11)


def test_the_floor_comes_from_the_configuration_not_a_constant(placeholder_config) -> None:
    """Adding a longer-lookback indicator must move the floor with it."""
    required = required_trading_days(placeholder_config)
    longest = max(
        spec.normalization.window or 0
        for spec in placeholder_config.indicators.enabled_indicators.values()
    )
    assert required >= longest


def test_a_weekly_source_counts_its_window_in_weeks(placeholder_config) -> None:
    """AAII publishes weekly, so a 104-observation window spans two years."""
    weekly = [
        name
        for name, spec in placeholder_config.indicators.enabled_indicators.items()
        if spec.source == "AAII_SENTIMENT"
    ]
    if not weekly:
        pytest.skip("no weekly source enabled in this profile")
    spec = placeholder_config.indicators.enabled_indicators[weekly[0]]
    assert spec.normalization.window is not None
    # The floor must exceed the raw window count, or the weekly cadence was
    # silently treated as daily.
    assert required_trading_days(placeholder_config) >= spec.normalization.window * 5


def test_the_shipped_strategy_needs_more_than_four_years() -> None:
    config = load_config()
    assert required_trading_days(config) > TRADING_DAYS_PER_YEAR * 2
    assert minimum_years(config) == pytest.approx(
        required_trading_days(config) * SAFETY_FACTOR / TRADING_DAYS_PER_YEAR
    )


def test_the_workflows_five_year_window_clears_the_floor() -> None:
    """.github/workflows/daily_monitor.yml prunes with --keep-years 5."""
    config = load_config()
    assert plan(config, keep_years=5, as_of=AS_OF).safe


def test_too_short_a_window_is_reported_as_unsafe() -> None:
    config = load_config()
    decision = plan(config, keep_years=1, as_of=AS_OF)
    assert not decision.safe
    assert "TOO SHORT" in decision.describe()


def test_the_cutoff_is_measured_back_from_the_given_day() -> None:
    config = load_config()
    decision = plan(config, keep_years=5, as_of=AS_OF)
    assert decision.cutoff < AS_OF
    assert (AS_OF - decision.cutoff).days == pytest.approx(5 * 365.25, abs=1)


def test_a_non_positive_window_is_refused() -> None:
    config = load_config()
    with pytest.raises(ValueError, match="positive"):
        plan(config, keep_years=0, as_of=AS_OF)


def test_the_workflow_and_the_script_agree_on_the_window() -> None:
    """A drift between them would silently change what production keeps."""
    import scripts.prune_observations as prune_cli

    from fear_ladder import paths

    workflow = (
        paths.PROJECT_ROOT / ".github" / "workflows" / "daily_monitor.yml"
    ).read_text(encoding="utf-8")
    assert f"--keep-years {prune_cli.DEFAULT_KEEP_YEARS:g}" in workflow

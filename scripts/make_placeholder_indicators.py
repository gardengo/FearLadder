"""Regenerate ``config/research/placeholder.indicators.yaml`` from the real file.

The placeholder profile exists only so development and tests can execute; every
unresolved research window is filled with one arbitrary constant. Keeping it
generated (rather than hand-maintained) means the two files cannot drift and
nobody is tempted to tune the placeholder.

Usage::

    python scripts/make_placeholder_indicators.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder import paths
from fear_ladder.config.loader import (
    PLACEHOLDER_INDICATORS,
    RESEARCH_DIR_NAME,
)

#: One arbitrary lookback for every daily rolling normalizer, ~2 years.
DAILY_WINDOW = 504
DAILY_MIN_PERIODS = 252
#: AAII is weekly, so its window is counted in weekly observations.
WEEKLY_WINDOW = 104
WEEKLY_MIN_PERIODS = 52

HEADER = """# placeholder.indicators.yaml — RESEARCH PLACEHOLDER ONLY. DO NOT SHIP.
#
# A mechanical copy of config/indicators.yaml with every null normalization
# window filled with the SAME arbitrary value ({daily} trading days ~ 2 years) so
# that development, unit tests and exploratory backtests can run end to end.
#
# The uniformity is deliberate: it is obviously not a researched choice. Nothing
# here may be promoted to config/strategy.yaml without going through
# TASK-041 / TASK-081 / TASK-083 and the OOS gate.
#
# Regenerate with: python scripts/make_placeholder_indicators.py

"""


def render(source_text: str) -> str:
    body = source_text.split("version: 1", 1)[1]
    out = HEADER.format(daily=DAILY_WINDOW) + "version: 1" + body
    out = out.replace(
        "      window: null\n      min_periods: null",
        f"      window: {DAILY_WINDOW} # RESEARCH_PLACEHOLDER\n"
        f"      min_periods: {DAILY_MIN_PERIODS} # RESEARCH_PLACEHOLDER",
    )
    out = out.replace(
        "    params: { window: null } # RESEARCH: own lookback for the percentile rank",
        f"    params: {{ window: {DAILY_WINDOW} }} # RESEARCH_PLACEHOLDER",
    )
    return out.replace(
        f"      window: {DAILY_WINDOW} # RESEARCH_PLACEHOLDER\n"
        f"      min_periods: {DAILY_MIN_PERIODS} # RESEARCH_PLACEHOLDER\n"
        "      research_candidates: [52, 104, 156, 260] # weekly observations",
        f"      window: {WEEKLY_WINDOW} # RESEARCH_PLACEHOLDER (weekly observations)\n"
        f"      min_periods: {WEEKLY_MIN_PERIODS} # RESEARCH_PLACEHOLDER\n"
        "      research_candidates: [52, 104, 156, 260] # weekly observations",
    )


def main() -> int:
    source = paths.CONFIG_DIR / "indicators.yaml"
    target = paths.CONFIG_DIR / RESEARCH_DIR_NAME / PLACEHOLDER_INDICATORS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(source.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

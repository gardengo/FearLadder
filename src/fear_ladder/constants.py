"""Domain constants shared across the whole system.

Only things that are *definitional* live here. Every number that the strategy
research still has to decide (weights, thresholds, boundaries, allocations)
belongs in ``config/`` and stays ``null`` until the strategy is frozen.
See ``CLAUDE_CODE_INITIAL_PROMPT.md`` §7 and §10.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Asset(StrEnum):
    """Investable sleeves supported by the allocation engine (``PRD.md`` §9)."""

    QQQ = "QQQ"
    QLD = "QLD"
    TQQQ = "TQQQ"
    CASH = "CASH"


#: Stated daily target multiple of each sleeve. Used only to express the research
#: metric ``Target Leverage`` (``BACKTEST_SPEC.md`` §14). It is *not* a claim about
#: realised long-horizon returns.
ASSET_LEVERAGE: Final[dict[Asset, float]] = {
    Asset.QQQ: 1.0,
    Asset.QLD: 2.0,
    Asset.TQQQ: 3.0,
    Asset.CASH: 0.0,
}

#: Price series the pipeline must be able to fetch.
TRADABLE_SYMBOLS: Final[tuple[str, ...]] = ("QQQ", "QLD", "TQQQ")

#: The reserved regime label used whenever the pipeline cannot produce a
#: trustworthy state (``ARCHITECTURE.md`` §15). It is never configurable and it
#: never carries an allocation.
UNKNOWN_REGIME: Final[str] = "UNKNOWN"

#: Score domain. 0 = extreme fear / risk-off, 100 = extreme greed / risk-on
#: (``PRD.md`` §7).
SCORE_MIN: Final[float] = 0.0
SCORE_MAX: Final[float] = 100.0


class IndicatorDirection(StrEnum):
    """How a raw indicator maps onto the fear/greed score axis.

    ``HIGHER_IS_GREED``
        Raw value up -> score up (e.g. 12M momentum, RSI).
    ``HIGHER_IS_FEAR``
        Raw value up -> score down (e.g. VIX level, drawdown depth).
    """

    HIGHER_IS_GREED = "HIGHER_IS_GREED"
    HIGHER_IS_FEAR = "HIGHER_IS_FEAR"


class DataQualityStatus(StrEnum):
    """Provenance verdict attached to every stored observation.

    ``REVIEW`` is produced by cross-validation mismatches and must never be
    silently auto-corrected (``BACKTEST_SPEC.md`` §5.5).
    """

    OK = "OK"
    REVIEW = "REVIEW"
    STALE = "STALE"
    MISSING = "MISSING"
    CORRUPT = "CORRUPT"


class ParameterStatus(StrEnum):
    """Lifecycle of the strategy parameter set (``ARCHITECTURE.md`` §14)."""

    RESEARCH = "RESEARCH"
    RESEARCH_PLACEHOLDER = "RESEARCH_PLACEHOLDER"
    VALIDATION = "VALIDATION"
    FROZEN = "FROZEN"


#: Only a frozen parameter set may drive the production daily runner.
PRODUCTION_PARAMETER_STATUS: Final[ParameterStatus] = ParameterStatus.FROZEN


class EventType(StrEnum):
    """Alert event catalogue (``PRD.md`` §16, ``TASKS.md`` TASK-121)."""

    REGIME_CHANGED = "REGIME_CHANGED"
    TARGET_LEVERAGE_CHANGED = "TARGET_LEVERAGE_CHANGED"
    EXTREME_FEAR = "EXTREME_FEAR"
    EXTREME_BUBBLE = "EXTREME_BUBBLE"
    TQQQ_CANDIDATE = "TQQQ_CANDIDATE"
    DATA_FAILURE = "DATA_FAILURE"


class PipelineStatus(StrEnum):
    """Terminal state of one daily pipeline run."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    DATA_FAILURE = "DATA_FAILURE"


class ExecutionTiming(StrEnum):
    """When a signal generated at ``t`` close is assumed to be executed.

    Same-day close execution is deliberately absent: ``BACKTEST_SPEC.md`` §5
    forbids it as the headline result.
    """

    NEXT_OPEN = "NEXT_OPEN"
    NEXT_CLOSE = "NEXT_CLOSE"

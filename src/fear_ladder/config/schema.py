"""Validated configuration models (TASK-002).

Design rule: a research parameter that the backtest has not decided yet is
``null`` in YAML and ``None`` here. Validation therefore has to accept ``None``
everywhere *and* be able to report which parameters are still unresolved, so the
production runner can refuse to act on them.
"""

from __future__ import annotations

from datetime import date, datetime
from itertools import pairwise
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from fear_ladder.constants import (
    ASSET_LEVERAGE,
    SCORE_MAX,
    SCORE_MIN,
    UNKNOWN_REGIME,
    Asset,
    DataQualityStatus,
    ExecutionTiming,
    IndicatorDirection,
    ParameterStatus,
)

WEIGHT_SUM_TOLERANCE = 1e-6

Probability = Annotated[float, Field(ge=0.0, le=1.0)]
Score = Annotated[float, Field(ge=SCORE_MIN, le=SCORE_MAX)]
PositiveWindow = Annotated[int, Field(gt=0)]


class ConfigError(ValueError):
    """Raised when configuration is structurally invalid."""


class ResearchParameterError(RuntimeError):
    """Raised when unresolved research parameters would drive a production run."""


class _Base(BaseModel):
    """Base for every config model.

    Pydantic wraps a validator's ``ValueError`` in a ``ValidationError``. Since
    configuration problems should surface as one predictable type to callers and
    to the operator reading a failed GitHub Actions log, construction re-raises
    everything as :class:`ConfigError`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise ConfigError(_describe(exc)) from exc


def _describe(exc: ValidationError) -> str:
    """Flatten a pydantic error into one operator-readable message."""
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or exc.title
        lines.append(f"{location}: {error['msg']}")
    return "; ".join(lines) or str(exc)


# --------------------------------------------------------------------------
# data_sources.yaml
# --------------------------------------------------------------------------


class RetryPolicy(_Base):
    attempts: Annotated[int, Field(ge=1, le=10)] = 3
    backoff_seconds: Annotated[float, Field(ge=0.0, le=60.0)] = 2.0


class SymbolSpec(_Base):
    role: str
    leverage: Annotated[float, Field(ge=0.0, le=3.0)]
    inception: date | None = None


class CrossValidationSpec(_Base):
    """TASK-028 - ProShares cross-validation switches."""

    enabled: bool = True
    reference: Literal["proshares"] = "proshares"
    symbols: tuple[str, ...] = ()
    reference_dir: str = "data/reference/proshares"
    price_tolerance_pct: Annotated[float, Field(ge=0.0, le=100.0)] = 1.0
    on_mismatch: Literal["REVIEW"] = "REVIEW"

    @model_validator(mode="after")
    def _mismatch_is_never_auto_corrected(self) -> Self:
        # BACKTEST_SPEC.md 5.5: a mismatch is flagged, never silently repaired.
        if self.on_mismatch != DataQualityStatus.REVIEW.value:
            raise ConfigError("cross_validation.on_mismatch must be REVIEW")
        return self


class ReconstructionSpec(_Base):
    """Extending the tradable sleeves before their inception.

    Off by default: a reconstructed price is a *model*, and turning it on has to
    be a deliberate act recorded in configuration.
    """

    enabled: bool = False
    index_symbol: str = "FRED:NASDAQ100"
    financing_symbol: str | None = "FRED:DFF"
    quality_status: DataQualityStatus = DataQualityStatus.REVIEW

    @model_validator(mode="after")
    def _reconstruction_is_never_official(self) -> Self:
        # A modelled price must never be stored as if it had been observed.
        if self.enabled and self.quality_status is DataQualityStatus.OK:
            raise ConfigError(
                "reconstructed prices cannot be quality_status OK; they are "
                "modelled, not observed (PRD.md 6.5)"
            )
        return self


class PriceSourceSpec(_Base):
    provider: Literal["finance_datareader"] = "finance_datareader"
    symbols: dict[str, SymbolSpec]
    start_date: date
    mandatory: bool = True
    max_staleness_days: Annotated[int, Field(ge=0, le=90)] = 5
    retry: RetryPolicy = RetryPolicy()
    reconstruction: ReconstructionSpec = ReconstructionSpec()
    cross_validation: CrossValidationSpec = CrossValidationSpec()

    @model_validator(mode="after")
    def _leverage_matches_the_domain(self) -> Self:
        for symbol, spec in self.symbols.items():
            asset = Asset(symbol) if symbol in Asset.__members__ else None
            if asset is not None and ASSET_LEVERAGE[asset] != spec.leverage:
                raise ConfigError(
                    f"price.symbols.{symbol}.leverage={spec.leverage} contradicts "
                    f"the domain constant {ASSET_LEVERAGE[asset]}"
                )
        return self


class SeriesSourceSpec(_Base):
    """A non-price input series (VIX, sentiment, breadth)."""

    enabled: bool = True
    provider: str
    underlying_source: str
    series_id: str | None = None
    mandatory: bool = False
    #: Publication lag in *calendar* days between observation_date and the moment
    #: the value may legitimately be used. 0 = available at the close it describes.
    availability_lag_days: Annotated[int, Field(ge=0, le=30)] = 0
    max_staleness_days: Annotated[int, Field(ge=0, le=90)] = 5
    quality_status: DataQualityStatus = DataQualityStatus.OK
    retry: RetryPolicy = RetryPolicy()
    notes: str | None = None
    disabled_reason: str | None = None

    @model_validator(mode="after")
    def _disabled_sources_explain_themselves(self) -> Self:
        # PRD.md 6.5: breadth may only be excluded *explicitly*.
        if not self.enabled and not self.disabled_reason:
            raise ConfigError("a disabled source must state disabled_reason")
        return self


class DataSourcesConfig(_Base):
    version: int
    price: PriceSourceSpec
    series: dict[str, SeriesSourceSpec]
    #: Key in ``series`` whose value is the annualised rate the cash sleeve
    #: earns. ``None`` leaves cash at zero, which understates any rule that
    #: holds cash - and this strategy holds roughly a fifth of the book in it.
    cash_rate_series: str | None = None

    @property
    def mandatory_series(self) -> tuple[str, ...]:
        return tuple(
            name for name, spec in self.series.items() if spec.enabled and spec.mandatory
        )

    @model_validator(mode="after")
    def _cash_rate_names_a_real_series(self) -> Self:
        named = self.cash_rate_series
        if named is not None and named not in self.series:
            raise ConfigError(
                f"cash_rate_series={named!r} is not in series: {sorted(self.series)}"
            )
        return self


# --------------------------------------------------------------------------
# indicators.yaml
# --------------------------------------------------------------------------


class NormalizationSpec(_Base):
    """How a raw indicator becomes a 0-100 score.

    ``window`` is ``None`` while the research has not fixed it. Full-sample
    statistics are structurally impossible here: every supported method is
    trailing-only (``BACKTEST_SPEC.md`` 9).
    """

    method: Literal["rolling_percentile", "rolling_zscore", "fixed_mapping", "bounded"]
    window: PositiveWindow | None = None
    min_periods: PositiveWindow | None = None
    #: fixed_mapping / bounded: raw value that maps to score 0 and to score 100.
    raw_at_score_min: float | None = None
    raw_at_score_max: float | None = None
    #: rolling_zscore: z clipped to +/- this before being mapped onto 0-100.
    clip_sigma: Annotated[float, Field(gt=0.0, le=10.0)] | None = None
    research_candidates: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _method_specific_fields(self) -> Self:
        rolling = self.method in ("rolling_percentile", "rolling_zscore")
        if rolling and (self.raw_at_score_min is not None or self.raw_at_score_max is not None):
            raise ConfigError(f"{self.method} does not take raw_at_score_ bounds")
        if self.method in ("fixed_mapping", "bounded"):
            if self.window is not None:
                raise ConfigError(f"{self.method} does not take a rolling window")
            bounds = (self.raw_at_score_min, self.raw_at_score_max)
            if None not in bounds and self.raw_at_score_min == self.raw_at_score_max:
                raise ConfigError("raw_at_score_min and raw_at_score_max must differ")
        has_both = self.min_periods is not None and self.window is not None
        if has_both and self.min_periods > self.window:
            raise ConfigError("min_periods cannot exceed window")
        return self

    @property
    def is_resolved(self) -> bool:
        if self.method in ("rolling_percentile", "rolling_zscore"):
            return self.window is not None
        return self.raw_at_score_min is not None and self.raw_at_score_max is not None


class IndicatorSpec(_Base):
    family: Literal[
        "rsi", "trend", "momentum", "drawdown", "volatility", "sentiment", "breadth"
    ]
    #: Name of the computation registered in ``fear_ladder.indicators``.
    compute: str
    #: Keyword arguments for that computation (window lengths etc.). These come
    #: from the documents (RSI(14), 200DMA, 52W drawdown...) and are definitional,
    #: not free research parameters.
    params: dict[str, Any] = Field(default_factory=dict)
    #: Keys inside ``params`` that are research parameters, not definitional ones.
    #: They are allowed to be ``null`` and are reported as unresolved until the
    #: search fixes them. A ``null`` outside this list is a deliberate mode flag
    #: (e.g. drawdown against an expanding peak).
    research_params: tuple[str, ...] = ()
    #: Input series key: a price symbol or a ``series`` key from data_sources.
    source: str
    direction: IndicatorDirection
    normalization: NormalizationSpec
    #: The range this indicator can take *by construction* - RSI is 0-100, a
    #: drawdown is 0-1 - not the range it happened to take in some sample.
    #: Declaring it lets the search offer an absolute scale without inventing a
    #: threshold, which ``CLAUDE_CODE_INITIAL_PROMPT.md`` 10 forbids. Leave it
    #: out for open-ended series (momentum, VIX level, distance from a moving
    #: average): those have no definitional bounds to map onto.
    definitional_range: tuple[float, float] | None = None
    enabled: bool = True
    mandatory: bool = False
    description: str | None = None
    disabled_reason: str | None = None

    @model_validator(mode="after")
    def _definitional_range_is_ordered(self) -> Self:
        if self.definitional_range is not None:
            low, high = self.definitional_range
            if low >= high:
                raise ConfigError(
                    f"definitional_range {self.definitional_range} is not ascending"
                )
        return self

    @model_validator(mode="after")
    def _disabled_indicators_explain_themselves(self) -> Self:
        if not self.enabled and not self.disabled_reason:
            raise ConfigError("a disabled indicator must state disabled_reason")
        unknown = set(self.research_params) - set(self.params)
        if unknown:
            raise ConfigError(f"research_params not present in params: {sorted(unknown)}")
        return self

    @property
    def unresolved_params(self) -> tuple[str, ...]:
        return tuple(key for key in self.research_params if self.params.get(key) is None)

    @property
    def is_resolved(self) -> bool:
        return not self.unresolved_params and self.normalization.is_resolved


class IndicatorsConfig(_Base):
    version: int
    indicators: dict[str, IndicatorSpec]

    @model_validator(mode="after")
    def _at_least_one_enabled(self) -> Self:
        if not any(spec.enabled for spec in self.indicators.values()):
            raise ConfigError("indicators.yaml enables no indicator at all")
        return self

    @property
    def enabled_indicators(self) -> dict[str, IndicatorSpec]:
        return {name: s for name, s in self.indicators.items() if s.enabled}

    def unresolved_parameters(self) -> tuple[str, ...]:
        unresolved: list[str] = []
        for name, spec in self.enabled_indicators.items():
            if not spec.normalization.is_resolved:
                unresolved.append(f"indicators.{name}.normalization")
            unresolved.extend(
                f"indicators.{name}.params.{key}" for key in spec.unresolved_params
            )
        return tuple(unresolved)


# --------------------------------------------------------------------------
# strategy.yaml
# --------------------------------------------------------------------------


class ScoreSpec(_Base):
    #: indicator name -> weight. ``None`` until the weight search converges.
    weights: dict[str, Probability] | None = None

    @model_validator(mode="after")
    def _weights_form_a_convex_combination(self) -> Self:
        # BACKTEST_SPEC.md 10: weight_i >= 0 and sum == 1.
        if self.weights is None:
            return self
        if not self.weights:
            raise ConfigError("score.weights is empty; use null for 'not decided yet'")
        total = sum(self.weights.values())
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise ConfigError(f"score.weights must sum to 1.0, got {total!r}")
        return self


class RegimeSpec(_Base):
    count: Literal[3, 5, 7, 9] | None = None
    #: Ordered fear -> greed. ``labels[i]`` owns scores below ``boundaries[i]``.
    labels: tuple[str, ...] | None = None
    boundaries: tuple[Score, ...] | None = None
    research_candidates: tuple[Literal[3, 5, 7, 9], ...] = (3, 5, 7, 9)

    @model_validator(mode="after")
    def _shapes_agree(self) -> Self:
        if self.labels is not None:
            if UNKNOWN_REGIME in self.labels:
                raise ConfigError(f"{UNKNOWN_REGIME} is reserved and cannot be configured")
            if len(set(self.labels)) != len(self.labels):
                raise ConfigError("regime.labels contains duplicates")
            if self.count is not None and len(self.labels) != self.count:
                raise ConfigError(
                    f"regime.labels has {len(self.labels)} entries but count={self.count}"
                )
        if self.boundaries is not None:
            if list(self.boundaries) != sorted(self.boundaries):
                raise ConfigError("regime.boundaries must be ascending")
            if len(set(self.boundaries)) != len(self.boundaries):
                raise ConfigError("regime.boundaries contains duplicates")
            if self.count is not None and len(self.boundaries) != self.count - 1:
                raise ConfigError(
                    f"regime.boundaries needs count-1={self.count - 1} cut points, "
                    f"got {len(self.boundaries)}"
                )
        return self

    @property
    def is_resolved(self) -> bool:
        return None not in (self.count, self.labels, self.boundaries)


class TransitionSpec(_Base):
    """TASK-051 - how a raw regime becomes a confirmed regime."""

    confirmation_days: Annotated[int, Field(ge=1, le=60)] | None = None
    hysteresis: Annotated[float, Field(ge=0.0, le=50.0)] | None = None
    minimum_duration_days: Annotated[int, Field(ge=1, le=250)] | None = None
    research_candidates: dict[str, tuple[float, ...]] = Field(default_factory=dict)

    @property
    def is_resolved(self) -> bool:
        return None not in (
            self.confirmation_days,
            self.hysteresis,
            self.minimum_duration_days,
        )


class AllocationConstraints(_Base):
    weights_non_negative: bool = True
    weights_sum_to_one: bool = True
    #: PRD.md 2.1 - never fully exit the market, however overheated.
    min_market_exposure: Probability | None = None
    max_target_leverage: Annotated[float, Field(ge=0.0, le=3.0)] | None = None


class AllocationSpec(_Base):
    #: regime label -> {asset -> weight}. ``None`` until the allocation search runs.
    mappings: dict[str, dict[Asset, Probability]] | None = None
    constraints: AllocationConstraints = AllocationConstraints()

    @model_validator(mode="after")
    def _mappings_are_valid_portfolios(self) -> Self:
        if self.mappings is None:
            return self
        for label, weights in self.mappings.items():
            if label == UNKNOWN_REGIME:
                raise ConfigError(f"{UNKNOWN_REGIME} must not carry an allocation")
            total = sum(weights.values())
            if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
                raise ConfigError(f"allocation.mappings.{label} sums to {total!r}, not 1.0")
        return self

    @property
    def is_resolved(self) -> bool:
        return self.mappings is not None


class TqqqGateSpec(_Base):
    """TASK-063 - separates raw fear intensity from bottom confirmation."""

    enabled: bool = True
    #: Names of the ``BottomConfirmationRule`` s that must all pass.
    required_rules: tuple[str, ...] | None = None
    #: How many of ``candidate_rules`` must fire. ``None`` until researched.
    min_confirmations: Annotated[int, Field(ge=1, le=10)] | None = None
    candidate_rules: tuple[str, ...] = ()
    rule_params: dict[str, dict[str, Any]] | None = None

    @property
    def is_resolved(self) -> bool:
        if not self.enabled:
            return True
        return self.min_confirmations is not None and self.required_rules is not None


class TrendFilterSpec(_Base):
    """Leverage into fear, but not into a falling market.

    Measured over 1999-2015, a fear-only ladder is ruinous: the score reaches
    capitulation months into a long decline while the market keeps falling. This
    adds the second condition — the leveraged end of the ladder is available
    only while the long-term trend holds.

    Structural, not a preference: it says *when* leverage is permitted. Which
    indicator, which level and what cap are research parameters.
    """

    enabled: bool = False
    #: Raw indicator read as the trend signal, e.g. ``price_vs_200dma``.
    indicator: str | None = None
    #: Value the trend must fall *below* for the filter to engage.
    threshold: float | None = None
    #: Value the trend must climb back *above* for it to disengage. Leaving this
    #: null means no hysteresis, which measured badly: the filter flipped 82
    #: times over 22 years, a median of those lasting a single day, with the
    #: market going up as often as down. A separate re-entry level turns a
    #: threshold that is brushed into a band that must be crossed.
    reentry_threshold: float | None = None
    #: Ceiling on target leverage while the trend is broken.
    max_leverage_below: Annotated[float, Field(ge=0.0, le=3.0)] | None = None
    #: Indicator measuring how far the market has already fallen, e.g.
    #: ``drawdown_52w``. Paired with ``min_depth_to_engage``.
    depth_indicator: str | None = None
    #: How deep the fall must already be before the filter may engage at all.
    #: Without it the filter reacts to every dip below the trend line, and most
    #: dips are not crises: measured over 1999-2015, 391 of the days it engaged
    #: fell outside any 20% drawdown episode. A depth floor ignores those while
    #: still catching the two real crashes, because both went deep quickly.
    #: The cost is the crash that grinds: the 2008 decline took eleven months to
    #: reach -20%, and this rule holds leverage through that stretch.
    min_depth_to_engage: Annotated[float, Field(ge=0.0, lt=1.0)] | None = None
    research_candidates: dict[str, tuple[float, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reentry_is_above_exit(self) -> Self:
        both = self.threshold is not None and self.reentry_threshold is not None
        if both and self.reentry_threshold < self.threshold:
            raise ConfigError(
                f"reentry_threshold ({self.reentry_threshold}) is below threshold "
                f"({self.threshold}); that inverts the band"
            )
        return self

    @model_validator(mode="after")
    def _depth_needs_both_halves(self) -> Self:
        named = self.depth_indicator is not None
        floored = self.min_depth_to_engage is not None
        if named != floored:
            raise ConfigError(
                "depth_indicator and min_depth_to_engage are meaningless apart; "
                f"got depth_indicator={self.depth_indicator!r}, "
                f"min_depth_to_engage={self.min_depth_to_engage!r}"
            )
        return self

    @model_validator(mode="after")
    def _a_cap_must_actually_cap(self) -> Self:
        caps_nothing = (
            self.enabled
            and self.max_leverage_below is not None
            and self.max_leverage_below >= 3.0
        )
        if caps_nothing:
            raise ConfigError(
                "max_leverage_below=3.0 caps nothing; disable the filter "
                "instead of configuring it to do nothing"
            )
        return self

    @property
    def is_resolved(self) -> bool:
        if not self.enabled:
            return True
        return None not in (self.indicator, self.threshold, self.max_leverage_below)


class ExecutionSpec(_Base):
    """BACKTEST_SPEC.md 5 - signal at t close, execution at t+1."""

    timing: ExecutionTiming = ExecutionTiming.NEXT_OPEN
    same_day_execution_allowed: Literal[False] = False
    rebalance_on: Literal["regime_change"] = "regime_change"

    @model_validator(mode="after")
    def _forbid_same_day_execution(self) -> Self:
        if self.same_day_execution_allowed:
            raise ConfigError("same-day execution is forbidden by BACKTEST_SPEC.md 5")
        return self


class CostModelSpec(_Base):
    """TASK-072. Applied identically to strategy and benchmarks (17)."""

    commission_bps: Annotated[float, Field(ge=0.0, le=100.0)] | None = None
    spread_bps: Annotated[float, Field(ge=0.0, le=100.0)] | None = None
    slippage_bps: Annotated[float, Field(ge=0.0, le=100.0)] | None = None

    @property
    def is_resolved(self) -> bool:
        return None not in (self.commission_bps, self.spread_bps, self.slippage_bps)

    @property
    def total_bps(self) -> float:
        if not self.is_resolved:
            raise ResearchParameterError("cost model is not resolved")
        return float(self.commission_bps) + float(self.spread_bps) + float(self.slippage_bps)


class DatasetSplitSpec(_Base):
    """TASK-090. OOS boundaries are declared up front, before any optimisation."""

    research_start: date | None = None
    research_end: date | None = None
    validation_start: date | None = None
    validation_end: date | None = None
    oos_start: date | None = None
    oos_end: date | None = None

    @model_validator(mode="after")
    def _windows_are_ordered_and_disjoint(self) -> Self:
        edges = [
            ("research", self.research_start, self.research_end),
            ("validation", self.validation_start, self.validation_end),
            ("oos", self.oos_start, self.oos_end),
        ]
        for name, start, end in edges:
            if start is not None and end is not None and start > end:
                raise ConfigError(f"dataset_split.{name}: start is after end")
        ordered = [(n, s, e) for n, s, e in edges if s is not None and e is not None]
        for (before, _, before_end), (after, after_start, _) in pairwise(ordered):
            if before_end >= after_start:
                raise ConfigError(f"dataset_split.{before} overlaps {after}")
        return self

    @property
    def is_resolved(self) -> bool:
        return all(
            value is not None
            for value in (
                self.research_start,
                self.research_end,
                self.validation_start,
                self.validation_end,
                self.oos_start,
                self.oos_end,
            )
        )


class StrategyConfig(_Base):
    version: int
    strategy_version: str
    parameter_status: ParameterStatus
    data_version: str | None = None
    parameter_version: str | None = None
    frozen_at: datetime | None = None
    score: ScoreSpec = ScoreSpec()
    regime: RegimeSpec = RegimeSpec()
    transition: TransitionSpec = TransitionSpec()
    allocation: AllocationSpec = AllocationSpec()
    tqqq_gate: TqqqGateSpec = TqqqGateSpec()
    trend_filter: TrendFilterSpec = TrendFilterSpec()
    execution: ExecutionSpec = ExecutionSpec()
    cost_model: CostModelSpec = CostModelSpec()
    dataset_split: DatasetSplitSpec = DatasetSplitSpec()

    @model_validator(mode="after")
    def _cross_section_consistency(self) -> Self:
        labels = self.regime.labels
        mappings = self.allocation.mappings
        if labels is not None and mappings is not None:
            missing = set(labels) - set(mappings)
            unknown = set(mappings) - set(labels)
            if missing:
                raise ConfigError(f"allocation.mappings is missing regimes: {sorted(missing)}")
            if unknown:
                raise ConfigError(f"allocation.mappings has unknown regimes: {sorted(unknown)}")
        if self.parameter_status is ParameterStatus.FROZEN and self.frozen_at is None:
            raise ConfigError("a FROZEN strategy must record frozen_at")
        return self

    def unresolved_parameters(self) -> tuple[str, ...]:
        """Dotted paths of every research parameter still awaiting a decision."""
        unresolved: list[str] = []
        if self.score.weights is None:
            unresolved.append("score.weights")
        if not self.regime.is_resolved:
            unresolved.append("regime")
        if not self.transition.is_resolved:
            unresolved.append("transition")
        if not self.allocation.is_resolved:
            unresolved.append("allocation.mappings")
        if not self.tqqq_gate.is_resolved:
            unresolved.append("tqqq_gate")
        if not self.trend_filter.is_resolved:
            unresolved.append("trend_filter")
        if not self.cost_model.is_resolved:
            unresolved.append("cost_model")
        if not self.dataset_split.is_resolved:
            unresolved.append("dataset_split")
        return tuple(unresolved)

    @property
    def is_production_ready(self) -> bool:
        return (
            self.parameter_status is ParameterStatus.FROZEN
            and not self.unresolved_parameters()
        )


# --------------------------------------------------------------------------
# alerts.yaml
# --------------------------------------------------------------------------


class AlertRuleSpec(_Base):
    enabled: bool = True
    #: Minimum days between two alerts of the same type (TASK-122 backstop; the
    #: primary deduplication is the unique key on ``alert_events``).
    cooldown_days: Annotated[int, Field(ge=0, le=365)] = 0
    template: str
    #: Regime labels that trigger this alert, when the alert is regime-scoped.
    regimes: tuple[str, ...] | None = None
    severity: Literal["INFO", "WARNING", "CRITICAL"] = "INFO"


class TelegramSpec(_Base):
    enabled: bool = True
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    parse_mode: Literal["HTML", "MarkdownV2", "none"] = "HTML"
    timeout_seconds: Annotated[float, Field(gt=0.0, le=120.0)] = 15.0
    retry: RetryPolicy = RetryPolicy()

    @model_validator(mode="after")
    def _no_inline_secrets(self) -> Self:
        # ARCHITECTURE.md 9: secrets come from the environment, never from YAML.
        for field in (self.bot_token_env, self.chat_id_env):
            if not field.isidentifier():
                raise ConfigError(
                    f"telegram: {field!r} must be an environment variable NAME, "
                    "not a value"
                )
        return self


class AlertsConfig(_Base):
    version: int
    telegram: TelegramSpec = TelegramSpec()
    rules: dict[str, AlertRuleSpec]
    #: Deduplication key components (ARCHITECTURE.md 11).
    dedupe_key: tuple[str, ...] = ("event_date", "event_type", "strategy_version")

    @model_validator(mode="after")
    def _rules_are_known_event_types(self) -> Self:
        from fear_ladder.constants import EventType

        known = {member.value for member in EventType}
        unknown = set(self.rules) - known
        if unknown:
            raise ConfigError(f"alerts.rules has unknown event types: {sorted(unknown)}")
        return self

    @property
    def enabled_rules(self) -> dict[str, AlertRuleSpec]:
        return {name: rule for name, rule in self.rules.items() if rule.enabled}


# --------------------------------------------------------------------------
# bundle
# --------------------------------------------------------------------------


class AppConfig(_Base):
    """Everything the pipeline needs, validated together."""

    indicators: IndicatorsConfig
    strategy: StrategyConfig
    alerts: AlertsConfig
    data_sources: DataSourcesConfig

    @model_validator(mode="after")
    def _cross_file_consistency(self) -> Self:
        known_sources = set(self.data_sources.price.symbols) | set(self.data_sources.series)
        for name, spec in self.indicators.enabled_indicators.items():
            if spec.source not in known_sources:
                raise ConfigError(
                    f"indicators.{name}.source={spec.source!r} is not declared in "
                    f"data_sources.yaml (known: {sorted(known_sources)})"
                )
            source_spec = self.data_sources.series.get(spec.source)
            if source_spec is not None and not source_spec.enabled:
                raise ConfigError(
                    f"indicators.{name} depends on disabled source {spec.source!r}"
                )
        trend = self.strategy.trend_filter
        names_unknown_indicator = (
            trend.enabled
            and trend.indicator is not None
            and trend.indicator not in self.indicators.enabled_indicators
        )
        if names_unknown_indicator:
            raise ConfigError(
                f"trend_filter.indicator={trend.indicator!r} is not an enabled "
                f"indicator (known: {sorted(self.indicators.enabled_indicators)})"
            )

        weights = self.strategy.score.weights
        if weights is not None:
            unknown = set(weights) - set(self.indicators.enabled_indicators)
            if unknown:
                raise ConfigError(f"score.weights references unknown indicators: {sorted(unknown)}")
        if self.alerts.rules:
            labels = self.strategy.regime.labels
            if labels is not None:
                for name, rule in self.alerts.rules.items():
                    if rule.regimes is None:
                        continue
                    unknown_regimes = set(rule.regimes) - set(labels)
                    if unknown_regimes:
                        raise ConfigError(
                            f"alerts.rules.{name}.regimes has unknown labels: "
                            f"{sorted(unknown_regimes)}"
                        )
        return self

    def unresolved_parameters(self) -> tuple[str, ...]:
        return self.strategy.unresolved_parameters() + self.indicators.unresolved_parameters()

    @property
    def is_production_ready(self) -> bool:
        return self.strategy.is_production_ready and not self.indicators.unresolved_parameters()

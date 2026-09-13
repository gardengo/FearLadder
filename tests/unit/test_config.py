"""TASK-002 — configuration loading, validation and the research-parameter gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from regime_monitor import paths
from regime_monitor.config.loader import (
    ensure_production_ready,
    load_config,
    load_research_placeholder_config,
    research_parameters_allowed,
)
from regime_monitor.config.schema import (
    AllocationSpec,
    ConfigError,
    CostModelSpec,
    DatasetSplitSpec,
    ExecutionSpec,
    IndicatorsConfig,
    RegimeSpec,
    ResearchParameterError,
    ScoreSpec,
    StrategyConfig,
    TelegramSpec,
)
from regime_monitor.constants import ParameterStatus

# --------------------------------------------------------------------- loading


def test_the_shipped_configuration_loads_and_cross_validates() -> None:
    config = load_config()
    assert config.indicators.enabled_indicators
    assert config.data_sources.price.symbols.keys() >= {"QQQ", "QLD", "TQQQ"}


def test_the_shipped_strategy_is_frozen_and_fully_resolved() -> None:
    """v1.0-frozen, 2026-09-13. Before that this asserted the opposite.

    The shipped file is the frozen output now, so the invariant worth pinning
    flipped: nothing may be left open, and production must accept it without
    the research-parameter escape hatch.
    """
    config = load_config()
    assert config.strategy.parameter_status is ParameterStatus.FROZEN
    assert config.strategy.frozen_at is not None
    assert config.strategy.unresolved_parameters() == ()
    assert config.unresolved_parameters() == ()
    assert config.is_production_ready


def test_the_frozen_strategy_has_a_manifest_recording_both_halves() -> None:
    """A frozen strategy without its evidence is just numbers in a file."""
    import json

    version = load_config().strategy.strategy_version
    manifest = json.loads(
        (paths.CONFIG_DIR / "frozen" / f"{version}.manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["strategy_version"] == version
    assert manifest["fingerprint"]
    assert manifest["evidence"]["research"]
    assert manifest["evidence"]["validation"]
    # The normalization is half the strategy (docs/strategy.md 2.6); a manifest
    # that recorded only the ladder could not reproduce the frozen result.
    assert manifest["indicators"]["indicators"]["rsi_14"]["normalization"]["method"]
    assert (paths.CONFIG_DIR / "frozen" / f"{version}.indicators.yaml").exists()


def test_every_enabled_indicator_points_at_a_declared_source() -> None:
    config = load_config()
    known = set(config.data_sources.price.symbols) | set(config.data_sources.series)
    for name, spec in config.indicators.enabled_indicators.items():
        assert spec.source in known, name


def test_breadth_is_disabled_with_a_stated_reason() -> None:
    # PRD.md 6.5 allows exclusion only when it is explicit.
    config = load_config()
    breadth = config.indicators.indicators["breadth_pct_above_200dma"]
    assert not breadth.enabled
    assert breadth.disabled_reason
    assert not config.data_sources.series["BREADTH_NDX"].enabled


def test_missing_config_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path)


def test_empty_config_file_is_rejected(tmp_path: Path) -> None:
    for name in ("indicators.yaml", "strategy.yaml", "alerts.yaml", "data_sources.yaml"):
        (tmp_path / name).write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="empty"):
        load_config(tmp_path)


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    payload = yaml.safe_load((paths.CONFIG_DIR / "strategy.yaml").read_text(encoding="utf-8"))
    payload["totally_unexpected"] = 1
    with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
        StrategyConfig(**payload)


# ------------------------------------------------------- the placeholder profile


def test_placeholder_profile_is_complete_but_still_blocked() -> None:
    config = load_research_placeholder_config()
    assert config.strategy.parameter_status is ParameterStatus.RESEARCH_PLACEHOLDER
    # It resolves everything, so the engines can actually run...
    assert config.unresolved_parameters() == ()
    # ...but it is still not allowed to drive production.
    assert not config.is_production_ready
    with pytest.raises(ResearchParameterError, match="RESEARCH_PLACEHOLDER"):
        ensure_production_ready(config, env={})


def test_placeholder_indicators_file_is_in_sync_with_the_real_one() -> None:
    from scripts.make_placeholder_indicators import render

    source = (paths.CONFIG_DIR / "indicators.yaml").read_text(encoding="utf-8")
    generated = render(source)
    on_disk = (
        paths.CONFIG_DIR / "research" / "placeholder.indicators.yaml"
    ).read_text(encoding="utf-8")
    assert generated == on_disk, "run: python scripts/make_placeholder_indicators.py"


def test_placeholder_weights_cover_exactly_the_enabled_indicators() -> None:
    config = load_research_placeholder_config()
    weights = config.strategy.score.weights
    assert weights is not None
    assert set(weights) == set(config.indicators.enabled_indicators)


def test_placeholder_target_leverage_falls_as_greed_rises() -> None:
    # Only the *direction* PRD.md 2.1 states is encoded; the magnitudes are arbitrary.
    from regime_monitor.constants import ASSET_LEVERAGE

    config = load_research_placeholder_config()
    labels = config.strategy.regime.labels
    mappings = config.strategy.allocation.mappings
    assert labels is not None and mappings is not None

    leverage = [
        sum(ASSET_LEVERAGE[asset] * w for asset, w in mappings[label].items())
        for label in labels
    ]
    assert leverage == sorted(leverage, reverse=True)
    assert all(value > 0 for value in leverage), "never a full exit from the market"


# ------------------------------------------------------------------ the gate


def test_gate_passes_only_for_a_frozen_and_resolved_strategy(monkeypatch) -> None:
    monkeypatch.delenv("REGIME_MONITOR_ALLOW_RESEARCH_PARAMS", raising=False)

    # The shipped config is frozen since v1.0-frozen, so it is what the gate
    # must now ACCEPT. Both directions are the point of the gate.
    ensure_production_ready(load_config())

    unresolved = load_research_placeholder_config()
    with pytest.raises(ResearchParameterError):
        ensure_production_ready(unresolved)


def test_gate_can_be_opened_explicitly_for_research(monkeypatch) -> None:
    config = load_research_placeholder_config()
    ensure_production_ready(config, env={"REGIME_MONITOR_ALLOW_RESEARCH_PARAMS": "1"})

    monkeypatch.setenv("REGIME_MONITOR_ALLOW_RESEARCH_PARAMS", "1")
    assert research_parameters_allowed()
    monkeypatch.setenv("REGIME_MONITOR_ALLOW_RESEARCH_PARAMS", "0")
    assert not research_parameters_allowed()


# ------------------------------------------------------------ schema invariants


def test_score_weights_must_sum_to_one() -> None:
    with pytest.raises(ConfigError, match=r"sum to 1\.0"):
        ScoreSpec(weights={"a": 0.4, "b": 0.4})
    ScoreSpec(weights={"a": 0.4, "b": 0.6})


def test_score_weights_cannot_be_negative() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        ScoreSpec(weights={"a": -0.1, "b": 1.1})


def test_regime_boundaries_must_be_ascending_and_match_the_count() -> None:
    with pytest.raises(ConfigError, match="ascending"):
        RegimeSpec(boundaries=(60.0, 40.0))
    with pytest.raises(ConfigError, match="cut points"):
        RegimeSpec(count=5, boundaries=(20.0, 40.0))
    RegimeSpec(count=3, labels=("a", "b", "c"), boundaries=(30.0, 70.0))


def test_unknown_is_a_reserved_regime_label() -> None:
    with pytest.raises(ConfigError, match="reserved"):
        RegimeSpec(count=3, labels=("UNKNOWN", "b", "c"))
    with pytest.raises(ConfigError, match="must not carry an allocation"):
        AllocationSpec(mappings={"UNKNOWN": {"QQQ": 1.0}})


def test_allocation_mappings_must_be_portfolios() -> None:
    with pytest.raises(ConfigError, match="sums to"):
        AllocationSpec(mappings={"Fear": {"QQQ": 0.5, "CASH": 0.2}})
    AllocationSpec(mappings={"Fear": {"QQQ": 0.5, "CASH": 0.5}})


def test_allocation_must_cover_every_regime_label() -> None:
    base = {"version": 1, "strategy_version": "t", "parameter_status": "RESEARCH"}
    with pytest.raises(ConfigError, match="missing regimes"):
        StrategyConfig(
            **base,
            regime=RegimeSpec(count=3, labels=("a", "b", "c"), boundaries=(30.0, 70.0)),
            allocation=AllocationSpec(mappings={"a": {"CASH": 1.0}, "b": {"CASH": 1.0}}),
        )


def test_same_day_execution_cannot_be_switched_on() -> None:
    # BACKTEST_SPEC.md 5 - not a preference, a hard rule.
    with pytest.raises(Exception):  # noqa: B017
        ExecutionSpec(same_day_execution_allowed=True)


def test_frozen_strategy_must_record_when_it_was_frozen() -> None:
    with pytest.raises(ConfigError, match="frozen_at"):
        StrategyConfig(version=1, strategy_version="v1.0-frozen", parameter_status="FROZEN")


def test_dataset_splits_may_not_overlap() -> None:
    with pytest.raises(ConfigError, match="overlaps"):
        DatasetSplitSpec(
            research_start="2000-01-01",
            research_end="2015-12-31",
            validation_start="2015-01-01",
            validation_end="2019-12-31",
        )


def test_cost_model_refuses_to_total_while_unresolved() -> None:
    with pytest.raises(ResearchParameterError):
        _ = CostModelSpec().total_bps
    assert CostModelSpec(commission_bps=1, spread_bps=2, slippage_bps=2).total_bps == 5


def test_telegram_config_holds_variable_names_not_secrets() -> None:
    with pytest.raises(ConfigError, match="environment variable NAME"):
        TelegramSpec(bot_token_env="123456:AAH-real-looking-token")


def test_normalization_rejects_mismatched_fields() -> None:
    from regime_monitor.config.schema import NormalizationSpec

    with pytest.raises(ConfigError, match="does not take raw_at_score"):
        NormalizationSpec(method="rolling_percentile", window=252, raw_at_score_min=0.0)
    with pytest.raises(ConfigError, match="does not take a rolling window"):
        NormalizationSpec(method="bounded", window=252)
    with pytest.raises(ConfigError, match="min_periods cannot exceed window"):
        NormalizationSpec(method="rolling_percentile", window=100, min_periods=200)


def test_indicator_research_params_must_exist_in_params() -> None:
    from regime_monitor.config.schema import IndicatorSpec, NormalizationSpec

    with pytest.raises(ConfigError, match="research_params not present"):
        IndicatorSpec(
            family="rsi",
            compute="rsi",
            params={"window": 14},
            research_params=("lookback",),
            source="QQQ",
            direction="HIGHER_IS_GREED",
            normalization=NormalizationSpec(method="rolling_percentile", window=252),
        )


def test_indicators_config_needs_at_least_one_enabled_indicator() -> None:
    with pytest.raises(ConfigError, match="enables no indicator"):
        IndicatorsConfig(
            version=1,
            indicators={
                "x": {
                    "family": "rsi",
                    "compute": "rsi",
                    "params": {"window": 14},
                    "source": "QQQ",
                    "direction": "HIGHER_IS_GREED",
                    "enabled": False,
                    "disabled_reason": "test",
                    "normalization": {"method": "rolling_percentile", "window": 252},
                }
            },
        )

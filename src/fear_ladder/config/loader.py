"""YAML configuration loading and the production/research parameter gate (TASK-002)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from fear_ladder import paths
from fear_ladder.config.schema import (
    AlertsConfig,
    AppConfig,
    ConfigError,
    DataSourcesConfig,
    IndicatorsConfig,
    ResearchParameterError,
    StrategyConfig,
)
from fear_ladder.constants import ParameterStatus

#: Set to "1" to let the pipeline run on unresolved research parameters. Never
#: set in production / GitHub Actions.
ALLOW_RESEARCH_ENV = "FEAR_LADDER_ALLOW_RESEARCH_PARAMS"

logger = logging.getLogger(__name__)

CONFIG_FILENAMES = {
    "indicators": "indicators.yaml",
    "strategy": "strategy.yaml",
    "alerts": "alerts.yaml",
    "data_sources": "data_sources.yaml",
}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if payload is None:
        raise ConfigError(f"configuration file is empty: {path}")
    if not isinstance(payload, dict):
        raise ConfigError(f"configuration file must contain a mapping: {path}")
    return payload


def _build(model: type, payload: dict[str, Any], path: Path):  # type: ignore[no-untyped-def]
    try:
        return model(**payload)
    except ConfigError as exc:
        raise ConfigError(f"{path.name} failed validation: {exc}") from exc


def load_indicators(path_or_dir: Path | None = None) -> IndicatorsConfig:
    base = path_or_dir or paths.CONFIG_DIR
    path = base / CONFIG_FILENAMES["indicators"] if base.is_dir() else base
    return _build(IndicatorsConfig, _read_yaml(path), path)


def load_strategy(path_or_dir: Path | None = None) -> StrategyConfig:
    """Load a strategy file.

    Accepts either a directory (uses ``strategy.yaml``) or a direct path, which
    is how research profiles under ``config/research/`` are loaded.
    """
    base = path_or_dir or paths.CONFIG_DIR
    path = base / CONFIG_FILENAMES["strategy"] if base.is_dir() else base
    return _build(StrategyConfig, _read_yaml(path), path)


def load_alerts(config_dir: Path | None = None) -> AlertsConfig:
    path = (config_dir or paths.CONFIG_DIR) / CONFIG_FILENAMES["alerts"]
    return _build(AlertsConfig, _read_yaml(path), path)


def load_data_sources(config_dir: Path | None = None) -> DataSourcesConfig:
    path = (config_dir or paths.CONFIG_DIR) / CONFIG_FILENAMES["data_sources"]
    return _build(DataSourcesConfig, _read_yaml(path), path)


def load_config(
    config_dir: Path | None = None,
    *,
    strategy_path: Path | None = None,
    indicators_path: Path | None = None,
) -> AppConfig:
    """Load and cross-validate the whole configuration set.

    ``strategy_path`` / ``indicators_path`` swap in a research profile without
    touching the operational files under ``config/``.

    A FROZEN strategy is paired with the indicator half frozen alongside it (see
    :func:`_frozen_indicators`), because the two were searched together and
    neither reproduces the frozen result alone.
    """
    directory = config_dir or paths.CONFIG_DIR
    try:
        strategy = load_strategy(strategy_path or directory)
        if indicators_path is None:
            indicators_path = _frozen_indicators(directory, strategy) or directory
        return AppConfig(
            indicators=load_indicators(indicators_path),
            strategy=strategy,
            alerts=load_alerts(directory),
            data_sources=load_data_sources(directory),
        )
    except ConfigError as exc:  # cross-file validator failures
        raise ConfigError(f"configuration in {directory} is inconsistent: {exc}") from exc


def _frozen_indicators(directory: Path, strategy: StrategyConfig) -> Path | None:
    """The indicator half belonging to a frozen strategy, if one was written.

    ``config/indicators.yaml`` stays hand-maintained: it holds the research
    grids and the prose, and its normalization windows are still null, so a
    frozen strategy read against it is not production-ready. ``scripts/freeze.py``
    writes the resolved half to ``config/frozen/<version>.indicators.yaml`` and
    this is what pairs them back up.
    """
    if strategy.parameter_status is not ParameterStatus.FROZEN:
        return None
    candidate = directory / FROZEN_DIR_NAME / f"{strategy.strategy_version}.indicators.yaml"
    if not candidate.exists():
        logger.warning(
            "strategy %s is FROZEN but %s is missing; falling back to %s, whose "
            "research parameters are unresolved",
            strategy.strategy_version,
            candidate,
            directory / CONFIG_FILENAMES["indicators"],
        )
        return None
    return candidate


FROZEN_DIR_NAME = "frozen"
RESEARCH_DIR_NAME = "research"
PLACEHOLDER_STRATEGY = "placeholder.strategy.yaml"
PLACEHOLDER_INDICATORS = "placeholder.indicators.yaml"


def load_research_placeholder_config(config_dir: Path | None = None) -> AppConfig:
    """Load the explicitly-labelled RESEARCH_PLACEHOLDER profile.

    Development, unit tests and exploratory backtests need *some* concrete
    numbers before the strategy is researched. Those numbers live only here, are
    stamped ``parameter_status: RESEARCH_PLACEHOLDER``, and
    :func:`ensure_production_ready` rejects them.
    """
    directory = config_dir or paths.CONFIG_DIR
    research = directory / RESEARCH_DIR_NAME
    return load_config(
        directory,
        strategy_path=research / PLACEHOLDER_STRATEGY,
        indicators_path=research / PLACEHOLDER_INDICATORS,
    )


def research_parameters_allowed(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(ALLOW_RESEARCH_ENV, "").strip() in {"1", "true", "TRUE", "yes"}


def ensure_production_ready(config: AppConfig, *, env: dict[str, str] | None = None) -> None:
    """Refuse to drive a production run from unresolved research parameters.

    ``CONTRIBUTING.md`` 10/11: RESEARCH_PLACEHOLDER values exist so
    development can proceed, but they must never reach the operational runner.
    """
    if config.is_production_ready:
        return
    unresolved = config.unresolved_parameters()
    detail = ", ".join(unresolved) if unresolved else "(none)"
    message = (
        f"strategy '{config.strategy.strategy_version}' has parameter_status="
        f"{config.strategy.parameter_status.value} and unresolved parameters: {detail}. "
        "A production run requires a FROZEN strategy (TASK-100/TASK-101)."
    )
    if research_parameters_allowed(env):
        return
    raise ResearchParameterError(message)

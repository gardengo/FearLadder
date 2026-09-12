"""TASK-101, TASK-102 — the strategy manifest and frozen regression."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame

from regime_monitor.config.loader import load_config, load_strategy
from regime_monitor.config.schema import StrategyConfig
from regime_monitor.constants import ParameterStatus
from regime_monitor.research.backtest_runner import MarketData, StrategyBacktest
from regime_monitor.research.freeze import (
    FROZEN_HEADER,
    Fingerprint,
    FreezeError,
    FreezeEvidence,
    RegressionRecord,
    freeze,
    write_strategy_yaml,
)

START = date(2015, 1, 1)


def _market(seed: int = 21, length: int = 900) -> MarketData:
    rng = np.random.default_rng(seed)
    index = pd.Index(
        [START + timedelta(days=offset) for offset in range(length)],
        name="observation_date",
    )
    daily = rng.normal(0.0004, 0.012, length)
    closes = DataFrame(
        {
            "QQQ": 100.0 * np.cumprod(1 + daily),
            "QLD": 100.0 * np.cumprod(1 + 2 * daily),
            "TQQQ": 100.0 * np.cumprod(1 + 3 * daily),
        },
        index=index,
    )
    opens = closes.shift(1).fillna(closes.iloc[0]) * 0.5 + closes * 0.5
    series = closes.copy()
    series["VIX"] = np.abs(rng.normal(18, 5, length)) + np.abs(daily) * 300
    series["CNN_FEAR_GREED"] = np.clip(
        50 + daily.cumsum() * 200 + rng.normal(0, 10, length), 0, 100
    )
    series["AAII_SENTIMENT"] = np.clip(rng.normal(0.05, 0.12, length), -1, 1)
    return MarketData(series=series, closes=closes, opens=opens)


def _evidence() -> FreezeEvidence:
    return FreezeEvidence(
        research_summary="research 2010-2014: CAGR 18%, MDD 42%",
        validation_summary="validation 2015-2019: CAGR 16%",
        oos_summary="oos 2020-2025: CAGR 15%",
        objective="cagr+1, sharpe+1, max_drawdown-1",
    )


def _researched(placeholder_config):
    """A stand-in for a *researched* config.

    The placeholder profile is complete but is labelled RESEARCH_PLACEHOLDER, so
    relabelling it to RESEARCH is the smallest way to exercise the freeze path
    without pretending these numbers were researched.
    """
    payload = placeholder_config.strategy.model_dump()
    payload["parameter_status"] = ParameterStatus.RESEARCH.value
    from regime_monitor.config.schema import AppConfig

    return AppConfig(
        indicators=placeholder_config.indicators,
        strategy=StrategyConfig(**payload),
        alerts=placeholder_config.alerts,
        data_sources=placeholder_config.data_sources,
    )


# ------------------------------------------------------------------ TASK-101


def test_freezing_produces_a_frozen_strategy_and_a_manifest(placeholder_config) -> None:
    frozen, manifest = freeze(
        _researched(placeholder_config),
        strategy_version="v1.0-frozen",
        parameter_version="p1",
        data_version="d1",
        evidence=_evidence(),
        fingerprint="abc123",
        frozen_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert frozen.parameter_status is ParameterStatus.FROZEN
    assert frozen.frozen_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert frozen.strategy_version == "v1.0-frozen"

    payload = manifest.to_dict()
    assert payload["parameter_version"] == "p1"
    assert payload["data_version"] == "d1"
    assert payload["fingerprint"] == "abc123"
    assert payload["evidence"]["out_of_sample"]
    assert payload["parameters"]["regime"]["count"] == 5


def test_a_frozen_strategy_passes_the_production_gate(placeholder_config) -> None:
    from regime_monitor.config.loader import ensure_production_ready
    from regime_monitor.config.schema import AppConfig

    frozen, _ = freeze(
        _researched(placeholder_config),
        strategy_version="v1.0-frozen",
        parameter_version="p1",
        data_version="d1",
        evidence=_evidence(),
        fingerprint="abc123",
    )
    config = AppConfig(
        indicators=placeholder_config.indicators,
        strategy=frozen,
        alerts=placeholder_config.alerts,
        data_sources=placeholder_config.data_sources,
    )
    assert config.is_production_ready
    ensure_production_ready(config, env={})


def test_the_placeholder_profile_can_never_be_frozen(placeholder_config) -> None:
    # Its numbers are arbitrary by design; freezing them would launder them.
    with pytest.raises(FreezeError, match="RESEARCH_PLACEHOLDER"):
        freeze(
            placeholder_config,
            strategy_version="v1.0-frozen",
            parameter_version="p1",
            data_version="d1",
            evidence=_evidence(),
            fingerprint="abc",
        )


def test_freezing_requires_every_parameter_to_be_settled() -> None:
    # BACKTEST_SPEC.md 27 — the operational config still has nulls.
    with pytest.raises(FreezeError, match="unresolved parameters"):
        freeze(
            load_config(),
            strategy_version="v1.0-frozen",
            parameter_version="p1",
            data_version="d1",
            evidence=_evidence(),
            fingerprint="abc",
        )


def test_a_frozen_version_must_say_it_is_frozen(placeholder_config) -> None:
    with pytest.raises(FreezeError, match="should say so"):
        freeze(
            _researched(placeholder_config),
            strategy_version="v1.0",
            parameter_version="p1",
            data_version="d1",
            evidence=_evidence(),
            fingerprint="abc",
        )


def test_the_written_strategy_file_reloads_identically(
    placeholder_config, tmp_path: Path
) -> None:
    frozen, manifest = freeze(
        _researched(placeholder_config),
        strategy_version="v1.0-frozen",
        parameter_version="p1",
        data_version="d1",
        evidence=_evidence(),
        fingerprint="abc123",
        frozen_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    path = write_strategy_yaml(frozen, tmp_path / "strategy.yaml", header=FROZEN_HEADER)
    reloaded = load_strategy(path)

    assert reloaded.parameter_status is ParameterStatus.FROZEN
    assert reloaded.strategy_version == "v1.0-frozen"
    assert reloaded.regime.boundaries == frozen.regime.boundaries
    assert reloaded.allocation.mappings == frozen.allocation.mappings
    assert reloaded.tqqq_gate.rule_params == frozen.tqqq_gate.rule_params
    assert "Do not hand-edit" in path.read_text(encoding="utf-8")

    manifest_path = manifest.write(tmp_path / "manifest.json")
    assert manifest_path.is_file()


# ------------------------------------------------------------------ TASK-102


def test_the_same_inputs_give_the_same_fingerprint(placeholder_config) -> None:
    data = _market()
    first = Fingerprint.of_run(StrategyBacktest(placeholder_config).run(data, include_benchmarks=False))
    second = Fingerprint.of_run(StrategyBacktest(placeholder_config).run(data, include_benchmarks=False))

    assert first.matches(second)
    assert first.rows == second.rows


def test_different_inputs_give_a_different_fingerprint(placeholder_config) -> None:
    original = Fingerprint.of_run(
        StrategyBacktest(placeholder_config).run(_market(seed=21), include_benchmarks=False)
    )
    other = Fingerprint.of_run(
        StrategyBacktest(placeholder_config).run(_market(seed=22), include_benchmarks=False)
    )
    assert not original.matches(other)


def test_a_changed_parameter_changes_the_fingerprint(placeholder_config) -> None:
    from regime_monitor.research.search import Candidate

    data = _market()
    base = Fingerprint.of_run(
        StrategyBacktest(placeholder_config).run(data, include_benchmarks=False)
    )
    variant_config = Candidate(
        label="v",
        overrides={
            "transition": {
                "confirmation_days": 10,
                "hysteresis": 12.0,
                "minimum_duration_days": 30,
            }
        },
    ).apply(placeholder_config)
    variant = Fingerprint.of_run(
        StrategyBacktest(variant_config).run(data, include_benchmarks=False)
    )
    assert not base.matches(variant)


def test_fingerprints_tolerate_floating_point_noise() -> None:
    index = pd.Index([START + timedelta(days=i) for i in range(3)])
    left = DataFrame({"nav": [1.0, 1.1, 1.2]}, index=index)
    right = DataFrame({"nav": [1.0, 1.1 + 1e-15, 1.2]}, index=index)
    assert Fingerprint.of_frame(left).matches(Fingerprint.of_frame(right))


def test_a_regression_record_round_trips_and_checks(tmp_path: Path) -> None:
    record = RegressionRecord(strategy_version="v1.0-frozen", fingerprint="deadbeef", rows=100)
    path = record.write(tmp_path / "regression.json")
    reloaded = RegressionRecord.read(path)

    assert reloaded.fingerprint == "deadbeef"
    reloaded.check(Fingerprint(digest="deadbeef", rows=100))


def test_a_regression_failure_says_what_changed(tmp_path: Path) -> None:
    record = RegressionRecord(strategy_version="v1.0-frozen", fingerprint="deadbeef", rows=100)
    with pytest.raises(FreezeError, match="frozen regression failed"):
        record.check(Fingerprint(digest="cafebabe", rows=100))


def test_a_frozen_strategy_reproduces_itself_across_a_reload(
    placeholder_config, tmp_path: Path
) -> None:
    """The end-to-end TASK-102 guarantee.

    Freeze, write to disk, reload from disk, re-run: the outputs must be
    identical. A round-trip that loses precision or reorders anything would
    change the advice the daily worker gives.
    """
    from regime_monitor.config.schema import AppConfig

    data = _market()
    researched = _researched(placeholder_config)
    original_run = StrategyBacktest(researched).run(data, include_benchmarks=False)
    original = Fingerprint.of_run(original_run)

    frozen, _ = freeze(
        researched,
        strategy_version="v1.0-frozen",
        parameter_version="p1",
        data_version="d1",
        evidence=_evidence(),
        fingerprint=original.digest,
    )
    path = write_strategy_yaml(frozen, tmp_path / "strategy.yaml")
    reloaded = AppConfig(
        indicators=placeholder_config.indicators,
        strategy=load_strategy(path),
        alerts=placeholder_config.alerts,
        data_sources=placeholder_config.data_sources,
    )

    replayed = Fingerprint.of_run(
        StrategyBacktest(reloaded).run(data, include_benchmarks=False)
    )
    assert original.matches(replayed), "a reloaded frozen strategy changed its output"

    RegressionRecord(
        strategy_version="v1.0-frozen", fingerprint=original.digest, rows=original.rows
    ).check(replayed)

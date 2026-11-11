"""TASK-188 — the regime anchor: where the frozen backtest's replay ended."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pandas import Series

from fear_ladder.config.loader import load_config
from fear_ladder.regime.anchor import (
    RegimeAnchor,
    anchor_from_decisions,
    anchor_path,
    load_anchor,
)
from fear_ladder.regime.classifier import RegimeError, RegimeScale
from fear_ladder.regime.transition import (
    TransitionDecision,
    TransitionEngine,
    TransitionState,
)

START = date(2024, 1, 1)
SCALE = RegimeScale(
    labels=("Capitulation", "Fear", "Neutral", "Bull", "Overheated"),
    boundaries=(20.0, 40.0, 60.0, 80.0),
)


def _anchor(**overrides: Any) -> RegimeAnchor:
    fields: dict[str, Any] = {
        "strategy_version": "v9",
        "as_of": date(2024, 3, 1),
        "state": TransitionState(
            regime="Fear",
            since=date(2024, 2, 1),
            days_held=20,
            pending_regime="Neutral",
            pending_days=1,
        ),
        "provenance": {"history_start": "1996-01-02"},
    }
    return RegimeAnchor(**{**fields, **overrides})


def test_an_anchor_survives_being_written_and_read_back(tmp_path: Path) -> None:
    anchor = _anchor()
    path = anchor.write(anchor_path("v9", tmp_path))

    assert path.name == "v9.regime_anchor.json"
    assert RegimeAnchor.read(path) == anchor
    assert load_anchor("v9", tmp_path) == anchor


def test_a_version_with_no_anchor_file_simply_has_none(tmp_path: Path) -> None:
    # A research profile has no backtest to continue, and must not need one.
    assert load_anchor("v0.0-placeholder", tmp_path) is None


def test_a_file_that_names_another_version_is_refused(tmp_path: Path) -> None:
    _anchor(strategy_version="v8").write(anchor_path("v9", tmp_path))
    with pytest.raises(RegimeError, match="v8"):
        load_anchor("v9", tmp_path)


def test_a_malformed_anchor_is_an_error_not_a_default(tmp_path: Path) -> None:
    path = anchor_path("v9", tmp_path)
    path.write_text(json.dumps({"strategy_version": "v9", "as_of": "2024-03-01"}))
    with pytest.raises(RegimeError, match="malformed"):
        load_anchor("v9", tmp_path)


@pytest.mark.parametrize(
    "state",
    [
        TransitionState(regime="UNKNOWN", since=date(2024, 2, 1)),
        TransitionState(regime="Fear", since=date(2024, 3, 2)),  # entered after as_of
        TransitionState(regime="Fear", since=date(2024, 2, 1), days_held=0),
    ],
)
def test_an_anchor_that_could_not_have_been_replayed_is_rejected(
    state: TransitionState,
) -> None:
    with pytest.raises(RegimeError):
        _anchor(state=state)


def _decisions() -> tuple[list[TransitionDecision], list[date]]:
    engine = TransitionEngine(
        scale=SCALE, confirmation_days=1, hysteresis=0.0, minimum_duration_days=2
    )
    days = [START + timedelta(days=offset) for offset in range(6)]
    scores = Series([None, 50.0, 50.0, 50.0, 10.0, 10.0], index=pd.Index(days))
    return engine.run(scores), days


def test_the_anchor_is_the_state_the_replay_was_in_on_that_day() -> None:
    decisions, days = _decisions()
    anchor = anchor_from_decisions(decisions, strategy_version="v9", as_of=days[4])

    assert anchor.as_of == days[4]
    assert anchor.state == decisions[4].state
    assert anchor.state.regime == "Capitulation"
    assert anchor.state.since == days[4]


def test_an_anchor_is_never_taken_from_a_neighbouring_day() -> None:
    decisions, days = _decisions()
    with pytest.raises(RegimeError, match="did not step"):
        anchor_from_decisions(
            decisions, strategy_version="v9", as_of=days[-1] + timedelta(days=1)
        )
    with pytest.raises(RegimeError, match="no regime yet"):
        anchor_from_decisions(decisions, strategy_version="v9", as_of=days[0])


def test_the_frozen_strategy_ships_its_anchor() -> None:
    # Without this file the worker has nothing of the backtest's to resume from
    # on an empty database, and falls back to a replay whose answer depends on
    # where its window starts. Deleting it should fail here, not in production.
    config = load_config()
    strategy = config.strategy
    if not config.is_production_ready:
        pytest.skip("no frozen strategy configured")

    anchor = load_anchor(strategy.strategy_version)
    assert anchor is not None, "config/frozen/<version>.regime_anchor.json is missing"
    assert anchor.as_of == strategy.dataset_split.oos_end, (
        "the anchor is the end of the frozen backtest window"
    )
    assert anchor.state.regime in (strategy.regime.labels or ())

    regression = json.loads(
        anchor_path(strategy.strategy_version)
        .with_name(f"{strategy.strategy_version}.regression.json")
        .read_text(encoding="utf-8")
    )
    assert anchor.provenance.get("regression_fingerprint") == regression["fingerprint"], (
        "an anchor must come from a replay that reproduced the frozen backtest"
    )

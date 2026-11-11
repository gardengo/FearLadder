"""Where a frozen strategy's regime replay stands at the end of its backtest.

The transition rules never forget the day they started on (see
:mod:`fear_ladder.regime.transition`), so "which rung is it today?" has no
answer until someone says where the count began. The frozen backtest begins on
the first day of its history, in 1996. The daily worker cannot: the operational
database keeps five years, and what it loads from it starts a day later every
day.

An anchor closes that gap. It is the state the frozen backtest's own replay was
in on its last day, written once to ``config/frozen/<version>.regime_anchor.json``
by ``scripts/make_regime_anchor.py`` from the full history. The worker resumes
from it the first time it runs, and from its own stored
:class:`~fear_ladder.data.models.RegimeMemory` every day after — so the live
regime is the backtest's replay continued, not a second replay with a different
beginning.

This is evidence, not a parameter. It is derived entirely from the frozen
configuration and the history; nothing here can be tuned.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Self

from fear_ladder import paths
from fear_ladder.config.loader import FROZEN_DIR_NAME
from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.regime.classifier import RegimeError
from fear_ladder.regime.transition import TransitionDecision, TransitionState

ANCHOR_SUFFIX = ".regime_anchor.json"


@dataclass(frozen=True, slots=True)
class RegimeAnchor:
    """The replay state of one strategy version at the close of ``as_of``."""

    strategy_version: str
    as_of: date
    state: TransitionState
    #: How it was derived — history covered, steps replayed, the regression
    #: fingerprint it was checked against. For a reader, not for the engine.
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.strategy_version:
            raise RegimeError("a regime anchor must name its strategy version")
        if not self.state.regime or self.state.regime == UNKNOWN_REGIME:
            raise RegimeError("a regime anchor must hold a known regime")
        if self.state.since > self.as_of:
            raise RegimeError(
                f"anchor entered {self.state.regime} on {self.state.since}, "
                f"after its own date {self.as_of}"
            )
        if self.state.days_held < 1:
            raise RegimeError("an anchor's days_held starts at 1")

    def to_payload(self) -> dict[str, Any]:
        return {
            "strategy_version": self.strategy_version,
            "as_of": self.as_of.isoformat(),
            "state": {
                "regime": self.state.regime,
                "since": self.state.since.isoformat(),
                "days_held": self.state.days_held,
                "pending_regime": self.state.pending_regime,
                "pending_days": self.state.pending_days,
            },
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        try:
            state = payload["state"]
            return cls(
                strategy_version=str(payload["strategy_version"]),
                as_of=date.fromisoformat(payload["as_of"]),
                state=TransitionState(
                    regime=str(state["regime"]),
                    since=date.fromisoformat(state["since"]),
                    days_held=int(state["days_held"]),
                    pending_regime=state.get("pending_regime"),
                    pending_days=int(state.get("pending_days", 0)),
                ),
                provenance=dict(payload.get("provenance", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RegimeError(f"malformed regime anchor: {exc!r}") from exc

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_payload(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def read(cls, path: Path) -> Self:
        return cls.from_payload(json.loads(path.read_text(encoding="utf-8")))


def anchor_from_decisions(
    decisions: Sequence[TransitionDecision],
    *,
    strategy_version: str,
    as_of: date,
    provenance: Mapping[str, Any] | None = None,
) -> RegimeAnchor:
    """The anchor a finished replay leaves at the close of ``as_of``.

    ``as_of`` must be a day the replay actually stepped on. Taking the nearest
    earlier day instead would write a file whose date says one thing and whose
    count says another, and the worker would step the days in between twice.
    """
    for decision in reversed(decisions):
        if decision.observation_date == as_of:
            if decision.state is None or decision.regime == UNKNOWN_REGIME:
                raise RegimeError(f"the replay had no regime yet on {as_of}")
            return RegimeAnchor(
                strategy_version=strategy_version,
                as_of=as_of,
                state=decision.state,
                provenance=dict(provenance or {}),
            )
    raise RegimeError(f"the replay did not step on {as_of}; pick a day it covers")


def anchor_path(strategy_version: str, directory: Path | None = None) -> Path:
    """Where the anchor of ``strategy_version`` lives (whether or not it exists)."""
    base = directory if directory is not None else paths.CONFIG_DIR / FROZEN_DIR_NAME
    return base / f"{strategy_version}{ANCHOR_SUFFIX}"


def load_anchor(strategy_version: str, directory: Path | None = None) -> RegimeAnchor | None:
    """The anchor written for ``strategy_version``, or ``None`` if there is none.

    A research profile has no anchor and needs none. A file that exists but
    belongs to another version is an error rather than a silent ``None``: the
    filename and the contents disagreeing means someone edited one of them.
    """
    path = anchor_path(strategy_version, directory)
    if not path.is_file():
        return None
    anchor = RegimeAnchor.read(path)
    if anchor.strategy_version != strategy_version:
        raise RegimeError(
            f"{path.name} holds an anchor for {anchor.strategy_version}, "
            f"not {strategy_version}"
        )
    return anchor

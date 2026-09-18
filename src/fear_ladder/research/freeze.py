"""Strategy freeze (TASK-101) and frozen regression (TASK-102).

``BACKTEST_SPEC.md`` 27 lists what must be settled before a freeze::

    indicator set, normalization, weights, regime count, boundaries,
    allocation, TQQQ rules, confirmation, hysteresis, execution, cost model

and §28 says the operational worker then uses only the frozen version.

Two things are enforced here rather than trusted:

* :func:`freeze` refuses to stamp a strategy that still has unresolved
  parameters, or one that is still labelled RESEARCH_PLACEHOLDER. A freeze is a
  claim that research happened.
* :class:`Fingerprint` makes "identical input, identical output" checkable. The
  daily worker re-derives today's state from scratch every run, so any
  accidental non-determinism would quietly change advice between runs.

What this module deliberately does **not** do is choose the numbers. That is
TASK-100, and it is a research result plus a human decision, not something code
may invent (``CONTRIBUTING.md`` §7, §10, §15).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pandas import DataFrame

from fear_ladder.config.schema import AppConfig, IndicatorsConfig, StrategyConfig
from fear_ladder.constants import ParameterStatus
from fear_ladder.research.backtest_runner import BacktestRun

logger = logging.getLogger(__name__)

#: Everything ``BACKTEST_SPEC.md`` 27 requires to be settled.
FREEZE_REQUIREMENTS = (
    "score.weights",
    "regime",
    "transition",
    "allocation.mappings",
    "tqqq_gate",
    "cost_model",
    "dataset_split",
)


class FreezeError(RuntimeError):
    """Raised when a strategy cannot be frozen as presented."""


@dataclass(frozen=True, slots=True)
class FreezeEvidence:
    """Why this parameter set was chosen.

    A manifest without evidence is just a number dump. Recording the windows,
    the objective and the out-of-sample result is what lets someone later ask
    "was this justified?" rather than "what was it?".
    """

    research_summary: str
    validation_summary: str | None = None
    oos_summary: str | None = None
    objective: str | None = None
    walk_forward_summary: str | None = None
    sensitivity_summaries: tuple[str, ...] = ()
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "research": self.research_summary,
            "validation": self.validation_summary,
            "out_of_sample": self.oos_summary,
            "objective": self.objective,
            "walk_forward": self.walk_forward_summary,
            "sensitivity": list(self.sensitivity_summaries),
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class StrategyManifest:
    """The record TASK-101 asks for."""

    strategy_version: str
    parameter_version: str
    data_version: str
    frozen_at: datetime
    code_commit: str | None
    parameters: dict[str, Any]
    evidence: dict[str, Any]
    fingerprint: str
    #: The indicator half of the profile. A strategy is only reproducible with
    #: the normalization it was searched against (docs/strategy.md 2.6), so the
    #: manifest records both halves or neither.
    indicators: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_version": self.strategy_version,
            "parameter_version": self.parameter_version,
            "data_version": self.data_version,
            "frozen_at": self.frozen_at.isoformat(),
            "code_commit": self.code_commit,
            "fingerprint": self.fingerprint,
            "parameters": self.parameters,
            "indicators": self.indicators,
            "evidence": self.evidence,
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return path


def freeze(
    config: AppConfig,
    *,
    strategy_version: str,
    parameter_version: str,
    data_version: str,
    evidence: FreezeEvidence,
    fingerprint: str,
    code_commit: str | None = None,
    frozen_at: datetime | None = None,
) -> tuple[StrategyConfig, StrategyManifest]:
    """Stamp a researched parameter set as FROZEN.

    Returns the frozen :class:`StrategyConfig` and its manifest. Writing them to
    disk is the caller's decision, so a dry run is possible.
    """
    if config.strategy.parameter_status is ParameterStatus.RESEARCH_PLACEHOLDER:
        raise FreezeError(
            "refusing to freeze the RESEARCH_PLACEHOLDER profile. Its numbers are "
            "deliberately arbitrary and exist only so the code can run "
            "(CONTRIBUTING.md 10)."
        )
    unresolved = config.unresolved_parameters()
    if unresolved:
        raise FreezeError(
            f"cannot freeze with {len(unresolved)} unresolved parameters: "
            f"{', '.join(unresolved)}. BACKTEST_SPEC.md 27 requires all of "
            f"{', '.join(FREEZE_REQUIREMENTS)} to be settled first."
        )
    if not strategy_version.endswith("-frozen"):
        raise FreezeError(
            f"a frozen strategy version should say so, e.g. 'v1.0-frozen'; "
            f"got {strategy_version!r}"
        )

    moment = frozen_at or datetime.now(tz=UTC)
    payload = config.strategy.model_dump()
    payload.update(
        strategy_version=strategy_version,
        parameter_status=ParameterStatus.FROZEN.value,
        parameter_version=parameter_version,
        data_version=data_version,
        frozen_at=moment,
    )
    frozen = StrategyConfig(**payload)

    manifest = StrategyManifest(
        strategy_version=strategy_version,
        parameter_version=parameter_version,
        data_version=data_version,
        frozen_at=moment,
        code_commit=code_commit,
        parameters=_serialisable(frozen),
        evidence=evidence.to_dict(),
        fingerprint=fingerprint,
        indicators=json.loads(config.indicators.model_dump_json()),
    )
    logger.info("froze %s (fingerprint %s)", strategy_version, fingerprint[:12])
    return frozen, manifest


def write_strategy_yaml(strategy: StrategyConfig, path: Path, *, header: str = "") -> Path:
    """Write a frozen strategy to ``config/strategy.yaml``."""
    return _write_yaml(json.loads(strategy.model_dump_json()), path, header)


def write_indicators_yaml(indicators: IndicatorsConfig, path: Path, *, header: str = "") -> Path:
    """Write the resolved indicator half to ``config/indicators.yaml``.

    Freezing the strategy alone would leave production reading a file whose
    normalization windows are still ``null``. The two were searched together
    and only mean anything together.
    """
    return _write_yaml(json.loads(indicators.model_dump_json()), path, header)


def _write_yaml(payload: dict[str, Any], path: Path, header: str) -> Path:
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((header + body) if header else body, encoding="utf-8")
    return path


FROZEN_INDICATORS_HEADER = """# FROZEN indicator half.
#
# Generated by scripts/freeze.py next to the manifest that evidences it. The
# strategy and the indicators were searched together and are only meaningful
# together (docs/strategy.md 2.6), so this is the file config/strategy.yaml
# must be paired with. Do not hand-edit.
#
# This is NOT config/indicators.yaml. That one stays hand-maintained: it carries
# the research grids and the prose, and scripts/make_placeholder_indicators.py
# transforms its text.

"""

FROZEN_HEADER = """# strategy.yaml — FROZEN.
#
# Generated by scripts/freeze.py. Do not hand-edit: the manifest under
# config/frozen/ records the evidence for these exact numbers, and editing here
# silently invalidates it. To change the strategy, run the research again and
# freeze a new version (BACKTEST_SPEC.md 27, ARCHITECTURE.md 14).
#
# PAIRS WITH config/frozen/{indicators}. The normalization was searched with
# these parameters and neither half reproduces the frozen result alone.

"""


# ------------------------------------------------------------------ TASK-102


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """A stable hash of a backtest's outputs.

    Hashing the *outputs* rather than the inputs is the point: it catches
    non-determinism anywhere in the chain, including changes that leave the
    configuration identical.
    """

    digest: str
    rows: int
    columns: tuple[str, ...] = ()

    @classmethod
    def of_run(cls, run: BacktestRun) -> Fingerprint:
        frame = DataFrame(
            {
                "composite_score": run.composite_score,
                "regime": run.regimes,
                "nav": run.result.nav,
                "target_leverage": run.result.target_leverage,
            }
        ).sort_index()
        return cls.of_frame(frame)

    @classmethod
    def of_frame(cls, frame: DataFrame) -> Fingerprint:
        digest = hashlib.sha256()
        digest.update(",".join(str(column) for column in frame.columns).encode("utf-8"))
        for day, row in frame.iterrows():
            digest.update(str(day).encode("utf-8"))
            for value in row:
                digest.update(_canonical(value).encode("utf-8"))
        return cls(
            digest=digest.hexdigest(),
            rows=len(frame),
            columns=tuple(str(column) for column in frame.columns),
        )

    def matches(self, other: Fingerprint) -> bool:
        return self.digest == other.digest


def _canonical(value: Any) -> str:
    """Round floats before hashing so bit-level noise is not a false alarm."""
    if value is None:
        return "null"
    if isinstance(value, float):
        if value != value:  # NaN
            return "nan"
        return f"{value:.10g}"
    return str(value)


@dataclass(frozen=True, slots=True)
class RegressionRecord:
    """A stored fingerprint the frozen strategy must keep reproducing."""

    strategy_version: str
    fingerprint: str
    rows: int
    recorded_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_version": self.strategy_version,
            "fingerprint": self.fingerprint,
            "rows": self.rows,
            "recorded_at": self.recorded_at.isoformat(),
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: Path) -> RegressionRecord:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            strategy_version=payload["strategy_version"],
            fingerprint=payload["fingerprint"],
            rows=payload["rows"],
            recorded_at=datetime.fromisoformat(payload["recorded_at"]),
        )

    def check(self, fingerprint: Fingerprint) -> None:
        """Raise if the frozen strategy no longer reproduces itself."""
        if fingerprint.matches(Fingerprint(self.fingerprint, self.rows)):
            return
        raise FreezeError(
            f"frozen regression failed for {self.strategy_version}: expected "
            f"{self.fingerprint[:16]}... over {self.rows} rows, got "
            f"{fingerprint.digest[:16]}... over {fingerprint.rows} rows. "
            "The same inputs must give the same outputs (TASK-102)."
        )


def _serialisable(strategy: StrategyConfig) -> dict[str, Any]:
    return json.loads(strategy.model_dump_json())

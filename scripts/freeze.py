"""Freeze a researched strategy (TASK-100 execution, TASK-101, TASK-102).

    python scripts/freeze.py --candidate config/research/v1.0-candidate.yaml \
        --version v1.0-frozen --parameter-version p1 --data-version d1 \
        --evidence-research "..." --evidence-oos "..."

What this tool does **not** do is choose the numbers. TASK-100 is a research
result plus a human decision; ``CLAUDE_CODE_INITIAL_PROMPT.md`` §7 and §10 are
explicit that a developer may not invent weights, thresholds, boundaries or
allocations. So the workflow is:

1. run ``scripts/backtest.py`` and the searches in ``fear_ladder.research``
2. validate on the validation window, then walk-forward
3. write the chosen numbers into a candidate strategy yaml
4. run this tool, which verifies completeness, stamps FROZEN, records the
   manifest with its evidence, and stores the regression fingerprint

Step 4 refuses if anything is still ``null`` or if the candidate is the
RESEARCH_PLACEHOLDER profile.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.research.backtest_runner import StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.freeze import (
    FROZEN_HEADER,
    FROZEN_INDICATORS_HEADER,
    Fingerprint,
    FreezeError,
    FreezeEvidence,
    RegressionRecord,
    freeze,
    write_indicators_yaml,
    write_strategy_yaml,
)
from fear_ladder.research.reports import code_commit

logger = logging.getLogger("freeze")

FROZEN_DIR = paths.CONFIG_DIR / "frozen"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate",
        type=Path,
        required=True,
        help="strategy yaml holding the researched parameters",
    )
    parser.add_argument(
        "--indicators",
        type=Path,
        default=None,
        help=(
            "the indicators yaml this candidate was searched against. The two "
            "halves are only meaningful together; without this the default "
            "config/indicators.yaml is read and its research parameters are "
            "still unresolved, so the freeze refuses."
        ),
    )
    parser.add_argument("--version", required=True, help="e.g. v1.0-frozen")
    parser.add_argument("--parameter-version", required=True)
    parser.add_argument("--data-version", required=True)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--evidence-research", required=True)
    parser.add_argument("--evidence-validation", default=None)
    parser.add_argument("--evidence-oos", default=None)
    parser.add_argument("--evidence-walk-forward", default=None)
    parser.add_argument("--notes", default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="overwrite config/strategy.yaml (without this it is a dry run)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()

    config = load_config(strategy_path=args.candidate, indicators_path=args.indicators)
    logger.info(
        "candidate %s (%s)",
        config.strategy.strategy_version,
        config.strategy.parameter_status.value,
    )

    with SQLiteUnitOfWork(args.db) as uow:
        # As in scripts/backtest.py: --start windows the simulation, not the
        # data, so the indicators keep their warm-up.
        data = load_market_data(uow.observations, config, end=args.end)

    run = StrategyBacktest(config).run(
        data, start=args.start, end=args.end, include_benchmarks=True
    )
    fingerprint = Fingerprint.of_run(run)
    logger.info("fingerprint %s over %d rows", fingerprint.digest[:16], fingerprint.rows)
    print(run.comparison().to_string())

    evidence = FreezeEvidence(
        research_summary=args.evidence_research,
        validation_summary=args.evidence_validation,
        oos_summary=args.evidence_oos,
        walk_forward_summary=args.evidence_walk_forward,
        notes=args.notes,
    )

    try:
        frozen, manifest = freeze(
            config,
            strategy_version=args.version,
            parameter_version=args.parameter_version,
            data_version=args.data_version,
            evidence=evidence,
            fingerprint=fingerprint.digest,
            code_commit=code_commit(),
        )
    except FreezeError as exc:
        logger.error("%s", exc)
        return 2

    manifest_path = FROZEN_DIR / f"{args.version}.manifest.json"
    regression_path = FROZEN_DIR / f"{args.version}.regression.json"
    # The indicator half goes NEXT TO the manifest, not over config/indicators.yaml.
    # That file is hand-maintained and scripts/make_placeholder_indicators.py
    # transforms its *text*, so a machine dump over it breaks the placeholder
    # profile and loses the prose the indicator set is documented in.
    indicators_path = FROZEN_DIR / f"{args.version}.indicators.yaml"

    if not args.apply:
        logger.warning("dry run — nothing written. Re-run with --apply to commit the freeze.")
        logger.info("would write %s", manifest_path)
        logger.info("would write %s", regression_path)
        logger.info("would write %s", indicators_path)
        logger.info("would overwrite %s", paths.CONFIG_DIR / "strategy.yaml")
        return 0

    manifest.write(manifest_path)
    RegressionRecord(
        strategy_version=args.version,
        fingerprint=fingerprint.digest,
        rows=fingerprint.rows,
    ).write(regression_path)
    write_indicators_yaml(
        config.indicators, indicators_path, header=FROZEN_INDICATORS_HEADER
    )
    write_strategy_yaml(
        frozen,
        paths.CONFIG_DIR / "strategy.yaml",
        header=FROZEN_HEADER.format(indicators=indicators_path.name),
    )

    logger.info("wrote %s", manifest_path)
    logger.info("wrote %s", regression_path)
    logger.info("wrote %s", indicators_path)
    logger.info("froze %s into config/strategy.yaml", args.version)
    logger.warning(
        "commit config/strategy.yaml and config/frozen/ together: the manifest is "
        "the evidence for exactly these numbers"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

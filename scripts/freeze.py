"""Freeze a researched strategy (TASK-100 execution, TASK-101, TASK-102).

    python scripts/freeze.py --candidate config/research/v1.0-candidate.yaml \
        --version v1.0-frozen --parameter-version p1 --data-version d1 \
        --evidence-research "..." --evidence-oos "..."

What this tool does **not** do is choose the numbers. TASK-100 is a research
result plus a human decision; ``CLAUDE_CODE_INITIAL_PROMPT.md`` §7 and §10 are
explicit that a developer may not invent weights, thresholds, boundaries or
allocations. So the workflow is:

1. run ``scripts/backtest.py`` and the searches in ``regime_monitor.research``
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

from regime_monitor import paths
from regime_monitor.config.loader import load_config
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork
from regime_monitor.monitoring.logging import configure_logging
from regime_monitor.research.backtest_runner import StrategyBacktest
from regime_monitor.research.data_loader import load_market_data
from regime_monitor.research.freeze import (
    FROZEN_HEADER,
    Fingerprint,
    FreezeError,
    FreezeEvidence,
    RegressionRecord,
    freeze,
    write_strategy_yaml,
)
from regime_monitor.research.reports import code_commit

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

    config = load_config(strategy_path=args.candidate)
    logger.info(
        "candidate %s (%s)",
        config.strategy.strategy_version,
        config.strategy.parameter_status.value,
    )

    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config, start=args.start, end=args.end)

    run = StrategyBacktest(config).run(data, include_benchmarks=True)
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

    if not args.apply:
        logger.warning("dry run — nothing written. Re-run with --apply to commit the freeze.")
        logger.info("would write %s", manifest_path)
        logger.info("would write %s", regression_path)
        logger.info("would overwrite %s", paths.CONFIG_DIR / "strategy.yaml")
        return 0

    manifest.write(manifest_path)
    RegressionRecord(
        strategy_version=args.version,
        fingerprint=fingerprint.digest,
        rows=fingerprint.rows,
    ).write(regression_path)
    write_strategy_yaml(frozen, paths.CONFIG_DIR / "strategy.yaml", header=FROZEN_HEADER)

    logger.info("wrote %s", manifest_path)
    logger.info("wrote %s", regression_path)
    logger.info("froze %s into config/strategy.yaml", args.version)
    logger.warning(
        "commit config/strategy.yaml and config/frozen/ together: the manifest is "
        "the evidence for exactly these numbers"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

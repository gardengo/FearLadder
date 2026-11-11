"""Write the frozen strategy's regime anchor — where its backtest replay ended.

    python scripts/collect_full_history.py --out path/to/full.db
    python scripts/make_regime_anchor.py --db path/to/full.db

The daily worker resumes the regime replay instead of restarting it
(``ARCHITECTURE.md`` §4.1.1). The first time it runs it has nothing of its own
to resume from, so it resumes from this file: the state the frozen backtest's
replay was in on the last day of the frozen window.

**It needs the full history, and proves it has it.** The transition rules never
forget the day they started on, so an anchor replayed from a database that
begins in 2021 is a different anchor — same file, same shape, a different
regime today. The operational database is pruned to five years, so this script
is pointed at a rebuilt one (``scripts/collect_full_history.py``), and before it
writes anything it re-runs the research window and checks the result against
the fingerprint stored at freeze time. A replay that reproduces the frozen
backtest is the frozen backtest's replay; one that does not is refused.

**It is evidence, not a setting.** Nothing is chosen here. Re-running it against
the same history gives the same file, and an existing anchor is not overwritten
without ``--force`` — replacing it moves the live regime onto another path, and
that should never happen by accident.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder import paths
from fear_ladder.config.loader import FROZEN_DIR_NAME, load_config
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.regime.anchor import anchor_from_decisions, anchor_path
from fear_ladder.research.backtest_runner import StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.freeze import Fingerprint, FreezeError, RegressionRecord

logger = logging.getLogger("regime_anchor")

FROZEN_DIR = paths.CONFIG_DIR / FROZEN_DIR_NAME
#: How many of the latest transitions to list in the file, for a reader.
RECENT_TRANSITIONS = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, required=True,
        help="a database holding the full history (not the pruned operational one)",
    )
    parser.add_argument(
        "--as-of", type=date.fromisoformat, default=None,
        help="the day to anchor on (default: the last day of the frozen window)",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--force", action="store_true", help="replace an anchor that already exists"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)

    config = load_config()
    strategy = config.strategy
    version = strategy.strategy_version
    logger.info("%s (%s)", version, strategy.parameter_status.value)
    if not config.is_production_ready:
        logger.error("only a frozen strategy has a backtest to anchor to")
        return 2

    split = strategy.dataset_split
    as_of = args.as_of or split.oos_end
    if as_of is None:
        logger.error("the strategy declares no oos_end; pass --as-of")
        return 2

    out = args.out or anchor_path(version)
    if out.exists() and not args.force:
        logger.error(
            "%s already exists. Replacing it moves the live regime onto another "
            "path; pass --force if that is what you mean.",
            out,
        )
        return 2

    regression_path = FROZEN_DIR / f"{version}.regression.json"
    if not regression_path.is_file():
        logger.error("no %s to check the replay against", regression_path.name)
        return 2
    regression = RegressionRecord.read(regression_path)

    if args.db.resolve() == paths.default_db_path().resolve():
        logger.error(
            "%s is the operational database: it is pruned to five years, and an "
            "anchor replayed from it would start in the wrong place. Rebuild the "
            "full history with scripts/collect_full_history.py and pass that.",
            args.db,
        )
        return 2

    with SQLiteUnitOfWork(args.db) as uow:
        data = load_market_data(uow.observations, config, end=as_of)
    history_start = data.series.index[0]
    logger.info(
        "history %s .. %s (%d days)", history_start, data.series.index[-1], len(data.series)
    )
    if split.research_start is not None and history_start > split.research_start:
        logger.error(
            "the history starts %s, after the research window opens (%s). The "
            "frozen backtest cannot be reproduced from it, so there is nothing "
            "to anchor to.",
            history_start,
            split.research_start,
        )
        return 2

    backtest = StrategyBacktest(config)
    research = backtest.run(
        data, start=split.research_start, end=split.research_end, include_benchmarks=False
    )
    try:
        regression.check(Fingerprint.of_run(research))
    except FreezeError as exc:
        logger.error(
            "%s\nThis history does not reproduce the frozen backtest, so its "
            "replay is not the one to anchor to. The usual cause is a database "
            "that does not reach back to the start of the reconstructed history.",
            exc,
        )
        return 2
    logger.info("research window reproduces the frozen fingerprint %s", regression.fingerprint[:16])

    run = backtest.run(data, include_benchmarks=False)
    decisions = run.decisions
    regimes = [decision for decision in decisions if decision.regime != "UNKNOWN"]
    changes = [decision for decision in decisions if decision.changed]
    anchor = anchor_from_decisions(
        decisions,
        strategy_version=version,
        as_of=as_of,
        provenance={
            "history_start": str(data.series.index[0]),
            "first_regime_date": str(regimes[0].observation_date) if regimes else None,
            "days_replayed": len(decisions),
            "regime_changes": len(changes),
            "recent_transitions": [
                {
                    "date": str(change.observation_date),
                    "from": change.previous_regime,
                    "to": change.regime,
                }
                for change in changes[-RECENT_TRANSITIONS:]
            ],
            "regression_fingerprint": regression.fingerprint,
            "data_version": strategy.data_version,
            "parameter_version": strategy.parameter_version,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    anchor.write(out)
    logger.info(
        "wrote %s: %s since %s, held %d days as of %s",
        out, anchor.state.regime, anchor.state.since, anchor.state.days_held, anchor.as_of,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

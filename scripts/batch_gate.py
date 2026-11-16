"""Should this scheduled attempt run the daily worker? (TASK-189)

    python scripts/batch_gate.py [--manual] [--last-attempt]

The workflow is scheduled several times a morning because GitHub starts
scheduled workflows hours late; this decides which attempt does the work. The
rules and the reasons are in :mod:`fear_ladder.pipeline.gate`.

Writes ``run=true|false`` and ``reason=...`` to ``$GITHUB_OUTPUT`` (or to
stdout when that is not set) and always exits 0. It opens the database
read-only: an attempt that decides to do nothing must leave nothing to commit.

If the gate itself breaks, only the last attempt of the day (and a manual run)
goes ahead. That is the schedule this project ran on before the gate existed,
so a broken gate costs earliness and nothing else.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.data.collectors.registry import build_registry
from fear_ladder.data.repositories.connection import connect
from fear_ladder.monitoring.logging import configure_logging
from fear_ladder.pipeline.gate import (
    FeedProbe,
    GateDecision,
    decide,
    probe_feeds,
    recorded_through,
)

logger = logging.getLogger("batch_gate")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual", action="store_true", help="a person started this run")
    parser.add_argument(
        "--last-attempt",
        action="store_true",
        help="no later attempt is scheduled today; run even if the feeds carry nothing new",
    )
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="decide as of this date instead of today (UTC)",
    )
    return parser


def evaluate(args: argparse.Namespace) -> GateDecision:
    config = load_config()
    today = args.today or datetime.now(tz=UTC).date()

    target = args.db or paths.default_db_path()
    recorded = None
    if Path(target).is_file():
        connection = connect(target, read_only=True)
        try:
            recorded = recorded_through(connection, config.strategy.strategy_version)
        finally:
            connection.close()
    logger.info("recorded through %s; today is %s (UTC)", recorded, today)

    def probe() -> FeedProbe:
        feeds = probe_feeds(
            config.data_sources, build_registry(config.data_sources), today=today
        )
        for name, newest in sorted(feeds.latest.items()):
            logger.info("feed %s: newest row %s", name, newest)
        for name, error in sorted(feeds.unreadable.items()):
            logger.warning("feed %s: unreadable (%s)", name, error)
        return feeds

    return decide(
        today=today,
        recorded=recorded,
        manual=args.manual,
        last_attempt=args.last_attempt,
        probe=probe,
    )


def emit(decision: GateDecision) -> None:
    lines = f"run={'true' if decision.run else 'false'}\nreason={decision.reason}\n"
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with Path(target).open("a", encoding="utf-8") as handle:
            handle.write(lines)
    else:
        sys.stdout.write(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.INFO)
    try:
        decision = evaluate(args)
    except Exception as exc:  # the gate must never be the reason nothing ran
        logger.exception("the gate failed")
        fallback = args.manual or args.last_attempt
        decision = GateDecision(
            fallback,
            f"gate failed ({type(exc).__name__}); "
            + ("running anyway" if fallback else "leaving it to the last attempt"),
        )
    logger.info("%s: %s", "RUN" if decision.run else "SKIP", decision.reason)
    emit(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

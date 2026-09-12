"""Logging setup.

The daily worker's only window into a failed run is the GitHub Actions log, so
messages are timestamped and the logger name is always shown: "which stage was
this?" has to be answerable from the log alone.
"""

from __future__ import annotations

import logging
import os
import sys

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

LEVEL_ENV = "REGIME_MONITOR_LOG_LEVEL"


def configure_logging(level: int | None = None, *, stream: object | None = None) -> None:
    """Configure the root logger once, idempotently."""
    resolved = level if level is not None else _level_from_env()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stderr)  # type: ignore[arg-type]
    handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(resolved)

    # These are noisy and say nothing about the pipeline.
    for noisy in ("urllib3", "requests", "matplotlib"):
        logging.getLogger(noisy).setLevel(max(resolved, logging.WARNING))


def _level_from_env() -> int:
    name = os.environ.get(LEVEL_ENV, "INFO").upper()
    return getattr(logging, name, logging.INFO)

"""Dashboard views, one module per tab.

Each module exposes ``render()`` and reads only through
:mod:`views.common`. No compute module is imported anywhere under ``app/`` —
the dashboard shows what the daily worker computed and what the freeze
measured, and recomputing either here would let the page disagree with the
system of record (``ARCHITECTURE.md`` §4.2).
"""

from __future__ import annotations

from views import events, history, operations, performance, strategy, today

__all__ = [
    "events",
    "history",
    "operations",
    "performance",
    "strategy",
    "today",
]

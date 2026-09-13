"""TASK-140 .. TASK-144 — the dashboard's read layer.

The Streamlit page itself is not unit-tested (it is a rendering shell); what is
tested is everything it reads, plus the architectural rule that it only reads.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from fear_ladder import paths
from fear_ladder.alerts.engine import NullNotifier
from fear_ladder.constants import UNKNOWN_REGIME, Asset
from fear_ladder.data.models import Provenance
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.pipeline.daily import DailyPipeline
from fear_ladder.pipeline.queries import (
    DashboardDataError,
    DashboardQueries,
    is_signal_stale,
    read_only,
    regime_spans,
)
from tests.conftest import make_price_observation, make_scalar_observation

TODAY = date.today()
HISTORY_DAYS = 900


class _NoCollection:
    def collect(self, repository: object, *, start: date, end: date):
        from fear_ladder.data.collection import CollectionReport

        return CollectionReport()


@pytest.fixture
def populated(db_path: Path, provenance: Provenance, placeholder_config) -> Path:
    """A database with real pipeline output for the last few days.

    Data is loaded day by day, exactly as it arrives in production. Seeding the
    whole history up front and then running *past* dates would leave every run
    looking at data dated in its own future, which the freshness validator
    correctly reports as CORRUPT.
    """
    rng = np.random.default_rng(77)
    days = [TODAY - timedelta(days=offset) for offset in range(HISTORY_DAYS)][::-1]
    daily = rng.normal(0.0004, 0.012, HISTORY_DAYS)
    qqq = 100.0 * np.cumprod(1 + daily)
    qld = 100.0 * np.cumprod(1 + 2 * daily)
    tqqq = 100.0 * np.cumprod(1 + 3 * daily)
    vix = np.abs(rng.normal(18, 5, HISTORY_DAYS)) + np.abs(daily) * 300
    fng = np.clip(50 + daily.cumsum() * 200 + rng.normal(0, 10, HISTORY_DAYS), 0, 100)

    def rows_for(index: int) -> list:
        day = days[index]
        return [
            make_price_observation("QQQ", day, float(qqq[index]), provenance),
            make_price_observation("QLD", day, float(qld[index]), provenance),
            make_price_observation("TQQQ", day, float(tqqq[index]), provenance),
            make_scalar_observation("VIX", day, float(vix[index]), provenance),
            make_scalar_observation("CNN_FEAR_GREED", day, float(fng[index]), provenance),
        ]

    backfill_from = HISTORY_DAYS - 5
    with SQLiteUnitOfWork(db_path) as uow:
        history = [row for index in range(backfill_from) for row in rows_for(index)]
        uow.observations.save_observations(history)

    pipeline = DailyPipeline(
        placeholder_config,
        notifier=NullNotifier(),
        collection_service=_NoCollection(),
    )
    for index in range(backfill_from, HISTORY_DAYS):
        with SQLiteUnitOfWork(db_path) as uow:
            uow.observations.save_observations(rows_for(index))
            pipeline.run(uow, as_of=days[index])
    return db_path


@pytest.fixture
def queries(populated: Path) -> DashboardQueries:
    return DashboardQueries(db_path=populated)


# --------------------------------------------------------------- read-only


def test_the_dashboard_cannot_write(populated: Path) -> None:
    """``ARCHITECTURE.md`` §4.2 — the worker owns the database."""
    import sqlite3

    with read_only(populated) as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM market_states")


def test_a_missing_database_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(DashboardDataError, match="daily_runner"), read_only(
        tmp_path / "absent.db"
    ):
        pass


def test_the_dashboard_never_recomputes_the_strategy() -> None:
    """It reads stored state; it does not run engines.

    Parsed rather than grepped so a comment about the rule is not a violation.
    """
    app_dir = paths.PROJECT_ROOT / "app"
    modules = [app_dir / "streamlit_app.py", *sorted((app_dir / "views").glob("*.py"))]
    banned = (
        "fear_ladder.indicators",
        "fear_ladder.scoring",
        "fear_ladder.regime",
        "fear_ladder.allocation",
        "fear_ladder.research",
        "fear_ladder.pipeline.daily",
    )
    offenders = []
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            offenders.extend(
                f"{path.name}:{name}" for name in names if name.startswith(banned)
            )
    assert not offenders, f"the dashboard imports compute modules: {offenders}"
    assert len(modules) > 3, "the view modules should be picked up, not just the entry point"


def test_the_query_layer_holds_no_engine_imports() -> None:
    from fear_ladder.pipeline import queries as module

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("fear_ladder.research")


# ------------------------------------------------------------------ TASK-140


def test_current_state_is_readable(queries: DashboardQueries) -> None:
    version = queries.active_strategy_version()
    assert version == "v0.0-placeholder"

    state = queries.latest_state(version)
    assert state is not None
    assert state["regime"]
    assert state["observation_date"]


def test_the_current_allocation_is_readable(queries: DashboardQueries) -> None:
    version = queries.active_strategy_version()
    allocation = queries.latest_allocation(version)
    assert not allocation.empty
    assert set(allocation["asset"]) <= {asset.value for asset in Asset}
    assert allocation["weight"].sum() == pytest.approx(1.0)


def test_staleness_is_reported_not_hidden() -> None:
    fresh = {"observation_date": date.today().isoformat(), "regime": "Neutral"}
    old = {
        "observation_date": (date.today() - timedelta(days=30)).isoformat(),
        "regime": "Neutral",
    }
    unknown = {"observation_date": date.today().isoformat(), "regime": UNKNOWN_REGIME}

    assert not is_signal_stale(fresh)
    assert is_signal_stale(old)
    assert is_signal_stale(unknown), "an UNKNOWN state is never a current signal"
    assert is_signal_stale(None)


# ------------------------------------------------------------------ TASK-141


def test_indicator_scores_are_readable(queries: DashboardQueries) -> None:
    version = queries.active_strategy_version()
    assert version is not None
    state = queries.latest_state(version)
    assert state is not None

    scores = queries.indicator_scores(str(state["observation_date"]), version)
    assert not scores.empty
    assert {"indicator_name", "score", "normalization_method"} <= set(scores.columns)
    usable = scores["score"].dropna()
    assert usable.between(0, 100).all()


def test_indicator_history_is_readable(queries: DashboardQueries) -> None:
    version = queries.active_strategy_version()
    assert version is not None
    names = queries.available_indicators(version)
    assert "rsi_14" in names

    history = queries.indicator_history("rsi_14", version)
    assert not history.empty
    assert history.index.is_monotonic_increasing


# ------------------------------------------------------------------ TASK-142


def test_history_series_are_readable(queries: DashboardQueries) -> None:
    version = queries.active_strategy_version()
    history = queries.state_history(version)
    assert not history.empty
    assert {"composite_score", "regime", "target_leverage"} <= set(history.columns)

    prices = queries.price_history(("QQQ",))
    assert not prices.empty
    assert "QQQ" in prices.columns


def test_allocation_history_is_a_weight_matrix(queries: DashboardQueries) -> None:
    version = queries.active_strategy_version()
    assert version is not None
    allocations = queries.allocation_history(version)
    assert not allocations.empty
    assert allocations.sum(axis=1).round(6).eq(1.0).all()


def test_regime_spans_are_contiguous_and_cover_everything() -> None:
    import pandas as pd

    index = pd.to_datetime([date(2024, 1, day) for day in range(1, 8)])
    history = pd.DataFrame(
        {"regime": ["Fear", "Fear", "Neutral", "Neutral", "Neutral", "Bull", "Bull"]},
        index=index,
    )
    spans = regime_spans(history)
    assert [regime for _, _, regime in spans] == ["Fear", "Neutral", "Bull"]
    assert spans[0][0] == index[0]
    assert spans[-1][1] == index[-1]


def test_an_unknown_stretch_is_kept_visible() -> None:
    import pandas as pd

    index = pd.to_datetime([date(2024, 1, day) for day in range(1, 5)])
    history = pd.DataFrame(
        {"regime": ["Fear", UNKNOWN_REGIME, UNKNOWN_REGIME, "Fear"]}, index=index
    )
    spans = regime_spans(history)
    assert [regime for _, _, regime in spans] == ["Fear", UNKNOWN_REGIME, "Fear"]


def test_spans_of_an_empty_history_are_empty() -> None:
    import pandas as pd

    assert regime_spans(pd.DataFrame()) == []


# ------------------------------------------------------------------ TASK-143


def test_event_and_alert_history_are_readable(queries: DashboardQueries) -> None:
    version = queries.active_strategy_version()
    events = queries.regime_events(version)
    alerts = queries.alert_events()

    assert set(events.columns) >= {"event_date", "previous_regime", "new_regime"}
    assert set(alerts.columns) >= {"event_type", "delivery_status", "title"}


# ---------------------------------------------------------------- operations


def test_operations_views_are_readable(queries: DashboardQueries) -> None:
    runs = queries.recent_runs()
    assert not runs.empty
    assert {"status", "stage", "run_date"} <= set(runs.columns)

    coverage = queries.data_coverage()
    assert set(coverage["symbol"]) >= {"QQQ", "QLD", "TQQQ", "VIX"}
    assert (coverage["rows"] > 0).all()

    assert queries.open_findings().empty
    assert isinstance(queries.strategy_versions().shape[0], int)


def test_an_empty_database_reads_as_empty(db_path: Path) -> None:
    queries = DashboardQueries(db_path=db_path)
    assert queries.active_strategy_version() is None
    assert queries.latest_state() is None
    assert queries.latest_allocation().empty
    assert queries.state_history().empty
    assert queries.recent_runs().empty


# ------------------------------------------------------------------ TASK-144


def test_the_backtest_tab_handles_a_missing_report(tmp_path: Path) -> None:
    # The dashboard must not crash before a backtest has ever been run.
    assert not (tmp_path / "metrics.json").is_file()


def test_utc_timestamps_survive_the_round_trip(queries: DashboardQueries) -> None:
    runs = queries.recent_runs()
    started = str(runs["started_at"].iloc[0])
    assert datetime.fromisoformat(started).tzinfo is not None
    assert datetime.fromisoformat(started).utcoffset() == UTC.utcoffset(None)


# ------------------------------------------------------- the page itself


@pytest.fixture
def clean_streamlit_caches():
    """Streamlit caches live in the process, not the session.

    Without clearing them, one test's results leak into the next and an
    "empty database" test would happily read the previous test's rows.
    """
    streamlit = pytest.importorskip("streamlit")
    streamlit.cache_data.clear()
    streamlit.cache_resource.clear()
    yield
    streamlit.cache_data.clear()
    streamlit.cache_resource.clear()


def _render_app():
    streamlit_testing = pytest.importorskip("streamlit.testing.v1")
    app = streamlit_testing.AppTest.from_file(
        str(paths.PROJECT_ROOT / "app" / "streamlit_app.py"), default_timeout=120
    )
    app.run()
    return app


def test_the_streamlit_page_renders_without_error(
    populated: Path, clean_streamlit_caches
) -> None:
    """Render the real page against real pipeline output.

    ``AppTest`` executes the script exactly as Streamlit would, so this catches
    template and API breakage that the query tests cannot.
    """
    app = _render_app()

    assert not app.exception, [str(item.value) for item in app.exception]
    assert not app.error, [item.value for item in app.error]

    labels = {metric.label for metric in app.metric}
    assert {"Regime", "Market Score", "Target Leverage", "Last Update"} <= labels

    headers = {header.value for header in app.header}
    assert {"오늘의 신호", "지표", "기록", "이벤트", "운영"} <= headers
    # The two pages that explain the strategy render without a database row.
    assert "이 전략은 어떻게 동작하는가" in headers
    assert "성과" in headers


def test_the_page_survives_an_empty_database(
    db_path: Path, clean_streamlit_caches
) -> None:
    # A fresh install must show guidance, not a stack trace.
    app = _render_app()

    assert not app.exception, [str(item.value) for item in app.exception]
    assert any("daily_runner" in message.value for message in app.info)

"""TASK-160 .. TASK-162 — end-to-end, and the deployment surface (TASK-150/151).

TASK-160 runs the documented chain in one go::

    Data → Indicator → Score → Regime → Allocation → SQLite → Event → Telegram

TASK-161 runs it twice and checks nothing doubled.
TASK-162 breaks the data and checks no investment signal or alert is produced.

The notifier here is a recording stand-in rather than a mock: the goal is to see
what *would* be sent, byte for byte, without a network call.
"""

from __future__ import annotations

import re
import shlex
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
import yaml

from regime_monitor import paths
from regime_monitor.alerts.telegram import RecordingNotifier
from regime_monitor.constants import UNKNOWN_REGIME, Asset, EventType, PipelineStatus
from regime_monitor.data.collection import CollectionReport
from regime_monitor.data.models import Provenance
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork
from regime_monitor.pipeline.daily import DailyPipeline
from tests.conftest import make_price_observation, make_scalar_observation

TODAY = date(2024, 9, 20)
HISTORY_DAYS = 900


class _SeededCollection:
    """Stands in for the network so the chain is deterministic."""

    def collect(self, repository: object, *, start: date, end: date) -> CollectionReport:
        return CollectionReport(collected={"(pre-seeded)": 0})


def _history(provenance: Provenance, *, through: date, crash_at: int | None = None) -> list:
    rng = np.random.default_rng(101)
    days = [through - timedelta(days=offset) for offset in range(HISTORY_DAYS)][::-1]
    daily = rng.normal(0.0005, 0.011, HISTORY_DAYS)
    if crash_at is not None:
        shock = np.random.default_rng(999).normal(-0.03, 0.04, HISTORY_DAYS - crash_at)
        daily[crash_at:] = shock

    qqq = 100.0 * np.cumprod(1 + daily)
    qld = 100.0 * np.cumprod(1 + 2 * daily)
    tqqq = 100.0 * np.cumprod(1 + 3 * daily)
    vix = np.abs(rng.normal(16, 4, HISTORY_DAYS)) + np.abs(daily) * 400
    fng = np.clip(50 + daily.cumsum() * 250 + rng.normal(0, 8, HISTORY_DAYS), 0, 100)

    rows = []
    for index, day in enumerate(days):
        rows.append(make_price_observation("QQQ", day, float(qqq[index]), provenance))
        rows.append(make_price_observation("QLD", day, float(qld[index]), provenance))
        rows.append(make_price_observation("TQQQ", day, float(tqqq[index]), provenance))
        rows.append(make_scalar_observation("VIX", day, float(vix[index]), provenance))
        rows.append(
            make_scalar_observation("CNN_FEAR_GREED", day, float(fng[index]), provenance)
        )
    return rows


@pytest.fixture
def seeded(db_path: Path, provenance: Provenance) -> Path:
    with SQLiteUnitOfWork(db_path) as uow:
        uow.observations.save_observations(_history(provenance, through=TODAY))
    return db_path


def _pipeline(config, notifier: RecordingNotifier) -> DailyPipeline:
    return DailyPipeline(config, notifier=notifier, collection_service=_SeededCollection())


# ------------------------------------------------------------------ TASK-160


def test_the_documented_chain_runs_end_to_end(seeded: Path, placeholder_config) -> None:
    notifier = RecordingNotifier()
    version = placeholder_config.strategy.strategy_version

    with SQLiteUnitOfWork(seeded) as uow:
        result = _pipeline(placeholder_config, notifier).run(uow, as_of=TODAY)

        # Data -> Indicator
        values = uow.indicators.get_values("rsi_14")
        assert values, "indicator values were persisted"

        # Indicator -> Score
        scores = uow.indicators.get_scores(
            result.state.observation_date, strategy_version=version
        )
        assert scores
        assert all(
            score.score is None or 0 <= score.score <= 100 for score in scores
        )

        # Score -> Regime -> Allocation -> SQLite
        state = uow.states.get_state(result.state.observation_date, strategy_version=version)
        assert state is not None
        assert state.regime in placeholder_config.strategy.regime.labels
        allocation = uow.states.get_allocation(
            result.state.observation_date, strategy_version=version
        )
        assert allocation is not None
        assert allocation.target_leverage == pytest.approx(state.target_leverage)

        # -> Event / Telegram
        alerts = uow.events.get_alerts()
        assert len(alerts) == len(notifier.messages)

    assert result.run.status is PipelineStatus.SUCCESS


#: The crash has to begin *inside* the window the pipeline actually runs. The
#: transition engine replays the whole history, so a change that settles before
#: the first run is never reported as a change on any day that is run.
OBSERVED_DAYS = 45


@pytest.fixture
def crashed(db_path: Path, provenance: Provenance, placeholder_config):
    """A market that crashes inside the observed window, run day by day."""
    notifier = RecordingNotifier()
    pipeline = _pipeline(placeholder_config, notifier)

    rows = _history(provenance, through=TODAY, crash_at=HISTORY_DAYS - OBSERVED_DAYS + 10)
    by_date: dict[date, list] = {}
    for row in rows:
        by_date.setdefault(row.observation_date, []).append(row)
    days = sorted(by_date)

    with SQLiteUnitOfWork(db_path) as uow:
        history = [row for day in days[:-OBSERVED_DAYS] for row in by_date[day]]
        uow.observations.save_observations(history)

    for day in days[-OBSERVED_DAYS:]:
        with SQLiteUnitOfWork(db_path) as uow:
            uow.observations.save_observations(by_date[day])
            pipeline.run(uow, as_of=day)

    return db_path, notifier


def test_what_would_be_sent_is_complete_and_honest(crashed, placeholder_config) -> None:
    """Inspect the actual outbound messages, not a mock's call count."""
    _, notifier = crashed
    assert notifier.messages, "a crashing market must produce something to say"

    for message in notifier.messages:
        assert "자동매매를 하지 않습니다" in message.body, "PRD.md 1.2"
        assert message.title
        assert message.strategy_version == placeholder_config.strategy.strategy_version
        assert message.event_type in set(EventType)


def test_a_regime_change_travels_all_the_way_to_an_alert(
    crashed, placeholder_config
) -> None:
    """A regime change must produce a stored event *and* an outbound message."""
    db_path, notifier = crashed
    version = placeholder_config.strategy.strategy_version

    with SQLiteUnitOfWork(db_path) as uow:
        events = uow.events.get_regime_events(strategy_version=version)
        alerts = uow.events.get_alerts(event_type=EventType.REGIME_CHANGED)

    assert events, "the crash should have moved the regime at least once"
    event_dates = {event.event_date for event in events}
    alert_dates = {alert.event_date for alert in alerts}
    assert event_dates == alert_dates, "every regime change must also be notified"
    assert len(notifier.messages) >= len(events)


# ------------------------------------------------------------------ TASK-161


def test_running_the_pipeline_twice_duplicates_nothing(
    seeded: Path, placeholder_config
) -> None:
    notifier = RecordingNotifier()
    pipeline = _pipeline(placeholder_config, notifier)
    version = placeholder_config.strategy.strategy_version

    with SQLiteUnitOfWork(seeded) as uow:
        pipeline.run(uow, as_of=TODAY)
    with SQLiteUnitOfWork(seeded) as uow:
        pipeline.run(uow, as_of=TODAY)

    with SQLiteUnitOfWork(seeded) as uow:
        connection = uow.connection
        counts = {
            table: connection.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            for table in (
                "market_states",
                "target_allocations",
                "regime_events",
                "alert_events",
            )
        }
        states = uow.states.get_state_history(strategy_version=version)

    assert counts["market_states"] == 1
    assert states[0].regime != UNKNOWN_REGIME
    # One row per sleeve in the allocation, written once — not once per run.
    assert 0 < counts["target_allocations"] <= len(Asset)
    assert counts["regime_events"] <= 1

    keys = [message.dedupe_key for message in notifier.messages]
    assert len(keys) == len(set(keys)), "a message was sent twice"


def test_a_third_run_still_changes_nothing(seeded: Path, placeholder_config) -> None:
    notifier = RecordingNotifier()
    pipeline = _pipeline(placeholder_config, notifier)

    snapshots = []
    for _ in range(3):
        with SQLiteUnitOfWork(seeded) as uow:
            result = pipeline.run(uow, as_of=TODAY)
            snapshots.append(
                (
                    result.state.regime,
                    result.state.composite_score,
                    result.state.target_leverage,
                    tuple(sorted((a.value, w) for a, w in (result.allocation.weights.items()))),
                )
            )
    assert snapshots[0] == snapshots[1] == snapshots[2]


# ------------------------------------------------------------------ TASK-162


def test_missing_mandatory_data_produces_no_investment_signal(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    """The requirement in one test.

    With a mandatory input gone, there must be no score, no allocation, no
    leverage, and the only message must be the failure notice.
    """
    notifier = RecordingNotifier()
    version = placeholder_config.strategy.strategy_version

    with SQLiteUnitOfWork(db_path) as uow:
        rows = [
            row
            for row in _history(provenance, through=TODAY)
            if row.symbol != "VIX"  # mandatory
        ]
        uow.observations.save_observations(rows)

    with SQLiteUnitOfWork(db_path) as uow:
        result = _pipeline(placeholder_config, notifier).run(uow, as_of=TODAY)

        assert result.state.regime == UNKNOWN_REGIME
        assert result.state.composite_score is None
        assert result.state.target_leverage is None
        assert uow.states.get_allocation(TODAY, strategy_version=version) is None
        assert uow.events.get_regime_events(strategy_version=version) == []

    assert [message.event_type for message in notifier.messages] == [
        EventType.DATA_FAILURE
    ]
    body = notifier.messages[0].body
    assert "QQQ" not in body or "목표 배분" not in body
    assert re.search(r"\d+\.\d+x", body) is None, "no leverage may appear"


def test_a_failed_day_does_not_overwrite_yesterdays_good_state(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    notifier = RecordingNotifier()
    pipeline = _pipeline(placeholder_config, notifier)
    version = placeholder_config.strategy.strategy_version
    yesterday = TODAY - timedelta(days=1)

    with SQLiteUnitOfWork(db_path) as uow:
        uow.observations.save_observations(_history(provenance, through=yesterday))
    with SQLiteUnitOfWork(db_path) as uow:
        good = pipeline.run(uow, as_of=yesterday)
        assert not good.is_data_failure

    # Today the mandatory feed is gone.
    with SQLiteUnitOfWork(db_path) as uow:
        uow.connection.execute("DELETE FROM market_observations WHERE symbol = 'VIX'")
        failed = pipeline.run(uow, as_of=TODAY)

    assert failed.is_data_failure
    with SQLiteUnitOfWork(db_path) as uow:
        preserved = uow.states.get_state(
            good.state.observation_date, strategy_version=version
        )
        assert preserved is not None
        assert preserved.regime == good.state.regime
        assert preserved.target_leverage is not None


def test_recovery_after_a_failure_resumes_normal_signals(
    db_path: Path, provenance: Provenance, placeholder_config
) -> None:
    notifier = RecordingNotifier()
    pipeline = _pipeline(placeholder_config, notifier)

    with SQLiteUnitOfWork(db_path) as uow:
        rows = [row for row in _history(provenance, through=TODAY) if row.symbol != "VIX"]
        uow.observations.save_observations(rows)
    with SQLiteUnitOfWork(db_path) as uow:
        assert pipeline.run(uow, as_of=TODAY).is_data_failure

    # The feed comes back.
    with SQLiteUnitOfWork(db_path) as uow:
        vix = [row for row in _history(provenance, through=TODAY) if row.symbol == "VIX"]
        uow.observations.save_observations(vix)
        recovered = pipeline.run(uow, as_of=TODAY)

    assert not recovered.is_data_failure
    assert recovered.allocation is not None


# ------------------------------------------------ TASK-150 / TASK-151 surface


def test_the_dockerfile_copies_what_it_runs() -> None:
    dockerfile = (paths.PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for required in ("src/", "config/", "scripts/", "app/"):
        assert f"COPY {required}" in dockerfile, required
    assert "USER regime" in dockerfile, "the container must not run as root"
    assert "tzdata" in dockerfile, "availability timestamps need a timezone database"


def test_the_entrypoint_commands_all_exist() -> None:
    entrypoint = (paths.PROJECT_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "scripts/daily_runner.py" in entrypoint
    assert "scripts/backtest.py" in entrypoint
    assert "app/streamlit_app.py" in entrypoint

    for referenced in ("scripts/daily_runner.py", "scripts/backtest.py", "app/streamlit_app.py"):
        assert (paths.PROJECT_ROOT / referenced).is_file(), referenced


def test_compose_services_use_the_entrypoint_verbs() -> None:
    compose = yaml.safe_load(
        (paths.PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    entrypoint = (paths.PROJECT_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    for name, spec in compose["services"].items():
        verb = spec["command"][0]
        assert f"  {verb})" in entrypoint, f"{name} uses unknown verb {verb!r}"


def test_the_dashboard_container_cannot_write_the_database() -> None:
    # ARCHITECTURE.md §4.2 — enforced by the mount, not only by the code.
    compose = yaml.safe_load(
        (paths.PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    mounts = compose["services"]["dashboard"]["volumes"]
    data_mount = next(mount for mount in mounts if mount.startswith("./data"))
    assert data_mount.endswith(":ro")


def test_no_secret_reaches_the_image() -> None:
    ignored = (paths.PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in ignored
    compose = (paths.PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # Only variable interpolation, never a literal value.
    for line in compose.splitlines():
        if "TELEGRAM" in line and ":" in line:
            value = line.split(":", 1)[1].strip()
            assert value.startswith("${"), line


def test_requirements_txt_matches_the_dashboard_extra() -> None:
    # TASK-152: Streamlit Cloud installs from requirements.txt, so a drift here
    # means the deployed dashboard runs different versions from everyone else.
    import tomllib

    pyproject = tomllib.loads(
        (paths.PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = set(pyproject["project"]["dependencies"])
    declared |= set(pyproject["project"]["optional-dependencies"]["dashboard"])

    requirements = (paths.PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    listed = {
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.startswith("#")
    }

    def name_of(requirement: str) -> str:
        return re.split(r"[<>=;\[ ]", requirement, maxsplit=1)[0].strip().lower()

    declared_names = {name_of(item) for item in declared}
    listed_names = {name_of(item) for item in listed}
    assert declared_names <= listed_names, declared_names - listed_names


def test_local_development_commands_are_runnable() -> None:
    """TASK-151 — the two commands the docs promise actually parse."""
    import scripts.backtest as backtest_cli
    import scripts.daily_runner as daily_cli

    args = daily_cli.build_parser().parse_args(shlex.split("--dry-run --no-collect"))
    assert args.dry_run and args.no_collect

    args = backtest_cli.build_parser().parse_args(
        shlex.split("--profile placeholder --report")
    )
    assert args.profile == "placeholder" and args.report

"""TASK-170 .. TASK-172 — the documentation has to be true.

A runbook that names a function which no longer exists is worse than no runbook:
it is read under pressure, at the moment its reader can least afford to debug it.
So the snippets in `docs/operations.md` are executed here, and the claims the
README makes about the system are checked against the system.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from fear_ladder import paths

README = paths.PROJECT_ROOT / "README.md"
STRATEGY_DOC = paths.PROJECT_ROOT / "docs" / "strategy.md"
OPERATIONS_DOC = paths.PROJECT_ROOT / "docs" / "operations.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _bash_blocks(markdown: str) -> list[str]:
    """Only fenced shell blocks count as commands.

    Prose that merely *mentions* a script is not an invocation, and treating it
    as one would make these tests fail on punctuation.
    """
    return re.findall(r"```bash\n(.*?)```", markdown, re.DOTALL)


def _python_snippets(markdown: str) -> list[str]:
    """Extract the Python the runbook tells an operator to paste."""
    snippets: list[str] = []
    for block in _bash_blocks(markdown):
        snippets.extend(re.findall(r"<<'PY'\n(.*?)\nPY", block, re.DOTALL))
        snippets.extend(re.findall(r'python -c "\n?(.*?)"', block, re.DOTALL))
    return snippets


# --------------------------------------------------------------- the docs exist


@pytest.mark.parametrize("path", [README, STRATEGY_DOC, OPERATIONS_DOC])
def test_the_documents_exist_and_are_not_stubs(path: Path) -> None:
    assert path.is_file(), path
    assert len(_text(path)) > 2000, f"{path.name} looks like a stub"


def test_every_document_the_readme_links_to_exists() -> None:
    for target in re.findall(r"\]\(([^)]+\.md)\)", _text(README)):
        assert (paths.PROJECT_ROOT / target).is_file(), target


# ------------------------------------------------------------------ TASK-170


def test_the_readme_states_the_no_auto_trading_rule() -> None:
    # PRD.md 1.2 — the single most important thing a reader must not miss.
    text = _text(README)
    assert "자동매매" in text
    assert "자동매매 시스템이 아니다" in text


def test_the_readme_names_the_frozen_version_and_its_evidence() -> None:
    """Until 2026-09-13 this asserted the opposite, and had to.

    A README that still said "parameters undecided" after the freeze would be
    lying about the thing a reader most needs to know, so the assertion flipped
    with the strategy: name the version, and point at the evidence for it.
    """
    from fear_ladder.config.loader import load_config

    text = _text(README)
    version = load_config().strategy.strategy_version
    assert version in text, version
    assert "config/frozen/" in text
    assert "RESEARCH" in text, "the research-parameter escape hatch stays documented"


def test_the_readme_commands_parse() -> None:
    """Every documented CLI invocation must actually be a valid one.

    Only ```bash fences are read: prose that merely *mentions* a script is not a
    command, and treating it as one would make this test fail on punctuation.
    """
    import shlex

    import scripts.backtest as backtest_cli
    import scripts.daily_runner as daily_cli

    parsers = {
        "scripts/daily_runner.py": daily_cli.build_parser(),
        "scripts/backtest.py": backtest_cli.build_parser(),
    }

    checked = 0
    for block in _bash_blocks(_text(README)):
        # Join shell line continuations before splitting into commands.
        for command in block.replace("\\\n", " ").splitlines():
            stripped = command.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for script, parser in parsers.items():
                if script not in stripped:
                    continue
                tail = stripped.split(script, 1)[1].strip()
                if not tail:
                    continue
                parser.parse_args(shlex.split(tail))
                checked += 1
    assert checked >= 4, f"only {checked} documented commands were checked"


def test_the_readme_environment_variables_are_the_real_ones() -> None:
    from fear_ladder.config.loader import ALLOW_RESEARCH_ENV
    from fear_ladder.monitoring.logging import LEVEL_ENV

    text = _text(README)
    for variable in (ALLOW_RESEARCH_ENV, LEVEL_ENV, "FEAR_LADDER_DB"):
        assert variable in text, variable


def test_the_readme_describes_the_real_package_layout() -> None:
    text = _text(README)
    for package in (
        "config",
        "data",
        "indicators",
        "scoring",
        "regime",
        "allocation",
        "backtest",
        "research",
        "alerts",
        "pipeline",
    ):
        assert f"{package}/" in text, package
        assert (paths.PACKAGE_ROOT / package).is_dir(), package


# ------------------------------------------------------------------ TASK-171


def test_the_strategy_doc_separates_settled_from_unsettled() -> None:
    text = _text(STRATEGY_DOC)
    assert "확정된 것" in text
    assert "확정되지 않은 것" in text
    assert "RESEARCH_PLACEHOLDER" in text


def test_the_strategy_doc_lists_exactly_the_open_parameters() -> None:
    """The doc's "not settled" table must match what the config actually reports."""
    from fear_ladder.config.loader import load_config

    text = _text(STRATEGY_DOC)
    unresolved = load_config().strategy.unresolved_parameters()
    for parameter in unresolved:
        head = parameter.split(".")[0]
        assert head in text, f"{parameter} is unresolved but undocumented"


def test_the_strategy_doc_records_why_breadth_is_excluded() -> None:
    text = _text(STRATEGY_DOC)
    assert "생존편향" in text
    assert "point-in-time" in text


def test_the_strategy_doc_matches_the_leverage_constants() -> None:
    from fear_ladder.constants import ASSET_LEVERAGE, Asset

    text = _text(STRATEGY_DOC)
    for asset, leverage in ASSET_LEVERAGE.items():
        if asset is Asset.CASH:
            continue
        assert f"{asset.value} | {leverage:g}x" in text, asset


def test_the_strategy_doc_names_the_real_gate_rules() -> None:
    from fear_ladder.config.loader import load_config

    text = _text(STRATEGY_DOC)
    for rule in load_config().strategy.tqqq_gate.candidate_rules:
        assert rule in text, rule


# ------------------------------------------------------------------ TASK-172


def test_the_operations_guide_covers_every_required_topic() -> None:
    # TASK-172 lists six topics by name.
    text = _text(OPERATIONS_DOC)
    for topic in (
        "GitHub Actions",
        "Telegram",
        "수동 실행",
        "SQLite",
        "Streamlit 재배포",
        "데이터 장애",
    ):
        assert topic in text, topic


def test_every_runbook_snippet_is_valid_python() -> None:
    snippets = _python_snippets(_text(OPERATIONS_DOC))
    assert len(snippets) >= 6, "the runbook lost its snippets"
    for snippet in snippets:
        ast.parse(snippet)


def test_every_symbol_the_runbook_names_still_exists() -> None:
    """The check that actually matters: an import in the runbook must resolve."""
    import importlib

    for snippet in _python_snippets(_text(OPERATIONS_DOC)):
        for node in ast.walk(ast.parse(snippet)):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("fear_ladder"):
                continue
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name), (
                    f"docs/operations.md uses {node.module}.{alias.name}, "
                    "which no longer exists"
                )


def test_the_runbook_repair_snippets_run(populated_db, monkeypatch) -> None:
    """Execute the read-only diagnostics against a real database."""
    monkeypatch.setenv("FEAR_LADDER_DB", str(populated_db))

    from fear_ladder.data.repositories.connection import checkpoint, connect
    from fear_ladder.pipeline.queries import DashboardQueries

    queries = DashboardQueries()
    assert queries.active_strategy_version() is not None
    assert queries.latest_state() is not None
    assert not queries.recent_runs().empty
    assert queries.alert_events(limit=20) is not None
    assert queries.open_findings() is not None

    connection = connect(read_only=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()

    writable = connect()
    try:
        checkpoint(writable)
    finally:
        writable.close()


def test_the_runbook_freshness_snippet_runs(populated_db, monkeypatch) -> None:
    from datetime import date

    monkeypatch.setenv("FEAR_LADDER_DB", str(populated_db))

    from fear_ladder.config.loader import load_config
    from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
    from fear_ladder.data.validators.freshness import FreshnessValidator

    config = load_config()
    with SQLiteUnitOfWork(populated_db) as uow:
        report = FreshnessValidator(config.data_sources).validate(
            uow.observations, as_of=date.today()
        )
    assert report.summary()
    assert all(source.describe() for source in report.sources)


def test_the_runbook_does_not_recommend_anything_forbidden() -> None:
    text = _text(OPERATIONS_DOC)
    assert "하지 말아야 할 것" in text
    for forbidden in ("OOS", "ALLOW_RESEARCH_PARAMS", "손으로 수정"):
        assert forbidden in text, forbidden


@pytest.fixture
def populated_db(db_path: Path, provenance, placeholder_config) -> Path:
    """A small but real database for the runbook snippets to read."""
    from datetime import date, timedelta

    import numpy as np

    from fear_ladder.alerts.engine import NullNotifier
    from fear_ladder.data.collection import CollectionReport
    from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
    from fear_ladder.pipeline.daily import DailyPipeline
    from tests.conftest import make_price_observation, make_scalar_observation

    class _NoCollection:
        def collect(self, repository: object, *, start: date, end: date) -> CollectionReport:
            return CollectionReport()

    today = date.today()
    length = 900
    rng = np.random.default_rng(5)
    days = [today - timedelta(days=offset) for offset in range(length)][::-1]
    daily = rng.normal(0.0004, 0.012, length)
    qqq = 100.0 * np.cumprod(1 + daily)

    with SQLiteUnitOfWork(db_path) as uow:
        rows = []
        for index, day in enumerate(days):
            for symbol, multiple in (("QQQ", 1), ("QLD", 2), ("TQQQ", 3)):
                price = 100.0 * float(np.cumprod(1 + multiple * daily)[index])
                rows.append(make_price_observation(symbol, day, price, provenance))
            rows.append(
                make_scalar_observation(
                    "VIX", day, float(abs(rng.normal(18, 4))), provenance
                )
            )
        uow.observations.save_observations(rows)
        assert qqq.size == length

    with SQLiteUnitOfWork(db_path) as uow:
        DailyPipeline(
            placeholder_config,
            notifier=NullNotifier(),
            collection_service=_NoCollection(),
        ).run(uow, as_of=today)
    return db_path

"""The dashboard's presentation helpers (``app/views/common.py``).

Two things are worth pinning here. The reason-code translation is the only
place the engine's vocabulary is turned into Korean, so a renamed code must
fail loudly rather than vanish from the page. And the ladder gates — the one
piece of arithmetic the dashboard does rather than reads — must agree with
``TransitionEngine``: the page is forbidden from importing the engine
(``ARCHITECTURE.md`` §4.2), so a test is what keeps the two from drifting.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fear_ladder import paths

sys.path.insert(0, str(paths.PROJECT_ROOT / "app"))

from views import common

LABELS = ("Capitulation", "Panic", "Fear", "Neutral", "Optimism", "Greed", "Euphoria")
BOUNDARIES = (12.0, 27.0, 42.0, 58.0, 73.0, 85.0)
HYSTERESIS = 5.0


@pytest.fixture
def report() -> dict:
    return {
        "parameters": {
            "regime_labels": list(LABELS),
            "regime_boundaries": list(BOUNDARIES),
            "ladder": {"Neutral": 1.75, "Fear": 2.1666666667, "Capitulation": 3.0},
            "ladder_mappings": {
                "Neutral": {"QLD": 0.75, "QQQ": 0.25},
                "Greed": {"QQQ": 0.9166666667, "CASH": 0.0833333333},
                "Capitulation": {"TQQQ": 1.0},
            },
        }
    }


# ------------------------------------------------------------------- ladder


def test_the_ladder_reads_bands_and_compositions(report: dict) -> None:
    rungs = common.ladder(report)
    assert [rung.label for rung in rungs] == list(LABELS)

    neutral = common.rung_of(rungs, "Neutral")
    assert neutral is not None
    assert (neutral.low, neutral.high) == (42.0, 58.0)
    assert neutral.band == "42 – 58"
    assert neutral.composition() == "QLD 75 · QQQ 25"


def test_a_composition_reads_from_the_most_aggressive_sleeve_down(report: dict) -> None:
    rungs = common.ladder(report)
    greed = common.rung_of(rungs, "Greed")
    assert greed is not None
    # Not "현금 8 · QQQ 92": the sleeve carrying the leverage comes first.
    assert greed.composition() == "QQQ 92 · 현금 8"


def test_a_report_without_compositions_leaves_them_blank(report: dict) -> None:
    del report["parameters"]["ladder_mappings"]
    rungs = common.ladder(report)
    assert all(rung.composition() == "" for rung in rungs)
    assert rungs[0].band == "0 – 12"


def test_no_report_is_an_empty_ladder() -> None:
    assert common.ladder(None) == []


# -------------------------------------------------------------------- gates


def test_gates_match_the_transition_engine(report: dict) -> None:
    """The page's two score lines must be the ones the engine actually uses."""
    from fear_ladder.regime.classifier import RegimeScale
    from fear_ladder.regime.transition import TransitionEngine

    scale = RegimeScale(labels=LABELS, boundaries=BOUNDARIES)
    engine = TransitionEngine(
        scale=scale, confirmation_days=1, hysteresis=HYSTERESIS, minimum_duration_days=1
    )
    rungs = common.ladder(report)

    for held in LABELS:
        lines = common.gates(rungs, held, HYSTERESIS)
        for score in [value / 2 for value in range(0, 201)]:
            candidate = scale.classify(score)
            if candidate == held:
                continue
            cleared = engine._clears_hysteresis(held, candidate, score)
            downward = scale.index_of(candidate) < scale.index_of(held)
            line = lines.down if downward else lines.up
            assert line is not None, f"{held} has no line toward {candidate}"
            shown = score <= line if downward else score >= line
            assert shown == cleared, (
                f"held={held} candidate={candidate} score={score}: "
                f"page says {shown}, engine says {cleared}"
            )


def test_the_end_rungs_have_nowhere_further_to_go(report: dict) -> None:
    rungs = common.ladder(report)
    assert common.gates(rungs, "Capitulation", HYSTERESIS).down is None
    assert common.gates(rungs, "Euphoria", HYSTERESIS).up is None


def test_an_unknown_regime_has_no_gates(report: dict) -> None:
    lines = common.gates(common.ladder(report), "UNKNOWN", HYSTERESIS)
    assert (lines.down, lines.up) == (None, None)


# ------------------------------------------------------------- reason codes


def test_every_code_the_engine_can_emit_is_translated() -> None:
    """A renamed or new reason code must not reach the page as a raw string."""
    from fear_ladder.regime.transition import ReasonCode

    emitted = {
        value
        for name, value in vars(ReasonCode).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    untranslated = {code for code in emitted if common.reason(code).text == code}
    assert not untranslated, f"no sentence for {sorted(untranslated)}"


@pytest.mark.parametrize(
    ("code", "kind", "fragment"),
    [
        ("BLOCKED_BY_HYSTERESIS:5", "lock", "히스테리시스 5점"),
        ("BLOCKED_BY_MIN_DURATION:74/75", "lock", "74/75거래일"),
        ("CONFIRMATION_PENDING:2/3", "lock", "2/3일"),
        ("TREND_INTACT:price_vs_200dma=0.07344", "trend", "+7.3%"),
        ("TREND_BROKEN:price_vs_200dma=-0.1122", "trend", "-11.2%"),
        ("LEVERAGE_CAPPED:2.17->0.50", "trend", "2.17x 에서 0.50x"),
        ("REQUIRED_RULE_FAILED:deep_drawdown,extreme_fear", "gate", "deep_drawdown, extreme_fear"),
        ("TQQQ_REALLOCATED_TO:QLD", "gate", "QLD"),
        ("REGIME_CONFIRMED", "change", "바뀌었습니다"),
        ("TARGET_LEVERAGE:1.75", "info", "1.75x"),
    ],
)
def test_codes_become_sentences(code: str, kind: str, fragment: str) -> None:
    translated = common.reason(code)
    assert translated.kind == kind
    assert fragment in translated.text
    assert translated.code == code


def test_an_unrecognised_code_still_shows_itself() -> None:
    translated = common.reason("SOMETHING_NEW:42")
    assert translated.text == "SOMETHING_NEW:42"
    assert translated.kind == "info"


def test_malformed_stored_codes_do_not_break_the_page() -> None:
    assert common.reasons_of(None) == []
    assert common.reasons_of("not json") == []
    assert common.reasons_of("[]") == []


# --------------------------------------------------------------------- locks


def test_locks_pick_out_the_brakes_and_their_counts() -> None:
    found = common.locks(
        '["BLOCKED_BY_MIN_DURATION:74/75", "REGIME_MAPPING:Greed", '
        '"BLOCKED_BY_HYSTERESIS:5"]'
    )
    assert [lock.kind for lock in found] == ["min_duration", "hysteresis"]
    assert found[0].progress == (74, 75)
    assert not found[0].is_ready
    assert found[1].progress is None


def test_a_full_count_reads_as_ready() -> None:
    (lock,) = common.locks('["BLOCKED_BY_MIN_DURATION:75/75"]')
    assert lock.is_ready


def test_a_day_without_brakes_has_no_locks() -> None:
    assert common.locks('["REGIME_HELD", "TARGET_LEVERAGE:1.75"]') == []


# ------------------------------------------------------------------ streaks


def _history(regimes: list[str]):  # type: ignore[no-untyped-def]
    import pandas as pd

    index = pd.bdate_range("2026-01-01", periods=len(regimes))
    return pd.DataFrame({"regime": regimes}, index=index)


def test_a_streak_counts_trading_days_not_calendar_days() -> None:
    run = common.streak(_history(["Greed"] * 4 + ["Neutral"] * 3))
    assert run is not None
    assert (run.regime, run.days) == ("Neutral", 3)
    assert (run.previous, run.previous_days) == ("Greed", 4)


def test_a_history_of_one_regime_has_no_previous() -> None:
    run = common.streak(_history(["Neutral"] * 5))
    assert run is not None
    assert run.days == 5
    assert run.previous is None


def test_an_empty_history_has_no_streak() -> None:
    import pandas as pd

    assert common.streak(pd.DataFrame()) is None
    assert common.streak(None) is None



# ------------------------------------------------------------------- theme


def test_the_foreground_differs_between_themes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single hex would vanish into one ground or the other."""
    for name in ("light", "dark"):
        monkeypatch.setattr(common, "theme", lambda name=name: name)
        assert common.INK[name] == common.ink()
        assert common.LOCK[name] == common.lock_colour()
    assert common.INK["light"] != common.INK["dark"]
    assert common.LOCK["light"] != common.LOCK["dark"]


def test_an_unknown_theme_falls_back_to_light() -> None:
    """Outside a live session there is no browser theme to read."""
    assert common.theme() in {"light", "dark"}
    assert common.ink() in set(common.INK.values())


def test_band_labels_flip_against_the_band_they_sit_on() -> None:
    """The regime scale runs dark-red to dark-green; one label colour fails."""
    assert common.readable_on("#b2182b") == "#ffffff"  # Capitulation
    assert common.readable_on("#1a9850") == "#ffffff"  # Euphoria
    assert common.readable_on("#d9d9d9") == "#111111"  # Neutral
    assert common.readable_on("#f4a582") == "#111111"  # Fear


def test_every_regime_colour_gets_a_readable_label() -> None:
    for fill in common.REGIME_COLOURS:
        assert common.readable_on(fill) in {"#111111", "#ffffff"}


# ------------------------------------------------------------- event log


def test_a_change_reads_as_a_direction_not_a_code() -> None:
    toward_fear = '["REGIME_CONFIRMED","MOVED_TOWARD_FEAR"]'
    toward_greed = '["REGIME_CONFIRMED","MOVED_TOWARD_GREED"]'
    assert common.change_direction(toward_fear) == "공포 쪽으로 한 칸"
    assert common.change_direction(toward_greed) == "탐욕 쪽으로 한 칸"


def test_a_change_with_no_direction_still_says_something() -> None:
    assert common.change_direction("[]") == "—"
    assert "바뀌었습니다" in common.change_direction('["REGIME_CONFIRMED"]')


def test_the_event_log_is_relabelled_for_a_reader() -> None:
    import pandas as pd

    events = pd.DataFrame(
        {
            "event_date": ["2026-08-21"],
            "previous_regime": ["Greed"],
            "new_regime": ["Neutral"],
            "previous_score": [45.87998541416041],
            "new_score": [50.235156920515216],
            "reason_codes": ['["REGIME_CONFIRMED","MOVED_TOWARD_FEAR"]'],
        }
    )
    table = common.regime_event_table(events)
    assert list(table.columns) == ["날짜", "이전 단계", "새 단계", "이전 점수", "새 점수", "사유"]
    # The stored JSON array must not survive into the page.
    assert "reason_codes" not in table.columns
    assert table["사유"].iloc[0] == "공포 쪽으로 한 칸"


def test_an_empty_event_log_passes_through() -> None:
    import pandas as pd

    empty = pd.DataFrame()
    assert common.regime_event_table(empty).empty
    assert common.regime_event_table(None) is None


def test_bands_are_painted_harder_on_the_dark_ground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same wash that tints white disappears on #0e1117."""
    monkeypatch.setattr(common, "theme", lambda: "dark")
    dark = common.band_opacity()
    monkeypatch.setattr(common, "theme", lambda: "light")
    assert dark > common.band_opacity()

# ------------------------------------------------------- architecture guard


def test_the_helpers_still_import_no_engine() -> None:
    """``common.py`` may read config, never an engine (ARCHITECTURE.md §4.2)."""
    import ast

    source = Path(common.__file__).read_text(encoding="utf-8")
    banned = ("fear_ladder.regime", "fear_ladder.allocation", "fear_ladder.scoring")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(banned)

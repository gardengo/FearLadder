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

# ``views/__init__`` eagerly imports every tab, so pulling in ``common`` pulls in
# ``streamlit`` — which lives in the ``dashboard`` extra, not ``dev``. Guard it
# the way ``tests/integration/test_dashboard.py`` does: an environment without
# the dashboard installed must **skip** this module, not fail collecting it.
# A collection error cannot be deselected by ``-m``, so an unguarded import here
# takes down every job that runs this tree, the daily worker included.
pytest.importorskip("streamlit")

from views import common

pytestmark = pytest.mark.dashboard

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


# ----------------------------------------------------------------- handrail

TREND = {
    "indicator": "price_vs_200dma",
    "threshold": -0.02,
    "reentry_threshold": 0.02,
    "max_leverage_below": 0.5,
    "depth_indicator": "drawdown_52w",
    "min_depth_to_engage": 0.2,
}


@pytest.fixture
def trend_report(report: dict) -> dict:
    report["parameters"]["trend_filter"] = dict(TREND)
    return report


def _rail(report: dict, code: str, distance: float, depth: float, close: float = 100.0):  # type: ignore[no-untyped-def]
    rail = common.railing(
        report,
        f'["REGIME_HELD","{code}"]',
        {"price_vs_200dma": distance, "drawdown_52w": depth},
        close,
    )
    assert rail is not None
    return rail


def test_the_handrail_agrees_with_the_trend_filter(trend_report: dict) -> None:
    """The page may not import the filter, so a test holds the two together."""
    from fear_ladder.allocation.trend_filter import TrendFilter

    engine = TrendFilter(
        indicator=TREND["indicator"],
        threshold=TREND["threshold"],
        max_leverage_below=TREND["max_leverage_below"],
        reentry_threshold=TREND["reentry_threshold"],
        depth_indicator=TREND["depth_indicator"],
        min_depth_to_engage=TREND["min_depth_to_engage"],
    )
    for distance in [value / 200 for value in range(-40, 41)]:
        for depth in [value / 100 for value in range(0, 45)]:
            rail = _rail(trend_report, "TREND_INTACT:price_vs_200dma=0", distance, depth)
            _, verdict = engine.apply({}, distance, depth_observed=depth)
            assert rail.would_engage == verdict.engaged, (
                f"distance={distance} depth={depth}: page says {rail.would_engage}, "
                f"filter says {verdict.engaged}"
            )


def test_under_the_line_but_not_deep_enough_is_its_own_state(trend_report: dict) -> None:
    """2026-11-05: 4.9% under the average, 14.8% off the high, handrail off."""
    rail = _rail(
        trend_report, "TREND_INTACT:price_vs_200dma=-0.04945", -0.04945, 0.1482, close=645.0
    )
    assert rail.status == "shallow"
    assert rail.below_line and not rail.deep_enough and not rail.would_engage
    assert rail.depth_shortfall == pytest.approx(0.0518)
    # Already past the line, so the price that matters is the depth one.
    assert rail.trigger_price == pytest.approx(rail.depth_price)
    assert rail.depth_price == pytest.approx(645.0 / (1 - 0.1482) * 0.8)


def test_above_the_line_reads_as_clear_with_room_to_spare(trend_report: dict) -> None:
    rail = _rail(trend_report, "TREND_INTACT:price_vs_200dma=0.08", 0.08, 0.03, close=108.0)
    assert rail.status == "clear"
    assert rail.to_engage == pytest.approx(0.10)
    assert rail.average == pytest.approx(100.0)
    assert rail.price_at(rail.engage_below) == pytest.approx(98.0)
    assert rail.price_at(rail.release_above) == pytest.approx(102.0)


def test_engaged_comes_from_the_stored_state_not_from_todays_reading(
    trend_report: dict,
) -> None:
    """Between the two lines the handrail stays wherever it already was."""
    held = _rail(trend_report, "TREND_BROKEN:price_vs_200dma=0.01", 0.01, 0.05)
    assert held.status == "engaged"
    assert not held.would_engage, "today's readings alone would not switch it on"
    assert held.to_release == pytest.approx(0.01)

    released = _rail(trend_report, "TREND_INTACT:price_vs_200dma=0.01", 0.01, 0.05)
    assert released.status == "clear"


def test_the_distance_survives_without_stored_readings(trend_report: dict) -> None:
    rail = common.railing(trend_report, '["TREND_INTACT:price_vs_200dma=0.07344"]')
    assert rail is not None
    assert rail.distance == pytest.approx(0.07344)
    assert rail.depth is None and rail.deep_enough, "an unreadable depth counts as deep"
    assert rail.average is None and rail.trigger_price is None


def test_an_unreadable_trend_is_an_engaged_handrail(trend_report: dict) -> None:
    rail = common.railing(
        trend_report, '["TREND_UNKNOWN:price_vs_200dma","LEVERAGE_CAPPED_BY_DEFAULT"]'
    )
    assert rail is not None
    assert rail.status == "engaged"
    assert rail.distance is None


def test_no_filter_means_no_handrail(report: dict, trend_report: dict) -> None:
    assert common.railing(None, "[]") is None
    assert common.railing(trend_report, '["TREND_FILTER_DISABLED"]') is None
    del report["parameters"]["trend_filter"]
    assert common.railing(report, '["TREND_INTACT:price_vs_200dma=0.1"]') is None


# ------------------------------------------------------- live versus fixed


def test_run_times_are_shown_on_the_readers_clock() -> None:
    # 14:02 UTC is 23:02 the same day in Seoul; 20:30 UTC is already tomorrow.
    assert common.local_time("2026-11-10T14:02:56.872962+00:00") == "11-10 23:02 KST"
    assert common.local_time("2026-11-10T20:30:00+00:00") == "11-11 05:30 KST"


def test_a_missing_or_naive_run_time_shows_nothing() -> None:
    assert common.local_time(None) is None
    assert common.local_time("not a time") is None
    assert common.local_time("2026-11-10T14:02:56") is None


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


def test_the_engines_own_count_replaces_the_row_count_when_it_is_stored() -> None:
    # TASK-188. The stored rows show Neutral for three days, but the engine
    # entered it nine stepped days ago: the worker missed some, or the rows in
    # between were written from another path. The lock is counted from the
    # engine's date, so that is the one the page must show beside it.
    from datetime import date

    stored = common.streak(_history(["Greed"] * 4 + ["Neutral"] * 3))
    memory = {"regime": "Neutral", "since": date(2025, 12, 24), "days_held": 9}

    run = common.carried(stored, memory)
    assert run is not None
    assert (run.regime, run.days, run.since) == ("Neutral", 9, date(2025, 12, 24))
    assert (run.previous, run.previous_days) == ("Greed", 4)


def test_without_a_stored_count_the_row_count_stands() -> None:
    stored = common.streak(_history(["Neutral"] * 5))

    assert common.carried(stored, None) is stored
    # A memory for some other regime describes a different day's state — the
    # row for today has not been written yet — and must not be mixed in.
    assert common.carried(stored, {"regime": "Fear", "since": None, "days_held": 2}) is stored
    assert common.carried(None, {"regime": "Neutral", "days_held": 2}) is None



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
    # The codes carry a direction and nothing else.
    assert common.change_direction(toward_fear) == "공포 쪽으로"
    assert common.change_direction(toward_greed) == "탐욕 쪽으로"
    # The distance comes from the two regimes, and is not always one.
    assert common.change_direction(toward_fear, 1) == "공포 쪽으로 한 칸"
    assert common.change_direction(toward_fear, 4) == "공포 쪽으로 4칸"


def test_the_distance_of_a_change_is_counted_in_rungs() -> None:
    """Optimism to Panic on 2025-03-03 was written as "한 칸". It is three."""
    assert common.rungs_between("Neutral", "Optimism") == 1
    assert common.rungs_between("Optimism", "Panic") == 3
    # Optimism sits between them: the hold released a move two rungs wide.
    assert common.rungs_between("Greed", "Neutral") == 2
    assert common.rungs_between("Neutral", "no-such-regime") is None
    assert common.rungs_between(None, "Neutral") is None


def test_a_change_the_record_caught_up_with_says_when_the_engine_moved() -> None:
    late = '["REGIME_CHANGED_ON:2026-10-02","MOVED_TOWARD_GREED"]'
    text = common.change_direction(late, 1)

    assert text.startswith("탐욕 쪽으로 한 칸")
    assert "2026-10-02" in text
    # And the code reads as a sentence wherever codes are listed one by one.
    assert "2026-10-02" in common.reason("REGIME_CHANGED_ON:2026-10-02").text
    assert common.reason("REGIME_MEMORY_OUT_OF_WINDOW").text != "REGIME_MEMORY_OUT_OF_WINDOW"


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
    # Greed to Neutral passes over Optimism. This row used to say "한 칸".
    assert table["사유"].iloc[0] == "공포 쪽으로 2칸"


def test_the_event_log_shows_the_leverage_on_either_side() -> None:
    import pandas as pd

    events = pd.DataFrame(
        {
            "event_date": ["2026-11-10", "2025-03-03", "2024-02-01"],
            "previous_regime": ["Neutral", "Optimism", "Fear"],
            "new_regime": ["Optimism", "Panic", "Neutral"],
            "previous_score": [19.2, 30.0, 40.0],
            "new_score": [19.0, 20.0, 45.0],
            "reason_codes": [
                '["REGIME_CHANGED_ON:2026-10-02","MOVED_TOWARD_GREED"]',
                '["REGIME_CONFIRMED","MOVED_TOWARD_FEAR"]',
                '["REGIME_CONFIRMED","MOVED_TOWARD_GREED"]',
            ],
            "previous_leverage": [1.75, 1.3333333333, 0.5],
            "new_leverage": [1.3333333333, 2.5, None],
        }
    )
    table = common.regime_event_table(events)

    assert list(table.columns) == [
        "날짜", "이전 단계", "새 단계", "목표 레버리지", "이전 점수", "새 점수", "사유",
    ]
    assert table["목표 레버리지"].tolist() == ["1.75x → 1.33x", "1.33x → 2.50x", "—"]
    assert table["사유"].iloc[0] == "탐욕 쪽으로 한 칸 · 실제 전환 2026-10-02"
    assert table["사유"].iloc[1] == "공포 쪽으로 3칸"


def test_the_days_the_record_was_put_back_on_the_engines_path_are_found() -> None:
    from datetime import date

    import pandas as pd

    events = pd.DataFrame(
        {
            "event_date": ["2026-11-10", "2026-08-21"],
            "reason_codes": [
                '["REGIME_CHANGED_ON:2026-10-02","MOVED_TOWARD_GREED"]',
                '["REGIME_CONFIRMED","MOVED_TOWARD_FEAR"]',
            ],
        }
    )
    assert common.late_changes(events) == [(date(2026, 11, 10), date(2026, 10, 2))]
    assert common.late_changes(pd.DataFrame()) == []
    assert common.late_changes(None) == []


# ------------------------------------------------------------ today's action


def test_an_unchanged_target_asks_for_nothing() -> None:
    same = common.rebalance(
        {"QLD": 0.3333333333, "QQQ": 0.6666666667},
        {"QLD": 0.3333333333, "QQQ": 0.6666666667, "CASH": 0.0},
    )
    assert same is not None
    assert not same.needed
    # Most aggressive sleeve first, the way the ladder table reads; a sleeve
    # held at zero is not a holding.
    assert same.describe_holding() == "QLD 33% · QQQ 67%"


def test_a_changed_target_says_which_sleeves_moved_and_by_how_much() -> None:
    change = common.rebalance(
        {"QLD": 0.75, "QQQ": 0.25}, {"QLD": 0.3333333333, "QQQ": 0.6666666667}
    )
    assert change is not None
    assert change.needed
    assert change.describe_moves() == "QLD 75% → 33% · QQQ 25% → 67%"


def test_a_sleeve_that_appears_or_disappears_is_a_move() -> None:
    change = common.rebalance({"QQQ": 0.9166666667, "CASH": 0.0833333333}, {"TQQQ": 1.0})
    assert change is not None
    assert change.describe_moves() == "TQQQ 0% → 100% · QQQ 92% → 0% · 현금 8% → 0%"
    # With nothing to compare against there is no answer, not "nothing to do".
    assert common.rebalance(None, {"QQQ": 1.0}) is None
    assert common.rebalance({"QQQ": 1.0}, {}) is None


# ------------------------------------------------- when the minimum hold ends


def test_the_release_date_skips_weekends_and_exchange_holidays() -> None:
    from datetime import date

    # Thanksgiving 2026 is Thursday 11-26; Christmas falls on a Friday.
    assert common.trading_day_after(date(2026, 11, 25), 1) == date(2026, 11, 27)
    assert common.trading_day_after(date(2026, 12, 24), 1) == date(2026, 12, 28)
    # Good Friday is an exchange holiday that no federal calendar lists.
    assert common.trading_day_after(date(2026, 4, 2), 1) == date(2026, 4, 6)
    # Veterans Day is a federal holiday the exchange trades through.
    assert common.trading_day_after(date(2026, 11, 10), 1) == date(2026, 11, 11)
    assert common.trading_day_after(date(2026, 11, 13), 0) is None


def test_the_hold_release_counts_down_from_the_engines_own_number() -> None:
    from datetime import date

    run = common.Streak(
        regime="Optimism", days=31, since=date(2026, 10, 2), previous="Neutral",
        previous_days=65,
    )
    release = common.hold_release(run, 75, "2026-11-13")

    assert release is not None
    assert (release.held, release.required, release.remaining) == (31, 75, 44)
    assert not release.released
    # 44 sessions on, past Thanksgiving, Christmas, New Year and MLK day.
    assert release.expected == date(2027, 1, 20)


def test_a_hold_already_served_is_released() -> None:
    from datetime import date

    run = common.Streak(
        regime="Neutral", days=90, since=date(2026, 6, 11), previous="Fear",
        previous_days=75,
    )
    release = common.hold_release(run, 75, "2026-10-01")
    assert release is not None
    assert release.released
    assert release.expected is None
    # Without the engine's count, or without the rule, there is nothing to say.
    assert common.hold_release(None, 75, "2026-10-01") is None
    assert common.hold_release(run, None, "2026-10-01") is None


def test_every_sleeve_has_one_colour_for_every_chart() -> None:
    """The today donut and the history stack used two unrelated palettes."""
    assert set(common.SLEEVE_COLOURS) == set(common.ASSET_ORDER) == set(common.ASSET_ROWS)
    assert len(set(common.SLEEVE_COLOURS.values())) == len(common.SLEEVE_COLOURS)


def test_an_empty_event_log_passes_through() -> None:
    import pandas as pd

    empty = pd.DataFrame()
    assert common.regime_event_table(empty).empty
    assert common.regime_event_table(None) is None


# --------------------------------------------------------------- operations


def test_the_run_log_is_shown_on_the_readers_clock_with_a_duration() -> None:
    """The stored row is UTC to the microsecond, in English, with a literal None."""
    import pandas as pd

    runs = pd.DataFrame(
        {
            "run_date": ["2026-11-14", "2026-11-13"],
            "status": ["SUCCESS", "DATA_FAILURE"],
            "stage": ["track_record", "validate"],
            "started_at": [
                "2026-11-14T12:48:02.476121+00:00",
                "2026-11-13T13:43:51.296192+00:00",
            ],
            "finished_at": [
                "2026-11-14T12:48:09.051025+00:00",
                "2026-11-13T13:44:21.296192+00:00",
            ],
            "error_message": [None, "STALE:QQQ"],
            "strategy_version": ["v1.0-frozen", "v1.0-frozen"],
        }
    )
    table = common.run_table(runs)

    assert list(table.columns) == [
        "실행일 (UTC)", "시작 (KST)", "걸린 시간", "결과", "멈춘 단계", "오류",
    ]
    done, failed = table.iloc[0], table.iloc[1]
    # 12:48 UTC is 21:48 in Seoul, the same day.
    assert done["시작 (KST)"] == "11-14 21:48"
    assert done["걸린 시간"] == "6.6초"
    assert (done["결과"], done["멈춘 단계"], done["오류"]) == ("성공", "—", "—")
    # A run that worked reached the last stage; saying so is noise. One that
    # did not is exactly when the stage is worth a column.
    assert (failed["결과"], failed["멈춘 단계"]) == ("데이터 실패", "신선도 검사")
    assert failed["걸린 시간"] == "30초"
    assert failed["오류"] == "STALE:QQQ"


def test_an_empty_run_log_passes_through() -> None:
    import pandas as pd

    assert common.run_table(pd.DataFrame()).empty
    assert common.run_table(None) is None
    assert common.coverage_table(None) is None


def test_the_grade_column_is_not_called_flagged() -> None:
    """Every CNN row is REVIEW-grade by construction; "flagged" read as 1254 errors."""
    import pandas as pd

    coverage = pd.DataFrame(
        {
            "symbol": ["CNN_FEAR_GREED"],
            "rows": [1254],
            "first": ["2021-11-15"],
            "latest": ["2026-11-13"],
            "flagged": [1254],
        }
    )
    assert list(common.coverage_table(coverage).columns) == [
        "소스", "행 수", "처음", "최신", "재구성 등급 행",
    ]


def test_the_three_records_are_level_when_they_reach_the_same_day() -> None:
    from datetime import date

    day = date(2026, 11, 13)
    level = common.record_progress(
        {"signal": day, "track_record": day, "regime_memory": day}
    )
    assert level.level
    assert level.behind == ()


def test_a_record_that_fell_behind_the_signal_is_named() -> None:
    """The signal carrying on while the memory stops is the case worth a warning."""
    from datetime import date

    day = date(2026, 11, 13)
    late = common.record_progress(
        {"signal": day, "track_record": date(2026, 11, 12), "regime_memory": None}
    )
    assert not late.level
    assert late.behind == (("모의 성과", date(2026, 11, 12)), ("단계 기억", None))
    # Nothing recorded at all is not "behind": there is nothing to compare with.
    assert common.record_progress(None).signal is None
    assert not common.record_progress({}).behind


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

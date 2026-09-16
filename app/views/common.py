"""Shared plumbing for the dashboard views.

Everything here is read-only by construction. The dashboard shows what the daily
worker already computed and what the freeze already measured; it never runs an
engine (``ARCHITECTURE.md`` §4.2), which is why no compute module is imported
anywhere under ``app/``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamlit as st

from fear_ladder import paths
from fear_ladder.constants import UNKNOWN_REGIME
from fear_ladder.pipeline.queries import DashboardQueries

CACHE_SECONDS = 300

#: Fear (red) through neutral (grey) to greed (green), following the convention
#: every price chart already uses: falling is red, rising is green. A fearful
#: regime is where this strategy *adds* leverage, so the colours describe the
#: market rather than whether the day is good news for the portfolio.
REGIME_COLOURS = [
    "#b2182b", "#d6604d", "#f4a582", "#d9d9d9", "#a6d96a", "#66bd63", "#1a9850",
]
UNKNOWN_COLOUR = "#9e9e9e"

#: Colours that have to survive both themes. The regime palette above does not:
#: those fills are the fear-greed scale itself and read the same on either
#: ground. These two are drawn *against* the ground, so a single hex would
#: disappear into one theme or the other — a near-black QQQ line on Streamlit's
#: dark background is invisible, which is exactly what happened.
INK = {"light": "#222222", "dark": "#e6e8ea"}
#: Drawn under a dark mark, and over a light one. A two-layer line reads on any
#: ground without asking which ground it is — the pair always contains one mark
#: the background cannot swallow.
HALO = "#f5f5f5"
CORE = "#111111"
#: Everything a transition brake does is shown in this one colour, so a reader
#: learns it once: amber means "the score says one rung, the rules hold you on
#: another". Lifted in dark mode, where the darker amber turns to mud.
LOCK = {"light": "#8a6d1f", "dark": "#d9b04a"}

PERFORMANCE_REPORT = paths.REPORTS_DIR / "performance.json"
#: Written by scripts/transition_sensitivity.py. Optional: the dashboard shows
#: the evidence when it exists and simply omits the section when it does not.
SENSITIVITY_REPORTS = {
    "trigger": paths.REPORTS_DIR / "transition_sensitivity.json",
    "sweep": paths.REPORTS_DIR / "transition_parameter_sweep.json",
    "research": paths.REPORTS_DIR / "transition_sweep_research.json",
    "validation": paths.REPORTS_DIR / "transition_sweep_validation.json",
    # The window whose leveraged sleeves are real prices rather than
    # reconstructed ones. It reverses the other two (docs/strategy.md 2.11),
    # which is exactly why it is shown next to them.
    "real_etf": paths.REPORTS_DIR / "transition_sweep_real_etf.json",
}
STRATEGY_LABEL = "전략"


def theme() -> str:
    """``"dark"`` or ``"light"``, as the app is actually being rendered.

    Order matters. ``st.context.theme.type`` reports the *browser's* preference,
    not the theme Streamlit resolved: with ``--theme.base light`` it still says
    "dark" on a dark-mode machine, which is how the light page ended up drawing
    near-white lines on white. A configured base wins; only when there is none
    does Streamlit follow the browser, and then the browser is the right answer.

    Nothing legibility-critical should depend on this — see ``halo()``. It picks
    between two readable options, never between readable and invisible.
    """
    try:
        base = st.get_option("theme.base")
    except Exception:
        base = None
    if base in {"light", "dark"}:
        return str(base)
    try:
        return st.context.theme.type or "light"
    except Exception:  # no session, or an older Streamlit without the field
        return "light"


def ink() -> str:
    """The foreground colour for lines and text drawn on the chart ground."""
    return INK[theme()]


def lock_colour() -> str:
    """The one colour that means "a transition brake is holding this"."""
    return LOCK[theme()]


def halo() -> tuple[dict[str, object], dict[str, object]]:
    """A light line and a dark line to draw on top of it, in that order.

    Theme-proof by construction: on white the dark core carries the mark and
    the halo is invisible; on near-black the halo carries it. Used for marks
    that must never disappear, whatever the theme detection thinks.
    """
    return (
        {"color": HALO, "width": 5},
        {"color": CORE, "width": 2},
    )


def readable_on(fill: str) -> str:
    """Black or white text, whichever can be read on ``fill``.

    The regime bands run from a dark red through pale grey to a dark green, so
    one label colour cannot serve all seven — the ends were unreadable.
    """
    red, green, blue = (int(fill[index : index + 2], 16) for index in (1, 3, 5))
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "#111111" if luminance > 0.55 else "#ffffff"


def queries() -> DashboardQueries:
    """A fresh reader every call. Deliberately not cached.

    ``DashboardQueries`` holds one optional path and opens a connection per
    query, so there is nothing here worth keeping. Caching it was actively
    harmful: ``st.cache_resource`` survives a hot reload and hands back an
    instance of the *previous* class object, so freshly deployed view code went
    on calling the previous release's SQL — which is how the record tab started
    asking a DataFrame for a ``reason_codes`` column the old query never
    selected.
    """
    return DashboardQueries()


@st.cache_data(ttl=CACHE_SECONDS)
def load(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
    """One cached entry point, so every query shares the same TTL."""
    return getattr(queries(), name)(*args, **kwargs)


@st.cache_data(ttl=CACHE_SECONDS)
def performance_report(path: str | None = None) -> dict[str, Any] | None:
    """The frozen strategy's measured behaviour, or ``None`` if not generated.

    Written by ``scripts/make_performance_report.py``. Reading a file rather
    than recomputing keeps a 30-year backtest out of the page load, and keeps
    the dashboard honest: it can only show what a freeze actually measured.
    """
    target = Path(path) if path else PERFORMANCE_REPORT
    if not target.is_file():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


@st.cache_data(ttl=CACHE_SECONDS)
def sensitivity(name: str) -> dict[str, Any] | None:
    """One of the robustness measurements, or ``None`` if it was never run.

    Like the performance report, these are read rather than recomputed: the
    dashboard can only show what a measurement actually produced.
    """
    target = SENSITIVITY_REPORTS.get(name)
    if target is None or not target.is_file():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


def sensitivity_frame(report: dict[str, Any] | None, prefix: str) -> Any:
    """The variants of one family (``d=``/``c=``/``h=``/``lag``/``mean``)."""
    import pandas as pd

    if not report:
        return pd.DataFrame()
    rows = [
        {
            "variant": item["variant"].replace(" *", ""),
            "frozen": item["variant"].endswith(" *"),
            "cagr": item["cagr"],
            "max_drawdown": item["max_drawdown"],
            "sharpe": item["sharpe"],
            "regime_changes": item["regime_changes"],
        }
        for item in report["variants"]
        if item["variant"].startswith(prefix)
    ]
    return pd.DataFrame(rows)


@st.cache_data(ttl=CACHE_SECONDS)
def regime_order() -> list[str]:
    """The regimes from fear to greed, as the strategy defines them.

    Reading the configuration matters: the labels arriving from a query are in
    whatever order the database returned them, and sorting those alphabetically
    put Panic at the greed end of the palette and Euphoria at the fear end —
    every crash on the history chart was shaded as if it were a rally.
    """
    from fear_ladder.config.loader import load_config

    return list(load_config().strategy.regime.labels or ())


def regime_palette(labels: list[str]) -> dict[str, str]:
    """Colour per regime, placed by where each sits on the fear-greed range."""
    order = regime_order()
    known = [label for label in labels if label != UNKNOWN_REGIME]
    ranked = [label for label in order if label in known]
    # Anything the configuration does not know about goes after what it does,
    # so an unexpected label cannot shift the colours of the real ones.
    ranked += sorted(label for label in known if label not in order)

    last = len(REGIME_COLOURS) - 1
    span = max(len(ranked) - 1, 1)
    palette = {
        label: REGIME_COLOURS[round(index / span * last)]
        for index, label in enumerate(ranked)
    }
    palette[UNKNOWN_REGIME] = UNKNOWN_COLOUR
    return palette


@st.cache_data(ttl=CACHE_SECONDS)
def weekly_indicators() -> set[str]:
    """Indicators fed by a source that publishes weekly rather than daily.

    They have no reading on four days out of five, which is not a failure and
    must not be reported as one — the backtest treats those days the same way.
    """
    from fear_ladder.config.loader import load_config
    from fear_ladder.data.retention import WEEKLY_SOURCES

    return {
        name
        for name, spec in load_config().indicators.enabled_indicators.items()
        if spec.source in WEEKLY_SOURCES
    }


def missing_report_notice() -> None:
    st.info(
        "성과 리포트가 없습니다.\n\n"
        "`python scripts/make_performance_report.py` 로 생성하세요. "
        "이 파일은 고정된 전략을 전체 보유 기간에 대해 한 번 계산한 결과이며, "
        "대시보드는 그 결과를 읽기만 합니다."
    )


def percent(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def ratio(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


# --------------------------------------------------------------- the ladder

#: Leverage order, so a composition always reads from the most aggressive
#: sleeve to the least — "TQQQ 17 · QLD 83", never the other way round.
ASSET_ORDER = {"TQQQ": 3, "QLD": 2, "QQQ": 1, "CASH": 0}
ASSET_LABELS = {"CASH": "현금"}


@dataclass(frozen=True, slots=True)
class Rung:
    """One step of the ladder, as the frozen strategy defines it."""

    index: int
    label: str
    low: float
    high: float
    leverage: float | None
    #: Asset -> weight, the portfolio this rung *proposes*. What is actually
    #: held can differ: the trend filter and the TQQQ gate both sit downstream.
    weights: dict[str, float]

    @property
    def band(self) -> str:
        return f"{self.low:.0f} – {self.high:.0f}"

    def composition(self) -> str:
        return " · ".join(
            f"{ASSET_LABELS.get(asset, asset)} {weight * 100:.0f}"
            for asset, weight in sorted(
                self.weights.items(), key=lambda kv: -ASSET_ORDER.get(kv[0], 0)
            )
        )


def ladder(report: dict[str, Any] | None) -> list[Rung]:
    """The whole ladder, fear rung first. Empty when no freeze is on disk."""
    if not report:
        return []
    parameters = report["parameters"]
    labels: list[str] = list(parameters["regime_labels"])
    edges = [0.0, *parameters["regime_boundaries"], 100.0]
    leverages = parameters.get("ladder") or {}
    # A report written before ladder_mappings existed leaves the composition
    # blank rather than guessed at: deriving a portfolio here would mean doing
    # the allocation engine's job in the dashboard (ARCHITECTURE.md §4.2).
    mappings = parameters.get("ladder_mappings") or {}
    return [
        Rung(
            index=index,
            label=label,
            low=edges[index],
            high=edges[index + 1],
            leverage=leverages.get(label),
            weights=dict(mappings.get(label) or {}),
        )
        for index, label in enumerate(labels)
    ]


def rung_of(rungs: list[Rung], label: str | None) -> Rung | None:
    return next((rung for rung in rungs if rung.label == label), None)


@dataclass(frozen=True, slots=True)
class Gates:
    """The two score lines the current rung has to be cleared by.

    The transition engine measures hysteresis from the boundary of the regime
    being *left*, not the one being entered, so the pair depends only on where
    the score stands today. ``None`` means there is no rung that way.
    """

    down: float | None
    up: float | None


def gates(rungs: list[Rung], label: str | None, hysteresis: float) -> Gates:
    current = rung_of(rungs, label)
    if current is None:
        return Gates(None, None)
    return Gates(
        down=None if current.index == 0 else current.low - hysteresis,
        up=None if current.index == len(rungs) - 1 else current.high + hysteresis,
    )


# -------------------------------------------------------------- reason codes


@dataclass(frozen=True, slots=True)
class Reason:
    """One stored reason code, ready to show."""

    code: str
    #: ``lock`` / ``trend`` / ``gate`` / ``change`` / ``info``, so a view can
    #: decide which codes it surfaces and which stay in the raw list.
    kind: str
    text: str


def _split(code: str) -> tuple[str, str]:
    name, _, argument = code.partition(":")
    return name, argument


def _progress(argument: str) -> tuple[int, int] | None:
    done, _, total = argument.partition("/")
    try:
        return int(done), int(total)
    except ValueError:
        return None


def _signed_percent(argument: str) -> str:
    _, _, value = argument.partition("=")
    try:
        return f"{float(value) * 100:+.1f}%"
    except ValueError:
        return value


#: code name -> (kind, sentence). The sentence is a plain string when the code
#: carries no argument, and a callable taking the part after the colon when it
#: does.
_REASONS: dict[str, tuple[str, Any]] = {
    "BLOCKED_BY_HYSTERESIS": (
        "lock",
        lambda a: f"히스테리시스 {a}점 — 경계선을 스치기만 해서는 바뀌지 않습니다.",
    ),
    "BLOCKED_BY_MIN_DURATION": (
        "lock",
        lambda a: "최소 유지 기간 "
        + (f"{p[0]}/{p[1]}거래일" if (p := _progress(a)) else a)
        + " — 한 번 들어간 단계는 이만큼 지킵니다.",
    ),
    "CONFIRMATION_PENDING": (
        "lock",
        lambda a: "확인 대기 "
        + (f"{p[0]}/{p[1]}일" if (p := _progress(a)) else a)
        + " — 새 단계가 이 기간 유지돼야 인정합니다.",
    ),
    "TREND_INTACT": (
        "trend",
        lambda a: f"난간 해제 — 200일선 대비 {_signed_percent(a)}. "
        "사다리 레버리지를 그대로 씁니다.",
    ),
    "TREND_BROKEN": (
        "trend",
        lambda a: f"난간 작동 — 200일선 대비 {_signed_percent(a)}. 장기 추세가 무너진 구간입니다.",
    ),
    "LEVERAGE_CAPPED": (
        "trend",
        lambda a: f"난간이 레버리지를 {a.replace('->', 'x 에서 ')}x 로 묶었습니다.",
    ),
    "TREND_UNKNOWN": ("trend", lambda a: f"추세 지표({a})를 읽지 못했습니다."),
    "LEVERAGE_CAPPED_BY_DEFAULT": (
        "trend",
        "추세를 확인할 수 없어 방어적으로 레버리지를 묶었습니다.",
    ),
    "TREND_FILTER_DISABLED": ("trend", "추세 필터가 꺼져 있습니다."),
    "TQQQ_GATE_BLOCKED": (
        "gate",
        "바닥 확인 게이트 미통과 — 가장 공격적인 칸은 열리지 않았습니다.",
    ),
    "REQUIRED_RULE_FAILED": (
        "gate",
        lambda a: f"통과하지 못한 필수 조건: {a.replace(',', ', ')}",
    ),
    "INSUFFICIENT_CONFIRMATIONS": (
        "gate",
        lambda a: f"바닥 확인 신호 {a} — 수가 모자랍니다.",
    ),
    "TQQQ_REALLOCATED_TO": ("gate", lambda a: f"TQQQ 몫을 {a} 로 옮겼습니다."),
    "TQQQ_GATE_PASSED": ("gate", "바닥 확인 게이트 통과 — TQQQ 를 들 수 있습니다."),
    "TQQQ_GATE_DISABLED": ("gate", "바닥 확인 게이트가 꺼져 있습니다."),
    "CONFIRMATIONS": ("gate", lambda a: f"바닥 확인 신호 {a}."),
    "REGIME_CONFIRMED": ("change", "**오늘 단계가 바뀌었습니다.**"),
    "MOVED_TOWARD_FEAR": ("change", "공포 쪽으로 한 칸 — 레버리지가 올라갑니다."),
    "MOVED_TOWARD_GREED": ("change", "탐욕 쪽으로 한 칸 — 레버리지가 내려갑니다."),
    "INITIAL_REGIME": ("change", "첫 판정 — 비교할 이전 단계가 없습니다."),
    "REGIME_HELD": ("info", "점수가 같은 칸에 머물러 단계를 유지했습니다."),
    "UNKNOWN_INPUT": ("info", "필수 데이터를 믿을 수 없어 판정하지 않았습니다."),
    "REGIME_MAPPING": ("info", lambda a: f"{a} 단계의 배분표를 적용했습니다."),
    "TARGET_LEVERAGE": ("info", lambda a: f"목표 레버리지 {a}x."),
}


def reason(code: str) -> Reason:
    """Turn one stored code into a sentence a person can read.

    The codes are the engine's own words (``regime.transition.ReasonCode`` and
    the allocation modules). This is the only place they are translated, so no
    view has to know their shape — and an unrecognised code still shows, as
    itself, rather than disappearing.
    """
    name, argument = _split(code)
    kind, phrase = _REASONS.get(name, ("info", None))
    if phrase is None:
        return Reason(code, kind, code)
    return Reason(code, kind, phrase(argument) if callable(phrase) else phrase)


def reasons_of(raw: object) -> list[Reason]:
    """Every stored code for one day, translated. Bad JSON reads as no codes."""
    try:
        codes = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return [reason(str(code)) for code in codes]


@dataclass(frozen=True, slots=True)
class Lock:
    """Why today's score did not move the ladder."""

    kind: str  # hysteresis | min_duration | confirmation
    text: str
    #: ``(elapsed, required)`` for the two brakes that count days.
    progress: tuple[int, int] | None = None

    @property
    def is_ready(self) -> bool:
        """Whether the count is full, so the next day could release the rung."""
        return self.progress is not None and self.progress[0] >= self.progress[1]


LOCK_KINDS = {
    "BLOCKED_BY_HYSTERESIS": "hysteresis",
    "BLOCKED_BY_MIN_DURATION": "min_duration",
    "CONFIRMATION_PENDING": "confirmation",
}


def locks(raw: object) -> list[Lock]:
    """The brakes that fired on one day, in the order the engine applies them."""
    found = []
    for item in reasons_of(raw):
        name, argument = _split(item.code)
        if (kind := LOCK_KINDS.get(name)) is None:
            continue
        found.append(Lock(kind=kind, text=item.text, progress=_progress(argument)))
    return found


def is_locked(row: object) -> bool:
    """Whether the score pointed at one rung while another was held."""
    raw = row["raw_regime"] if row["raw_regime"] else None  # type: ignore[index]
    return bool(raw) and raw != row["regime"] and raw != UNKNOWN_REGIME  # type: ignore[index]


#: How heavily a regime band is painted behind a chart. The dark ground eats a
#: light wash, so the same alpha that reads as a tint on white reads as nothing
#: at all on #0e1117.
BAND_OPACITY = {"light": 0.22, "dark": 0.42}


def band_opacity() -> float:
    return BAND_OPACITY[theme()]


#: Direction of a confirmed regime change, for a log a person reads. The engine
#: also writes REGIME_CONFIRMED on every one of these rows, which says nothing
#: a table of confirmed changes has not already said.
DIRECTIONS = {
    "MOVED_TOWARD_FEAR": "공포 쪽으로 한 칸",
    "MOVED_TOWARD_GREED": "탐욕 쪽으로 한 칸",
}


def change_direction(raw: object) -> str:
    """Which way a stored regime change went, in words."""
    for item in reasons_of(raw):
        if (direction := DIRECTIONS.get(item.code)) is not None:
            return direction
    return " · ".join(item.text for item in reasons_of(raw)) or "—"


def regime_event_table(events: Any) -> Any:
    """The regime-change log as a person reads it, not as it is stored.

    Column names and a JSON array of reason codes are the storage format; a
    reader wants dates, stages and a direction.
    """
    if events is None or events.empty:
        return events
    return (
        events.assign(사유=lambda frame: frame["reason_codes"].map(change_direction))
        .drop(columns=["reason_codes"])
        .rename(
            columns={
                "event_date": "날짜",
                "previous_regime": "이전 단계",
                "new_regime": "새 단계",
                "previous_score": "이전 점수",
                "new_score": "새 점수",
            }
        )
    )


# ------------------------------------------------------------------ streaks


@dataclass(frozen=True, slots=True)
class Streak:
    """How long the current rung has been held, and what came before it."""

    regime: str
    days: int
    since: Any
    previous: str | None = None
    previous_days: int | None = None


def streak(history: Any) -> Streak | None:
    """Read the current run of days off the stored history.

    Counting rows rather than calendar days is deliberate: the transition
    engine's ``minimum_duration_days`` counts the days it actually stepped, and
    those are trading days.
    """
    if history is None or history.empty or "regime" not in history.columns:
        return None
    regimes = [str(value) for value in history["regime"].tolist()]
    moments = list(history.index)
    current = regimes[-1]

    days = 0
    for value in reversed(regimes):
        if value != current:
            break
        days += 1

    previous = previous_days = None
    if days < len(regimes):
        previous = regimes[-days - 1]
        previous_days = 0
        for value in reversed(regimes[: len(regimes) - days]):
            if value != previous:
                break
            previous_days += 1

    return Streak(
        regime=current,
        days=days,
        since=moments[len(moments) - days],
        previous=previous,
        previous_days=previous_days,
    )

"""The shared measurement plumbing (``fear_ladder.research.measurement``).

Nine scripts under ``scripts/`` each used to carry their own copy of these three
pieces, and each carried its own copy of the tests for them. The property that
mattered in every one of those copies is the same, and it is the one this file
exists to hold: **a variant must not contaminate the frozen configuration.**
``config/`` is the freeze's evidence (``BACKTEST_SPEC.md`` §27), and every number
in ``docs/strategy.md`` §2.12–§2.18 is measured against it. A variant that
mutated it in place would turn each later window into a comparison of one thing
with itself — silently, and in a way no single report would reveal.
"""

from __future__ import annotations

from datetime import date

import pytest

from fear_ladder.config.loader import load_config
from fear_ladder.research.measurement import (
    ERAS,
    PARAMETER_FAMILIES,
    Era,
    eras,
    frozen_value,
    with_block,
    with_parameter,
)

# ------------------------------------------------------------------ the eras


def test_an_era_unpacks_as_the_plain_triple_the_reports_are_built_from() -> None:
    assert ERAS["full"] == ("full 1996-2026", None, None)
    label, start, end = ERAS["gfc"]
    assert (label, start, end) == ("gfc 2007-2009", date(2007, 1, 1), date(2009, 12, 31))


def test_the_eras_come_back_in_the_order_asked_for() -> None:
    """Each script reports its own subset in its own order."""
    assert eras("real", "full") == (ERAS["real"], ERAS["full"])
    assert eras() == ()


def test_a_misspelled_era_is_refused_rather_than_silently_dropped() -> None:
    """Measuring four eras where five were meant must not pass unnoticed."""
    with pytest.raises(KeyError, match="dot-com"):
        eras("full", "dot-com")


def test_the_real_era_starts_where_the_sleeves_stop_being_modelled() -> None:
    """``docs/strategy.md`` §2.11 turns on this one date."""
    config = load_config()
    symbols = config.data_sources.price.symbols
    assert ERAS["real"].start == max(symbols[name].inception for name in ("QLD", "TQQQ"))


def test_every_era_that_has_both_ends_runs_forwards() -> None:
    for era in ERAS.values():
        if era.start is not None and era.end is not None:
            assert era.start < era.end, era.label


# -------------------------------------------------------------- the variants


def test_a_variant_leaves_the_frozen_config_alone() -> None:
    """The property every one of these scripts depends on."""
    config = load_config()
    before = config.strategy.trend_filter.max_leverage_below

    variant = with_block(config, "trend_filter", max_leverage_below=1.5)

    assert variant is not config
    assert variant.strategy.trend_filter.max_leverage_below == 1.5
    assert config.strategy.trend_filter.max_leverage_below == before


def test_a_variant_changes_nothing_but_the_named_field() -> None:
    config = load_config()
    variant = with_block(config, "trend_filter", max_leverage_below=0.0)

    original = config.strategy.trend_filter.model_dump()
    updated = variant.strategy.trend_filter.model_dump()
    changed = {key for key in original if original[key] != updated[key]}
    assert changed == {"max_leverage_below"}
    assert variant.strategy.transition == config.strategy.transition
    assert variant.indicators == config.indicators


def test_a_duration_variant_stays_an_integer() -> None:
    """The sweeps are floats for uniformity; the schema's field is not."""
    variant = with_parameter(load_config(), "minimum_duration_days", 120.0)
    assert variant.strategy.transition.minimum_duration_days == 120
    assert isinstance(variant.strategy.transition.minimum_duration_days, int)


def test_an_unknown_parameter_family_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown parameter family"):
        with_parameter(load_config(), "regime_count", 5.0)
    with pytest.raises(ValueError, match="unknown parameter family"):
        frozen_value(load_config(), "regime_count")


@pytest.mark.parametrize("family", sorted(PARAMETER_FAMILIES))
def test_every_declared_family_names_a_field_that_exists(family: str) -> None:
    """A renamed schema field must break here, not in a report nobody re-reads."""
    config = load_config()
    block, _ = PARAMETER_FAMILIES[family]
    assert hasattr(config.strategy, block), block
    assert hasattr(getattr(config.strategy, block), family), family


def test_the_frozen_value_is_read_from_the_right_block() -> None:
    config = load_config()
    assert frozen_value(config, "max_leverage_below") == pytest.approx(
        config.strategy.trend_filter.max_leverage_below
    )
    assert frozen_value(config, "minimum_duration_days") == pytest.approx(
        config.strategy.transition.minimum_duration_days
    )


def test_setting_a_family_to_its_frozen_value_is_a_no_op_in_content() -> None:
    config = load_config()
    held = frozen_value(config, "hysteresis")
    assert with_parameter(config, "hysteresis", held).strategy == config.strategy


# -------------------------------------------------------------- the numbers


def test_the_headline_reads_the_four_numbers_off_one_run() -> None:
    """A stub run: the point is the wiring, not the simulator."""
    from types import SimpleNamespace

    import pandas as pd

    from fear_ladder.research.measurement import headline

    days = pd.bdate_range("2020-01-01", periods=260).date.tolist()
    nav = pd.Series([100.0 * (1.001**index) for index in range(260)], index=days)
    nav.iloc[130] = nav.iloc[129] * 0.5  # one deep, dated decline

    run = SimpleNamespace(
        result=SimpleNamespace(nav=nav),
        regime_change_count=3,
    )
    data = SimpleNamespace(cash_rates=None)

    stats = headline(run, data)  # type: ignore[arg-type]
    assert set(stats) == {"cagr", "max_drawdown", "sharpe", "regime_changes"}
    assert stats["regime_changes"] == 3.0
    assert stats["max_drawdown"] == pytest.approx(float((nav / nav.cummax() - 1.0).min()))


def test_the_headline_drawdown_does_not_depend_on_the_navs_scale() -> None:
    """Every script used to renormalise the curve before measuring it."""
    from types import SimpleNamespace

    import pandas as pd

    from fear_ladder.research.measurement import headline

    days = pd.bdate_range("2020-01-01", periods=60).date.tolist()
    nav = pd.Series([100.0, *(90.0 + index for index in range(59))], index=days)
    data = SimpleNamespace(cash_rates=None)

    plain = headline(SimpleNamespace(result=SimpleNamespace(nav=nav), regime_change_count=0), data)  # type: ignore[arg-type]
    scaled = headline(
        SimpleNamespace(result=SimpleNamespace(nav=nav / nav.iloc[0]), regime_change_count=0),  # type: ignore[arg-type]
        data,  # type: ignore[arg-type]
    )
    assert plain["max_drawdown"] == pytest.approx(scaled["max_drawdown"])
    assert plain["cagr"] == pytest.approx(scaled["cagr"])


def test_an_era_is_still_a_named_tuple_so_reports_can_unpack_it() -> None:
    era = Era("x", None, None)
    assert isinstance(era, tuple)
    assert era.label == "x"

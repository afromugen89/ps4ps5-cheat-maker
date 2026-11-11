"""core/trace_way: the maths of how a real value becomes the shown one (AC-T3/T5/T7)."""

from __future__ import annotations

import math
import struct

import pytest

from cheatmaker.core.trace_way import Kind, Step, Way, as_shown
from cheatmaker.core.values import ValueType
from tests.minicpu import bits_f32, f32_bits

F, D = ValueType.FLOAT, ValueType.DOUBLE
I8, I16, I32, I64 = ValueType.INT8, ValueType.INT16, ValueType.INT32, ValueType.INT64

X100_CUT = Way(F, I32, (Step(Kind.MUL, F, const=100.0), Step(Kind.TO_WHOLE, I32, rounding="cut")))
X100_ROUND = Way(
    F, I32, (Step(Kind.MUL, F, const=100.0), Step(Kind.TO_WHOLE, I32, rounding="round"))
)
CUT = Way(F, I32, (Step(Kind.TO_WHOLE, I32, rounding="cut"),))
SHARE = Way(
    F,
    I32,
    (
        Step(Kind.SHARE, F, max_at=0x2000_0014),
        Step(Kind.MUL, F, const=100.0),
        Step(Kind.TO_WHOLE, I32, rounding="cut"),
    ),
)


def cpu_cut_x100(x: float) -> int:
    """What mulss by 100.0f then cvttss2si gives, on float32 bits."""
    return math.trunc(bits_f32(f32_bits(bits_f32(f32_bits(x)) * 100.0)))


def test_as_shown_is_exact_and_factor_1():
    w = as_shown(I32)
    assert w.factor() == 1 and w.rounding == "exact" and w.label() == "as shown"
    assert w.to_screen(51) == 51 and w.to_real(51) == 51
    assert w.matches(51, 51) and not w.matches(51, 52)
    assert w.screen_text() == "as is"


def test_steps_must_end_in_the_shown_type():
    with pytest.raises(ValueError):
        Way(F, I32)
    with pytest.raises(ValueError):
        Way(F, I32, (Step(Kind.MUL, F, const=100.0),))


def test_x100_cut_forward_is_float32_faithful():
    for x in (0.87, 0.29, 0.5, 0.01, 1.0, 0.999, 0.333):
        assert X100_CUT.to_screen(bits_f32(f32_bits(x))) == cpu_cut_x100(x)
    assert X100_CUT.factor() == 100
    assert X100_CUT.label() == "x0.01 of the screen" and X100_CUT.screen_text() == "x100"
    assert X100_CUT.rounding == "cut"


@pytest.mark.parametrize("screen", [0, 1, 29, 57, 87, 99, 100, 250])
def test_x100_cut_inverse_round_trips(screen):
    r = X100_CUT.to_real(screen)
    assert struct.pack("<f", r) == F.encode(r), "a float32"
    assert X100_CUT.to_screen(r) == screen
    assert cpu_cut_x100(r) == screen


def test_inverse_round_trips_for_every_type_and_rounding():
    ways = [
        as_shown(F),
        as_shown(D),
        as_shown(I8),
        as_shown(I64),
        X100_ROUND,
        CUT,
        Way(I32, F, (Step(Kind.TO_DECIMAL, F),)),
        Way(F, D, (Step(Kind.WIDEN, D),)),
        Way(D, F, (Step(Kind.NARROW, F),)),
        Way(I16, I32, (Step(Kind.EXTEND, I32, signed=True),)),
        Way(D, D, (Step(Kind.DIV, D, const=2.0),)),
        Way(I64, D, (Step(Kind.TO_DECIMAL, D), Step(Kind.MUL, D, const=0.001))),
    ]
    for w in ways:
        for screen in (0, 1, 7, 87, 1234):
            if not w.shown.is_float and screen > w.shown.int_range()[1]:
                continue
            r = w.to_real(screen)
            assert w.matches(r, w.to_screen(r))
            got = w.to_screen(r)
            assert w.shown.encode(got) == w.shown.encode(screen), (w.text(), screen, r)


def test_to_real_gives_the_nearest_when_nothing_is_exact():
    w = Way(I32, I32, (Step(Kind.MUL, I32, const=10),))
    assert w.to_real(87) == 9  # 87 can't be shown: x10 shows 90
    assert w.real_for(87) == (9, False) and w.real_for(90) == (9, True)
    assert X100_CUT.real_for(29)[1], "nudged to an exact one"
    with pytest.raises(ValueError):
        Way(F, F, (Step(Kind.MUL, F, const=0.0),)).to_real(5)


def test_matches_rules():
    # cut: floor(x) == shown, toward zero
    assert CUT.matches(87.9, 87) and not CUT.matches(88.0, 87)
    assert CUT.matches(-87.9, -87)
    # round: |x - shown| <= 0.5
    w = Way(F, I32, (Step(Kind.TO_WHOLE, I32, rounding="round"),))
    assert w.matches(87.4, 87) and w.matches(86.5, 87) and not w.matches(86.4, 87)
    # decimals: within 1% or one unit
    d = as_shown(F)
    assert d.rounding == "decimal"
    assert d.matches(87.4, 87.0, unit=1.0) and not d.matches(85.0, 87.0, unit=1.0)
    assert d.matches(1000.0, 1009.0, unit=0.1) and not d.matches(1000.0, 1011.0, unit=0.1)
    assert not d.matches(float("nan"), 87.0) and not d.matches(87.0, float("inf"))
    # whole numbers: exact
    assert as_shown(I64).matches(51, 51) and not as_shown(I64).matches(51, 50)


def test_share_of_max_uses_the_max_now():
    assert SHARE.to_screen(43.5, 50.0) == 87
    assert SHARE.to_screen(43.5, {0x2000_0014: 50.0}) == 87
    assert SHARE.matches(43.5, 87, 50.0) and not SHARE.matches(40.0, 87, 50.0)
    assert SHARE.factor(50.0) == 2 and SHARE.label() == "share of max"
    assert SHARE.screen_text() == "as a share of a max"
    assert SHARE.max_addresses == (0x2000_0014,)
    assert SHARE.to_screen(SHARE.to_real(87, 200.0), 200.0) == 87
    with pytest.raises(ValueError):
        SHARE.to_screen(43.5)
    assert not SHARE.matches(43.5, 87), "no max: no match"


def test_extend_unsigned_and_wrapping():
    w = Way(I8, I32, (Step(Kind.EXTEND, I32, signed=False),))
    assert w.to_screen(-1) == 255
    w = Way(I32, I32, (Step(Kind.MUL, I32, const=2),))
    assert w.to_screen(0x7FFF_FFFF) == -2, "wraps like imul"


def test_float32_overflow_and_out_of_range_whole():
    big = Way(F, F, (Step(Kind.MUL, F, const=1e30),))
    assert big.to_screen(1e10) == math.inf
    assert CUT.to_screen(3e9) == I32.int_range()[0], "cvtt*: indefinite"
    assert CUT.to_screen(float("nan")) == I32.int_range()[0]


def test_whole_conversion_into_a_small_type_wraps_like_the_cpu():
    """cvttss2si writes a 4-byte register; a 1- or 2-byte copy keeps its low bits."""
    w = Way(F, I8, (Step(Kind.TO_WHOLE, I8, rounding="cut"),))
    assert w.to_screen(300.7) == 44 and w.to_screen(-129.0) == 127
    assert w.to_screen(5e9) == 0, "indefinite 0x80000000 keeps its low byte"
    w = Way(F, I16, (Step(Kind.TO_WHOLE, I16, rounding="round"),))
    assert w.to_screen(70000.0) == 70000 - 65536


def test_int64_to_float_rounds_once():
    n = 2**60 + 2**36 + 1  # just above half a float32 step: rounds up
    w = Way(I64, F, (Step(Kind.TO_DECIMAL, F),))
    assert w.to_screen(n) == float(2**60 + 2**37)
    assert w.to_screen(-n) == -float(2**60 + 2**37)
    assert w.to_screen(2**60 + 2**36) == float(2**60), "a tie goes to even"
    assert w.to_screen(51) == 51.0


def test_nudge_after_narrowing_steps_in_float32():
    """QA BUG-04: 2.37 as a double shows 236 through cvtsd2ss, x100, cut; the nudge
    has to step float32 values (2.3700001...) to reach 237."""
    w = Way(
        D,
        I32,
        (
            Step(Kind.NARROW, F),
            Step(Kind.MUL, F, const=100.0),
            Step(Kind.TO_WHOLE, I32, rounding="cut"),
        ),
    )
    real, exact = w.real_for(237)
    assert exact and w.to_screen(real) == 237 and real != 2.37


def test_a_decimal_screen_value_is_exact_to_its_digits():
    """QA BUG-05: 127.0000076 on a decimal screen is 127."""
    w = Way(F, F, (Step(Kind.MUL, F, const=1000.0),))
    assert w.real_for(127.0)[1]


def test_then_composes_hops():
    inner = Way(F, F, (Step(Kind.MUL, F, const=100.0),))  # real -> widget (x100, float)
    outer = Way(F, I32, (Step(Kind.TO_WHOLE, I32, rounding="cut"),))  # widget -> HUD
    total = inner.then(outer)
    assert total.real is F and total.shown is I32 and total.factor() == 100
    assert total.to_screen(bits_f32(f32_bits(0.87))) == cpu_cut_x100(0.87)
    with pytest.raises(ValueError):
        outer.then(inner)


def test_as_scale_and_text():
    s = X100_CUT.as_scale()
    assert s.label == "traced x0.01" and s.factor == pytest.approx(0.01) and s.decimal
    assert not as_shown(I64).as_scale().decimal
    assert X100_CUT.text() == "FLOAT -> x100 -> cut to whole -> INT32"
    assert SHARE.steps[0].text() == "share of max at 0x20000014"
    assert Step(Kind.DIV, F, const=2.0).text() == "/2"
    assert Step(Kind.EXTEND, I32, signed=False).text() == "zero-extend to 4 bytes"

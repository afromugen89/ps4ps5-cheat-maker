import struct

import pytest

from cheatmaker.core.values import (
    Compare,
    ValueType,
    guess_types,
    in_filter,
    matches,
    parse_filter,
    parse_number,
)


@pytest.mark.parametrize(
    "text,expected",
    [("2,450", 2450), (" 99 999 ", 99999), ("87.5", 87.5), ("-3", -3), ("1e3", 1000.0)],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "nan", "inf", "1.2.3"])
def test_parse_number_rejects(text):
    with pytest.raises(ValueError):
        parse_number(text)


def test_encode_decode_and_range():
    assert ValueType.INT16.decode(ValueType.INT16.encode(-2)) == -2
    with pytest.raises(ValueError):
        ValueType.INT8.encode(200)
    assert ValueType.INT8.clamp(1000) == 127
    assert ValueType.FLOAT.decode(ValueType.FLOAT.encode(1.5)) == 1.5


def test_matches_exact_and_float_tolerance():
    assert matches(ValueType.INT32, Compare.EXACT, struct.pack("<i", 7), target=7)
    assert not matches(ValueType.INT32, Compare.EXACT, struct.pack("<i", 8), target=7)
    assert matches(ValueType.FLOAT, Compare.EXACT, struct.pack("<f", 86.7), target=87)
    assert not matches(ValueType.FLOAT, Compare.EXACT, struct.pack("<f", float("nan")), target=1)
    with pytest.raises(ValueError):
        matches(ValueType.INT32, Compare.EXACT, b"\0" * 4)


@pytest.mark.parametrize(
    "cmp,prev,cur,ok",
    [
        (Compare.INCREASED, 5, 6, True),
        (Compare.INCREASED, 5, 5, False),
        (Compare.DECREASED, 5, 4, True),
        (Compare.CHANGED, 5, 5, False),
        (Compare.CHANGED, 5, 9, True),
        (Compare.UNCHANGED, 5, 5, True),
    ],
)
def test_matches_relative(cmp, prev, cur, ok):
    p, c = struct.pack("<i", prev), struct.pack("<i", cur)
    assert matches(ValueType.INT32, cmp, c, p) is ok


def test_relative_needs_previous():
    with pytest.raises(ValueError):
        matches(ValueType.INT32, Compare.DECREASED, b"\0" * 4)


def test_guess_types():
    assert guess_types(87.5)[0] is ValueType.FLOAT
    assert guess_types(100)[:2] == [ValueType.INT32, ValueType.FLOAT]
    assert ValueType.INT16 in guess_types(100)
    assert guess_types(2**40)[0] is ValueType.INT64


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("100", (99.5, 100.5)),
        ("90-110", (90.0, 110.0)),
        ("90 to 110", (90.0, 110.0)),
        ("110-90", (90.0, 110.0)),
        ("-5 - 5", (-5.0, 5.0)),
        ("-10", (-10.5, -9.5)),
        ("1,000 .. 2,000", (1000.0, 2000.0)),
    ],
)
def test_parse_filter(text, expected):
    assert parse_filter(text) == expected


@pytest.mark.parametrize("text", ["abc", "90-", "1-x"])
def test_parse_filter_rejects(text):
    with pytest.raises(ValueError):
        parse_filter(text)


def test_in_filter():
    assert in_filter(100, parse_filter("100"))
    assert not in_filter(101, parse_filter("100"))
    assert in_filter(86.7, parse_filter("87"))  # rounded float on screen
    assert in_filter(5, None)
    assert not in_filter(float("nan"), (0.0, 1.0))

"""Value types and comparisons used by the scanner (AC-3.3, AC-3.6)."""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from enum import Enum


class ValueType(Enum):
    # (label, struct code, size, is_float)
    INT8 = ("Whole number (1 byte)", "b", 1, False)
    INT16 = ("Whole number (2 bytes)", "h", 2, False)
    INT32 = ("Whole number (4 bytes)", "i", 4, False)
    INT64 = ("Whole number (8 bytes)", "q", 8, False)
    FLOAT = ("Decimal (4 bytes)", "f", 4, True)
    DOUBLE = ("Decimal (8 bytes)", "d", 8, True)

    def __init__(self, label: str, code: str, size: int, is_float: bool) -> None:
        self.label = label
        self.code = code
        self.size = size
        self.is_float = is_float

    @property
    def fmt(self) -> str:
        return "<" + self.code

    def encode(self, value: float | int) -> bytes:
        """Little-endian bytes for ``value``. Raises ValueError if it doesn't fit."""
        if self.is_float:
            return struct.pack(self.fmt, float(value))
        iv = int(value)
        lo, hi = self.int_range()
        if not lo <= iv <= hi:
            raise ValueError(f"{iv} doesn't fit in {self.label.lower()}")
        return struct.pack(self.fmt, iv)

    def decode(self, data: bytes) -> float | int:
        return struct.unpack(self.fmt, data[: self.size])[0]

    def int_range(self) -> tuple[int, int]:
        bits = self.size * 8
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1

    def clamp(self, value: float | int) -> float | int:
        if self.is_float:
            return float(value)
        lo, hi = self.int_range()
        return max(lo, min(hi, int(value)))


class Compare(Enum):
    EXACT = "exact"
    UNKNOWN = "unknown"  # first scan only: keep everything
    INCREASED = "increased"
    DECREASED = "decreased"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    BETWEEN = "between"  # value is (low, high), both included
    INCREASED_BY = "increased by"  # value is (amount, tolerance), AC-H1
    DECREASED_BY = "decreased by"

    @property
    def needs_value(self) -> bool:
        return self in (Compare.EXACT, Compare.BETWEEN, Compare.INCREASED_BY, Compare.DECREASED_BY)

    @property
    def needs_previous(self) -> bool:
        return self in (
            Compare.INCREASED,
            Compare.DECREASED,
            Compare.CHANGED,
            Compare.UNCHANGED,
            Compare.INCREASED_BY,
            Compare.DECREASED_BY,
        )

    @property
    def by_amount(self) -> bool:
        return self in (Compare.INCREASED_BY, Compare.DECREASED_BY)


FLOAT_TOLERANCE = 1.0
"""Games often show a rounded or cut-off float (health 87 might be 86.7 or 87.4), so an
exact float match accepts anything less than one unit of the typed number away (AC-H2).
Typed decimals get a smaller unit, see ``unit_of``. Same rule as PS5Debug-NG FuzzyValue."""


def matches(
    vtype: ValueType,
    cmp: Compare,
    current: bytes,
    previous: bytes | None = None,
    target: float | int | None = None,
) -> bool:
    """True if ``current`` satisfies the comparison."""
    if cmp is Compare.UNKNOWN:
        return True
    cur = vtype.decode(current)
    if vtype.is_float and (math.isnan(cur) or math.isinf(cur)):
        return False
    if cmp is Compare.EXACT:
        if target is None:
            raise ValueError("exact search needs a number")
        if vtype.is_float:
            return abs(cur - float(target)) <= FLOAT_TOLERANCE
        return cur == int(target)
    if cmp is Compare.BETWEEN:
        lo, hi = target  # type: ignore[misc]
        return lo <= cur <= hi
    if previous is None:
        raise ValueError(f"{cmp.value} needs a previous scan")
    prev = vtype.decode(previous)
    if cmp.by_amount:
        if target is None:
            raise ValueError(f"{cmp.value} needs an amount")
        amount, tol = target  # type: ignore[misc]
        diff = cur - prev if cmp is Compare.INCREASED_BY else prev - cur
        if vtype.is_float:
            return not math.isnan(prev) and abs(diff - amount) < tol
        return diff == int(amount)
    if cmp is Compare.INCREASED:
        return cur > prev
    if cmp is Compare.DECREASED:
        return cur < prev
    if cmp is Compare.CHANGED:
        return current != previous
    return current == previous  # UNCHANGED


def parse_number(text: str) -> float | int:
    """Parse what the player typed: "2,450", "99 999", "87.5". Raises ValueError."""
    cleaned = text.strip().replace(",", "").replace(" ", "").replace("_", "")
    if not cleaned:
        raise ValueError("Type a number first.")
    try:
        if any(c in cleaned for c in ".eE"):
            f = float(cleaned)
            if math.isnan(f) or math.isinf(f):
                raise ValueError
            return f
        return int(cleaned)
    except ValueError:
        raise ValueError(f'"{text}" isn\'t a number.') from None


@dataclass(frozen=True)
class Scale:
    """How the game stores what it shows (AC-H3): stored = shown x factor."""

    label: str
    factor: float
    decimal: bool = False  # fractions only fit in Decimal types

    def apply(self, value: float | int) -> float | int:
        if self.factor == 1:
            return value
        scaled = value * self.factor
        if isinstance(value, int) and not self.decimal and float(scaled).is_integer():
            return int(scaled)
        return scaled


SHOWN = Scale("the number shown", 1)


def make_scale(kind: str, max_value: float | int = 100) -> Scale:
    """``kind``: "shown", "fraction", "x10", "x100" or "x1000". Raises ValueError."""
    if kind == "shown":
        return SHOWN
    if kind == "fraction":
        if not max_value or max_value <= 0:
            raise ValueError("The most it can be must be more than 0.")
        return Scale(f"a fraction of {max_value:g}", 1 / max_value, decimal=True)
    if kind in ("x10", "x100", "x1000"):
        return Scale(kind, int(kind[1:]))
    raise ValueError(f"unknown scale {kind!r}")


def unit_of(text: str) -> float:
    """One unit of the last digit the player typed: "87" -> 1, "87.5" -> 0.1,
    "0.87" -> 0.01 (AC-H2). Exponent forms count as whole units of their value."""
    cleaned = text.strip().replace(",", "").replace(" ", "").replace("_", "")
    if "e" in cleaned.lower() or "." not in cleaned:
        return 1.0
    decimals = len(cleaned.split(".", 1)[1])
    return 10.0**-decimals if decimals else 1.0


def parse_amount(text: str) -> tuple[float | int, float]:
    """A number the player typed plus its unit (see ``unit_of``)."""
    return parse_number(text), unit_of(text)


def guess_types(value: float | int) -> list[ValueType]:
    """Most likely types for a number the player sees, best first (AC-3.6).

    Most games keep counters in 4-byte ints and bars in 4-byte floats, so those come
    first; the scanner tries the next type when one finds nothing.
    """
    if isinstance(value, float) and not value.is_integer():
        return [ValueType.FLOAT, ValueType.DOUBLE]
    iv = int(value)
    order = [ValueType.INT32, ValueType.FLOAT]
    if not ValueType.INT32.int_range()[0] <= iv <= ValueType.INT32.int_range()[1]:
        order = [ValueType.INT64, ValueType.DOUBLE]
    elif 0 <= iv <= 0x7FFF:
        order += [ValueType.INT16]
    return [*order, ValueType.DOUBLE] if ValueType.DOUBLE not in order else order


_RANGE = re.compile(r"^\s*(-?[^\s-][^-]*?)\s*(?:-|to|\.\.)\s*(-?[^\s-].*?)\s*$", re.IGNORECASE)


def parse_filter(text: str) -> tuple[float, float] | None:
    """What to keep in the match list: "100" or "90-110" / "90 to 110".

    Returns (low, high), or None for an empty box. Raises ValueError for text
    that isn't a number or a range. A single number keeps values less than one unit
    of it away (AC-H2), so a float showing 86.7 still matches a typed 87 and a typed
    0.87 keeps 0.865 but not 0.37.
    """
    if not text.strip():
        return None
    m = _RANGE.match(text)
    if m:
        lo, hi = float(parse_number(m.group(1))), float(parse_number(m.group(2)))
        return (min(lo, hi), max(lo, hi))
    n = float(parse_number(text))
    u = unit_of(text)
    # One unit either side, open ends: the next representable float inside the bound.
    return (math.nextafter(n - u, math.inf), math.nextafter(n + u, -math.inf))


def in_filter(value: float | int, keep: tuple[float, float] | None) -> bool:
    if keep is None:
        return True
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return False
    return keep[0] <= value <= keep[1]

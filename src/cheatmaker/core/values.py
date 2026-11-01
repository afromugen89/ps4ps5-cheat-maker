"""Value types and comparisons used by the scanner (AC-3.3, AC-3.6)."""

from __future__ import annotations

import math
import struct
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

    @property
    def needs_value(self) -> bool:
        return self is Compare.EXACT

    @property
    def needs_previous(self) -> bool:
        return self in (Compare.INCREASED, Compare.DECREASED, Compare.CHANGED, Compare.UNCHANGED)


FLOAT_TOLERANCE = 0.5
"""Games often show a rounded float (health 87 might be 86.7), so exact float match
accepts anything that rounds to the typed number."""


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
    if previous is None:
        raise ValueError(f"{cmp.value} needs a previous scan")
    prev = vtype.decode(previous)
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

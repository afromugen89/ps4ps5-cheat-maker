"""How a real value becomes the number on screen (spec-trace "Way", AC-T3, AC-T5, AC-T7).

Plain number maths, no x86: ``core/trace_flow`` records the steps the game's code
takes from the source to the copy, and this module works them forward (what the
screen shows for a real value, float32 rounding per step as the CPU does), backward
(the real value to write for a typed screen number), and checks a candidate source
against the copy with the AC-T5 rules.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from cheatmaker.core.values import Scale, ValueType

NUDGE_STEPS = 8
"""How many float32 / whole steps ``to_real`` tries either side of the first guess."""
DECIMAL_TOL = 0.01  # AC-T5: decimals match within 1% (or one unit, the larger)

Rounding = Literal["cut", "round"]
MaxNow = float | int | Mapping[int, float | int] | None
"""The max of a "share of max" way, read now: one number, or {max address: value}."""


class Kind(Enum):
    EXTEND = "extend"  # movzx/movsx/movsxd, 32-bit mov zero-extends
    TRUNCATE = "truncate"  # a wider whole number of which only the low bytes are stored
    TO_DECIMAL = "to decimal"  # cvtsi2ss / cvtsi2sd
    TO_WHOLE = "to whole"  # cvttss2si/cvttsd2si (cut) | cvtss2si/cvtsd2si (round)
    WIDEN = "widen"  # cvtss2sd
    NARROW = "narrow"  # cvtsd2ss
    MUL = "mul"  # by a constant
    DIV = "div"  # by a constant
    SHARE = "share of max"  # divided by a loaded max, read now


@dataclass(frozen=True)
class Step:
    kind: Kind
    to: ValueType  # number type after this step
    const: float | None = None  # MUL / DIV
    rounding: Rounding | None = None  # TO_WHOLE
    signed: bool = True  # EXTEND
    max_at: int | None = None  # SHARE: the max's address

    def text(self) -> str:
        """For Advanced and the log: "x100", "/2", "cut to whole", "share of max"."""
        if self.kind is Kind.MUL:
            return f"x{self.const:g}"
        if self.kind is Kind.DIV:
            return f"/{self.const:g}"
        if self.kind is Kind.TO_WHOLE:
            return f"{self.rounding} to whole"
        if self.kind is Kind.SHARE:
            at = f" at {self.max_at:#x}" if self.max_at is not None else ""
            return f"share of max{at}"
        if self.kind is Kind.EXTEND:
            return f"{'sign' if self.signed else 'zero'}-extend to {self.to.size} bytes"
        if self.kind is Kind.TRUNCATE:
            return f"low {self.to.size} bytes"
        return f"{self.kind.value} ({self.to.size} bytes)"


def _f32(x: float) -> float:
    """``x`` rounded to a 4-byte decimal; too big gives infinity, as the CPU does."""
    try:
        return struct.unpack("<f", struct.pack("<f", x))[0]
    except OverflowError:
        return math.copysign(math.inf, x)


def _fit(x: float, to: ValueType) -> float | int:
    if to is ValueType.FLOAT:
        return _f32(float(x))
    if to is ValueType.DOUBLE:
        return float(x)
    return _wrap(int(x), to)


def _wrap(x: int, to: ValueType) -> int:
    """Two's complement wrap into ``to`` (signed), as a register of that size holds it."""
    bits = 8 * to.size
    x &= (1 << bits) - 1
    return x - (1 << bits) if x >> (bits - 1) else x


def _div(a: float, b: float) -> float:
    if b == 0:
        return (
            math.nan
            if a == 0 or math.isnan(a)
            else math.copysign(math.inf, a) * (math.copysign(1.0, b))
        )
    return a / b


def _to_whole(x: float, to: ValueType, rounding: Rounding | None) -> int:
    """cvt(t)ss2si: NaN or out of range gives the "indefinite" value (the lowest)."""
    lo, hi = to.int_range()
    if not math.isfinite(x):
        return lo
    n = math.trunc(x) if rounding == "cut" else round(x)  # round: halves to even
    return n if lo <= n <= hi else lo


def _next(x: float | int, vtype: ValueType, up: bool) -> float | int:
    if not vtype.is_float:
        return x + (1 if up else -1)
    if vtype is ValueType.DOUBLE:
        return math.nextafter(x, math.inf if up else -math.inf)
    if x == 0:
        tiny = struct.unpack("<f", struct.pack("<I", 1))[0]
        return tiny if up else -tiny
    (bits,) = struct.unpack("<i", struct.pack("<f", x))
    bits += 1 if (x > 0) == up else -1
    return struct.unpack("<f", struct.pack("<i", bits))[0]


@dataclass(frozen=True)
class Way:
    """How the real value becomes the shown one: screen = steps(real)."""

    real: ValueType  # how the source keeps it
    shown: ValueType  # the watched copy's type
    steps: tuple[Step, ...] = ()  # in the game's order, real -> shown

    def __post_init__(self) -> None:
        last = self.steps[-1].to if self.steps else self.real
        if last is not self.shown:
            raise ValueError(f"the steps end in {last.name}, the copy is {self.shown.name}")

    # -- composing ---------------------------------------------------------------
    def then(self, outer: Way) -> Way:
        """This hop (source -> copy) followed by ``outer`` (copy -> screen)."""
        if outer.real is not self.shown:
            raise ValueError(f"can't follow {self.shown.name} with a way from {outer.real.name}")
        return Way(self.real, outer.shown, self.steps + outer.steps)

    @property
    def max_addresses(self) -> tuple[int, ...]:
        return tuple(s.max_at for s in self.steps if s.kind is Kind.SHARE and s.max_at is not None)

    @property
    def has_share(self) -> bool:
        return any(s.kind is Kind.SHARE for s in self.steps)

    # -- maths -------------------------------------------------------------------
    @staticmethod
    def _max(step: Step, max_now: MaxNow) -> float:
        if isinstance(max_now, Mapping):
            m = max_now.get(step.max_at) if step.max_at is not None else None
        else:
            m = max_now
        if m is None:
            raise ValueError("this way divides by a max: read the max first")
        return float(m)

    def factor(self, max_now: MaxNow = None) -> float:
        """screen = real x factor (conversions count as 1)."""
        f = 1.0
        for s in self.steps:
            if s.kind is Kind.MUL:
                f *= float(s.const or 0)
            elif s.kind is Kind.DIV:
                f = _div(f, float(s.const or 0))
            elif s.kind is Kind.SHARE:
                f = _div(f, self._max(s, max_now))
        return f

    def to_screen(self, real: float | int, max_now: MaxNow = None) -> float | int:
        """What the game shows for ``real``, step by step as its CPU works it out."""
        x: float | int = real
        t = self.real
        for s in self.steps:
            x = self._apply(s, x, t, max_now)
            t = s.to
        return x

    def _apply(self, s: Step, x: float | int, t: ValueType, max_now: MaxNow) -> float | int:
        k = s.kind
        if k is Kind.EXTEND:
            n = int(x)
            if not s.signed and n < 0:
                n += 1 << (8 * t.size)
            return _wrap(n, s.to)
        if k is Kind.TRUNCATE:  # wraps going forward; going back the value is kept as is
            return _wrap(int(x), s.to)
        if k is Kind.TO_WHOLE:
            return _to_whole(float(x), s.to, s.rounding)
        if k in (Kind.TO_DECIMAL, Kind.WIDEN, Kind.NARROW):
            return _fit(float(x), s.to)
        if k is Kind.MUL:
            if s.to.is_float:
                return _fit(float(x) * float(s.const or 0), s.to)
            return _wrap(int(x) * int(s.const or 0), s.to)
        if k is Kind.DIV:
            if s.to.is_float:
                return _fit(_div(float(x), float(s.const or 0)), s.to)
            return _wrap(int(x / (s.const or 1)), s.to)
        return _fit(_div(float(x), self._max(s, max_now)), s.to)  # SHARE

    def _same_shown(self, a: float | int, b: float | int) -> bool:
        try:
            return self.shown.encode(a) == self.shown.encode(b)
        except (ValueError, OverflowError):
            return False

    def to_real(self, screen: float | int, max_now: MaxNow = None) -> float | int:
        """The real value to write so the game shows ``screen``: the inverse of the
        factor, then nudged by up to NUDGE_STEPS float32 (or whole) steps until the
        forward maths gives ``screen`` exactly (a typed 29 -> 0.29f shows 28 when the
        game cuts 0.29f x 100). With no exact one, the nearest guess."""
        f = self.factor(max_now)
        if f == 0 or not math.isfinite(f):
            raise ValueError("this way can't be turned back into the real value")
        guess = float(screen) / f
        if self.real.is_float:
            x0: float | int = _fit(guess, self.real)
        else:
            lo, hi = self.real.int_range()
            x0 = max(lo, min(hi, round(guess))) if math.isfinite(guess) else 0
        if self._same_shown(self.to_screen(x0, max_now), screen):
            return x0
        up = down = x0
        for _ in range(NUDGE_STEPS):
            up, down = _next(up, self.real, True), _next(down, self.real, False)
            for x in (up, down):
                if self._fits(x) and self._same_shown(self.to_screen(x, max_now), screen):
                    return x
        return x0

    def _fits(self, x: float | int) -> bool:
        try:
            self.real.encode(x)
        except (ValueError, OverflowError):
            return False
        return True

    @property
    def rounding(self) -> Literal["exact", "cut", "round", "decimal"]:
        """How a candidate is compared with the copy (AC-T5)."""
        cuts = [s.rounding for s in self.steps if s.kind is Kind.TO_WHOLE]
        if cuts:
            return cuts[-1] or "round"
        if self.real.is_float or self.shown.is_float or any(s.to.is_float for s in self.steps):
            return "decimal"
        return "exact"

    def matches(
        self, real: float | int, shown: float | int, max_now: MaxNow = None, unit: float = 1.0
    ) -> bool:
        """AC-T5: does a source holding ``real`` explain a copy holding ``shown``?

        Whole numbers exact; a cut conversion floor(x) == shown (toward zero, as
        ``cvtt*`` does); a rounded one |x - shown| <= 0.5; decimals within 1% or
        ``unit`` (one unit of the shown number's last digit), whichever is larger."""
        if isinstance(real, float) and not math.isfinite(real):
            return False
        if isinstance(shown, float) and not math.isfinite(shown):
            return False
        try:
            screen = self.to_screen(real, max_now)
            ideal = float(real) * self.factor(max_now)  # the same sum without rounding
        except (ValueError, OverflowError):
            return False
        mode = self.rounding
        if mode == "exact":
            return screen == shown
        if mode == "cut":
            return screen == shown or (math.isfinite(ideal) and math.trunc(ideal) == shown)
        if mode == "round":
            return math.isfinite(ideal) and abs(ideal - float(shown)) <= 0.5
        if not math.isfinite(float(screen)):
            return False
        return abs(float(screen) - float(shown)) <= max(DECIMAL_TOL * abs(float(shown)), unit)

    # -- words -------------------------------------------------------------------
    def label(self, max_now: MaxNow = None) -> str:
        """Stored as: "as shown", "x0.01 of the screen", "share of max"."""
        if self.has_share:
            return "share of max"
        f = self.factor(max_now)
        return "as shown" if f == 1 else f"x{1 / f:g} of the screen"

    def screen_text(self, max_now: MaxNow = None) -> str:
        """How the screen shows it: "as is", "x100", "as a share of a max"."""
        if self.has_share:
            return "as a share of a max"
        f = self.factor(max_now)
        return "as is" if f == 1 else f"x{f:g}"

    def as_scale(self, max_now: MaxNow = None) -> Scale:
        """For Search for it that way: stored = shown x (1 / factor)."""
        f = self.factor(max_now)
        if f == 0 or not math.isfinite(f):
            raise ValueError("this way has no factor to search with")
        return Scale(f"traced x{1 / f:g}", 1 / f, decimal=self.real.is_float)

    def text(self) -> str:
        """For the log: "FLOAT -> x100 -> cut to whole -> INT32"."""
        return " -> ".join([self.real.name, *(s.text() for s in self.steps), self.shown.name])


def as_shown(vtype: ValueType) -> Way:
    """The copy holds the real value unchanged."""
    return Way(vtype, vtype)

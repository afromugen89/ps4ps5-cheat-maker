"""Percent checks done on the PC (spec-health-finding AC-P1 to AC-P3).

Pure logic, no console. PS5Debug-NG has no "percent of the value it had before"
compare, so once few enough matches are left the scanner reads them and keeps the ones
that hold the percent the player typed, in any of these ways:

* percent: the number itself (40 for 40%)
* fraction: 0.4 (decimals only)
* x10: 400
* of full: 40% of the match's value when the player said it was full

Each kept match is tagged with the way it matched, so it is compared that way next time.
Works the same for every game.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cheatmaker.core.values import (
    FRACTION,
    OF_FULL,
    PERCENT,
    X10,
    Scale,
    ValueType,
    open_window,
    subnormal,
    window,
)

FULL_TOL = 1.0
"""A typed percent keeps 39-41% of full for 40% (percentage points)."""
ABOUT_TOL = 12.5
"""About 75 / 50 / 25%: half the gap between two buttons, in percentage points."""


@dataclass(frozen=True)
class Target:
    """What the game shows now: ``value`` percent, typed to ``unit`` (AC-H2).
    ``about`` is a guess from a bar (AC-P3): wider, and only fraction / of full."""

    value: float | int
    unit: float
    about: bool = False


@dataclass(frozen=True)
class Full:
    """A match's value when full, and how far off that may be (relative)."""

    value: float
    rel_err: float = 0.0


def full_estimate(prev: float, last_pct: float | None, last_unit: float) -> Full | None:
    """The value when full from the value at a scan where the game showed ``last_pct``
    (typed to ``last_unit``). None when that percent isn't known or is 0."""
    if last_pct is None or last_pct <= 0 or not math.isfinite(prev):
        return None
    return Full(prev * 100.0 / last_pct, last_unit / last_pct)


def ways_to_try(vtype: ValueType, way: Scale | None, target: Target) -> list[Scale]:
    """A match tied to a way is checked that way only; others every way that fits."""
    if way is not None:
        return [way]
    if target.about:
        return [FRACTION, OF_FULL] if vtype.is_float else [OF_FULL]
    ways = [PERCENT, FRACTION, X10, OF_FULL]
    return ways if vtype.is_float else [w for w in ways if not w.decimal]


def matches_way(
    cur: float | int,
    way: Scale,
    full: Full | None,
    target: Target,
    vtype: ValueType = ValueType.DOUBLE,
) -> bool:
    """``cur`` (a ``vtype`` value) holds the target ``way``. Windows one unit wide are
    compared as ``vtype`` stores numbers (see ``values.open_window``)."""
    if isinstance(cur, float) and not math.isfinite(cur):
        return False
    if way == OF_FULL:
        if full is None or full.value == 0 or not math.isfinite(full.value):
            return False  # AC-P2: 0 at Full never matches
        if subnormal(vtype, cur) or subnormal(vtype, full.value):
            return False  # a whole number's bytes read as a decimal: scales the same way
        tol = (ABOUT_TOL if target.about else FULL_TOL) + target.value * full.rel_err
        return abs(cur * 100.0 / full.value - target.value) <= tol
    if target.about:
        lo, hi = target.value - ABOUT_TOL, target.value + ABOUT_TOL
        return lo * way.factor <= cur <= hi * way.factor
    lo, hi = open_window(vtype, window(target.value * way.factor, target.unit * way.factor))
    return lo <= cur <= hi


def fitting(
    vtype: ValueType, way: Scale | None, cur: float | int, full: Full | None, target: Target
) -> list[Scale]:
    """Every way ``cur`` holds the target percent (empty: it doesn't)."""
    return [w for w in ways_to_try(vtype, way, target) if matches_way(cur, w, full, target, vtype)]


def check(
    vtype: ValueType, way: Scale | None, cur: float | int, full: Full | None, target: Target
) -> Scale | None:
    """The first way ``cur`` holds the target percent, or None (a yes / no check; to
    tie a match to a way use ``tie``)."""
    ways = fitting(vtype, way, cur, full, target)
    return ways[0] if ways else None


def agrees(way: Scale, full: Full) -> bool:
    """A fixed way (percent, fraction, x10) fits a match whose value when full is
    ``full`` only if that is 100% that way (100, 1.0, 1000), give or take a point and
    how far off the estimate may be."""
    want = 100.0 * way.factor
    return abs(full.value - want) <= want * (FULL_TOL / 100.0 + full.rel_err)


def tie(ways: list[Scale], full: Full | None = None) -> Scale | None:
    """The way to tie a match to, given every way it fits (not empty).

    With a known value when full (a Full search), the first way that agrees with it:
    0 at 0% fits every way, but a shield whose full is 1,700 is kept "of full", not as
    a percent (whose full is 100). Otherwise, or when no way agrees: the only way it
    fits, or None when it fits several (1.2 is 1% and 0.12% x10), so every way is
    tried again next time until one is left."""
    if full is not None and full.value and math.isfinite(full.value):
        ok = [w for w in ways if w == OF_FULL or agrees(w, full)]
        if ok:
            return ok[0]
    return ways[0] if len(ways) == 1 else None

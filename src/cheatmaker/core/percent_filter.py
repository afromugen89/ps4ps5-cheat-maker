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

from cheatmaker.core.values import FRACTION, OF_FULL, PERCENT, X10, Scale, ValueType, window

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


def matches_way(cur: float | int, way: Scale, full: Full | None, target: Target) -> bool:
    if isinstance(cur, float) and not math.isfinite(cur):
        return False
    if way == OF_FULL:
        if full is None or full.value == 0 or not math.isfinite(full.value):
            return False  # AC-P2: 0 at Full never matches
        tol = (ABOUT_TOL if target.about else FULL_TOL) + target.value * full.rel_err
        return abs(cur * 100.0 / full.value - target.value) <= tol
    if target.about:
        lo, hi = target.value - ABOUT_TOL, target.value + ABOUT_TOL
        return lo * way.factor <= cur <= hi * way.factor
    lo, hi = window(target.value * way.factor, target.unit * way.factor)
    return lo <= cur <= hi


def check(
    vtype: ValueType, way: Scale | None, cur: float | int, full: Full | None, target: Target
) -> Scale | None:
    """The way ``cur`` holds the target percent, or None."""
    return next(
        (w for w in ways_to_try(vtype, way, target) if matches_way(cur, w, full, target)), None
    )

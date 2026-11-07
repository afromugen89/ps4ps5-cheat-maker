"""Group linked copies of a value (spec-health-finding AC-G1 to AC-G4).

Pure logic, no console. A game often keeps one value several ways (health 40, its
fraction 0.4, health / 3) and the HUD or menus copy it. Over the scans, linked matches
change together: one is always the other times the same number. Those become one
group, shown as one row.

Within a group a match is "likely real" when something else that changed with it sits
close by (within 64 bytes) at another scale, e.g. health next to its fraction: games
keep related numbers of one object together, display copies sit on their own. It is
a hint only; nothing is removed. Works the same for every game.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from cheatmaker.core.values import Scale, ValueType, subnormal

RATIO_TOL = 0.01  # linked when one is the other x the same number, within 1%
NEAR = 64  # bytes: same object
FAR = 65536  # bytes: copies further apart than this are probably display copies
FLOAT_SLACK = 1e-6  # 4-byte decimals keep about 7 digits

LIKELY = "likely real"
COPIES = "copies (probably display)"
STILL = "didn't change"

Key = tuple[int, ValueType]  # (address, number type)
Values = list[float | None]  # one entry per recorded scan, None when not known


@dataclass(frozen=True)
class Member:
    address: int
    vtype: ValueType
    way: Scale | None
    values: tuple[float | None, ...] = field(hash=False)

    @property
    def changed(self) -> bool:
        return len({v for v in self.values if v is not None}) > 1

    @property
    def last(self) -> float | None:
        return next((v for v in reversed(self.values) if v is not None), None)


@dataclass
class Group:
    members: list[Member]  # best first: the head
    likely: set[int] = field(default_factory=set)  # indexes into members
    factors: list[float] = field(default_factory=list)  # member = head x factor
    label: str = ""

    @property
    def head(self) -> Member:
        return self.members[0]

    @property
    def copies(self) -> int:
        return len(self.members) - 1

    @property
    def head_likely(self) -> bool:
        return 0 in self.likely


def _shared(a: Member, b: Member) -> list[tuple[float, float]]:
    return [
        (x, y) for x, y in zip(a.values, b.values, strict=False) if x is not None and y is not None
    ]


def first_ratio(a: Member, b: Member) -> float | None:
    """b / a at the first scan where both are known and not 0."""
    return next((y / x for x, y in _shared(a, b) if x != 0 and y != 0), None)


MIN_CHANGES = 2
"""Scans in which both must have changed to be linked. With one change any two values
that went down are "x the same number" of each other."""


def link_ratio(a: Member, b: Member, tol: float = RATIO_TOL) -> float | None:
    """k when b is always a x k (within ``tol``) and they changed together at least
    MIN_CHANGES times; else None. A 0 on one side only breaks the link."""
    if not (a.changed and b.changed):
        return None
    shared = _shared(a, b)
    if sum(1 for p, q in itertools.pairwise(shared) if p[0] != q[0] and p[1] != q[1]) < MIN_CHANGES:
        return None
    k = first_ratio(a, b)
    if k is None:
        return None
    for x, y in shared:
        if x == 0 or y == 0:
            if (x == 0) != (y == 0):
                return None
            continue
        if abs(y / x - k) > tol * abs(k):
            return None
    # The changes must scale by k too: 900 -> 880 and 905 -> 870 stay within 1% of each
    # other but don't move together. Float rounding gets a little slack.
    for (x0, y0), (x1, y1) in itertools.pairwise(shared):
        want, got = (x1 - x0) * k, y1 - y0
        slack = tol * max(abs(want), abs(got)) + FLOAT_SLACK * (abs(y0) + abs(y1))
        if abs(got - want) > slack:
            return None
    return k


def _moves(m: Member) -> tuple[bool | None, ...]:
    """Per scan after the first: did it change (None when not known)."""
    v = m.values
    return tuple(
        None if v[i] is None or v[i - 1] is None else v[i] != v[i - 1] for i in range(1, len(v))
    )


def _same_moves(a: Member, b: Member) -> bool:
    pairs = [(x, y) for x, y in zip(_moves(a), _moves(b), strict=False) if None not in (x, y)]
    return bool(pairs) and any(x for x, _ in pairs) and all(x == y for x, y in pairs)


def _scaled(k: float | None, tol: float) -> bool:
    """A factor that is clearly not 1 (another scale, not a copy)."""
    return k is not None and abs(k - 1) > tol


def _noise(m: Member) -> bool:
    """A decimal that only ever held subnormal numbers: another kind of data (a whole
    number's bytes) read as a decimal. Left out of the groups, so the whole number it
    shadows is the one a group row acts on."""
    nonzero = [v for v in m.values if v]
    return bool(nonzero) and all(subnormal(m.vtype, v) for v in nonzero)


def _overlap(a: Member, b: Member) -> bool:
    lo, hi = (a, b) if a.address <= b.address else (b, a)
    return hi.address < lo.address + lo.vtype.size


def group_matches(
    history: Mapping[Key, Values],
    tags: Mapping[Key, Scale | None] | None = None,
    ratio_tol: float = RATIO_TOL,
    near: int = NEAR,
    far: int = FAR,
) -> list[Group]:
    """Groups of linked matches, best first (AC-G2 to AC-G4).

    ``history`` holds each match's value at every recorded scan (None when unknown),
    ``tags`` how it keeps the number. Decimals that only held subnormal numbers (another
    type's bytes, see ``_noise``) are left out. Order: groups with a likely real match first, then
    smaller groups; matches that never changed come last, each on its own."""
    tags = tags or {}
    every = [
        Member(a, vt, tags.get((a, vt)), tuple(vals))
        for (a, vt), vals in sorted(history.items(), key=lambda kv: kv[0][0])
    ]
    every = [m for m in every if not _noise(m)]
    moving = [m for m in every if m.changed]
    still = [m for m in every if not m.changed]
    n = len(moving)
    # Linked values change in the same scans, so only those need comparing. A match
    # joins a group only when it links to that group's first member: no chains of
    # "close enough" links that end far from where they started.
    roots = list(range(n))
    buckets: dict[tuple, list[int]] = {}
    for i, m in enumerate(moving):
        buckets.setdefault(_moves(m), []).append(i)
    # One place in memory is never in a group twice (a value read as two types).
    for idx in buckets.values():
        firsts: dict[int, list[int]] = {}  # a group's first member -> its members
        for i in idx:
            m = moving[i]
            j = next(
                (
                    f
                    for f, own in firsts.items()
                    if not any(_overlap(moving[k], m) for k in own)
                    and link_ratio(moving[f], m, ratio_tol)
                ),
                None,
            )
            if j is None:
                firsts[i] = [i]
            else:
                roots[i] = j
                firsts[j].append(i)

    # Likely real: something that changed with it sits close by at another scale.
    likely = [False] * n
    neighbours = [0] * n  # in-group matches close by
    for i in range(n):  # moving is in address order
        for j in range(i + 1, n):
            a, b = moving[i], moving[j]
            if b.address - a.address > near:
                break
            if _overlap(a, b):
                continue  # one value read as two types, not two values
            if roots[i] == roots[j]:
                neighbours[i] += 1
                neighbours[j] += 1
                hit = _scaled(first_ratio(a, b), ratio_tol)
            else:
                hit = _same_moves(a, b) and _scaled(first_ratio(a, b), ratio_tol)
            if hit:
                likely[i] = likely[j] = True

    by_root: dict[int, list[int]] = {}
    for i, r in enumerate(roots):
        by_root.setdefault(r, []).append(i)
    groups = []
    for idx in by_root.values():
        idx.sort(key=lambda i: (not likely[i], -neighbours[i], moving[i].address))
        members = [moving[i] for i in idx]
        head = members[0]
        g = Group(
            members,
            {k for k, i in enumerate(idx) if likely[i]},
            [1.0 if m is head else (first_ratio(head, m) or math.nan) for m in members],
        )
        if g.likely:
            g.label = LIKELY
        elif len(members) > 1 and _display_copies(members, ratio_tol, far):
            g.label = COPIES
        groups.append(g)
    groups.sort(key=lambda g: (not g.likely, len(g.members), g.head.address))
    return groups + [Group([m], set(), [1.0], STILL) for m in still]


def _display_copies(members: list[Member], tol: float, far: int) -> bool:
    """All the same value, and all far apart."""
    head = members[0]
    if any(_scaled(first_ratio(head, m), tol) for m in members[1:]):
        return False
    addrs = sorted(m.address for m in members)
    return all(b - a > far for a, b in itertools.pairwise(addrs))

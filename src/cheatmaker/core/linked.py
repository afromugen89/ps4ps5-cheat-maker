"""Linked values in Make it work (docs/spec-linked-values.md, design in
docs/architecture-linked-values.md).

Pure logic, no console. A game may keep one value in several places at once (Returnal:
health / 100 and the health fraction, in different objects). Make it work fixes place 1;
the other members of its Find group (the pool) are watched during the test, and a member
that falls behind while place 1 holds is a second place the game keeps.

- ``from_group`` / ``pool_for``: the pool from a Find group, factors based on place 1.
- ``check_start``: at Start, drop members that aren't linked any more.
- ``plan_spans``: which members are read together (4 KiB reads).
- ``judge_member``: kept up, fell behind, or unknown over one test.
- ``clusters`` / ``pick``: which fell-behind member to fix next.
- ``scaled_full``: a linked place's Full for Always full.

Works the same for every game: nothing here knows a game, a name or an offset.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from cheatmaker.core import real_check as rc
from cheatmaker.core.grouping import Group
from cheatmaker.core.values import ValueType

POOL_MAX = 16  # L2: members not marked copy
MAX_PLACES = 3  # L10
MAX_LINKED_FIXES = 4  # L10: fixes tried per linked place
MAX_TESTS = 12  # L10: tests per build, place 1 and repeats included
LINK_TOL = 0.02  # L2 start check, falls behind, clusters
POOL_EVERY_S = 0.75  # each span is read again this long after its last read ...
POOL_EARLY_S = 0.1  # ... or up to this much sooner, at the tick before (ticks don't
# fall on due times: with 36 ms reads a tick is ~0.36 s, so a span is read every 2 ticks,
# 0.72 s, not every 3, 1.07 s; with no latency every 3 ticks, 0.75 s)
ONCE_A_SECOND_S = 1.0  # L3: a span read later than this after its last read is late
NEXT_TICK_SLACK_S = 0.1  # a span that would pass its once-a-second mark before the next
# tick's reads (expected this much after its start) is read first
POOL_SAMPLE_SHARE = 0.05  # a tick's pool reads fit in its wait plus this share of a tick
# (L3 allows the value 10% fewer readings; B5 comes first, so pool reads go late instead)
MAX_SPANS = 16  # spans read in one test; copy-only spans beyond this aren't watched
BEHIND_S = 0.75  # "at least 1 s in a row": behind readings in a row spanning this long
UNKNOWN_END_S = 2.0  # no readable reading in the last 2 s of the test: unknown
PAIR_S = 0.5  # clusters: readings of two members this close in time are compared
INT_START_SLACK = 1  # L2: a whole number may be off by 1 (rounding) at Start
FLOAT_START_SLACK = 1e-6

NOT_LINKED = "not linked any more"
NOT_READ = "couldn't be read"
BAD_FACTOR = "factor"
OVERLAPS = "shares bytes with another place"


@dataclass(frozen=True)
class PoolMember:
    address: int
    vtype: ValueType
    factor: float  # member = reference x factor (group head, or place 1 once in a pool)
    likely: bool = False  # grouping's "likely real"
    mark: rc.Verdict | None = None  # Check which is real; None = not checked

    @property
    def key(self) -> rc.Key:
        return (self.address, self.vtype)

    @property
    def target(self) -> rc.Target:
        return rc.Target(self.address, self.vtype)


@dataclass(frozen=True)
class LinkedGroup:
    """An in-memory snapshot of a Find group: never saved."""

    members: tuple[PoolMember, ...]  # group order (best first), factors vs the group head


class Status(Enum):
    KEPT_UP = "kept up"
    BEHIND = "fell behind"
    UNKNOWN = "couldn't be read"
    NOT_WATCHED = "not watched"


@dataclass(frozen=True)
class Reading:
    t: float
    ref: float | int | None  # place 1's value at that moment
    value: float | int | None  # the member's value; None when it couldn't be read


@dataclass(frozen=True)
class Judged:
    member: PoolMember
    status: Status
    worst: float | int | None = None  # the member's value where it was furthest behind


class Done(Enum):
    """What the build did with a pool member (L15); the words are in ui/hints."""

    NOTHING = "nothing"
    FIXED = "fixed"
    FROZEN = "held by a freeze"
    NOT_NEEDED = "not needed"  # it kept up after a later place was fixed
    COPY = "copy"  # never touched
    NOT_CHECKED = "couldn't be checked"
    NOT_FIXED = "not fixed"  # still behind when the build ended (L11)


@dataclass
class PoolRow:
    """One Advanced line (L15), updated after every judge."""

    member: PoolMember
    status: Status
    done: Done = Done.NOTHING
    after_place: int = 0  # NOT_NEEDED: the place whose fix made it keep up
    reason: str = ""  # NOT_FIXED / FROZEN: why it wasn't fixed in code (L11)


@dataclass
class PoolData:
    """What the pool watch saw during one test."""

    readings: dict[rc.Key, list[Reading]] = field(default_factory=dict)
    end_t: float = 0.0
    not_watched: list[PoolMember] = field(default_factory=list)
    spans: int = 0
    reads: int = 0
    late: int = 0  # reads more than ONCE_A_SECOND_S after the span's last read
    read_s: float = 0.0  # time spent reading the pool


# -- the pool -------------------------------------------------------------------------
def from_group(group: Group, verdicts: Mapping[rc.Key, rc.Result]) -> LinkedGroup | None:
    """The snapshot of a Find group with two or more members, with their marks."""
    if len(group.members) < 2:
        return None
    out = []
    for i, m in enumerate(group.members):
        factor = group.factors[i] if i < len(group.factors) else math.nan
        r = verdicts.get((m.address, m.vtype))
        out.append(
            PoolMember(
                m.address,
                m.vtype,
                float(factor),
                i in group.likely,
                None if r is None else r.verdict,
            )
        )
    return LinkedGroup(tuple(out))


def _overlap(a: rc.Target | PoolMember, b: rc.Target | PoolMember) -> bool:
    return a.address < b.address + b.vtype.size and b.address < a.address + a.vtype.size


def pool_for(
    linked: LinkedGroup, place1: rc.Key, marks: Mapping[rc.Key, rc.Result]
) -> tuple[list[PoolMember], list[PoolMember]]:
    """(watched, copies) for place 1, factors re-based on it. ``marks`` (fresher, from
    Check which is real in B1) overlay the snapshot's. Members sharing a byte with
    place 1 or with a member already taken, and factors that aren't positive numbers,
    are left out; at most POOL_MAX watched, in group order. Empty when place 1 isn't
    in the group."""
    head = next((m for m in linked.members if m.key == place1), None)
    if head is None:
        return [], []
    f1 = head.factor
    taken: list[rc.Target | PoolMember] = [head.target]
    watched: list[PoolMember] = []
    copies: list[PoolMember] = []
    for m in linked.members:
        if m is head:
            continue
        r = marks.get(m.key)
        mark = m.mark if r is None else r.verdict
        if any(_overlap(m, t) for t in taken):
            continue
        k = m.factor / f1 if f1 and math.isfinite(f1) else math.nan
        if not math.isfinite(k) or k <= 0:
            continue
        if mark is not rc.Verdict.COPY and len(watched) >= POOL_MAX:
            continue
        m = replace(m, factor=k, mark=mark)
        taken.append(m)
        (copies if mark is rc.Verdict.COPY else watched).append(m)
    return watched, copies


def _finite(v: float | int | None) -> bool:
    return v is not None and not (isinstance(v, float) and not math.isfinite(v))


def check_start(
    place1: rc.Target,
    v1: float | int | None,
    pool: Sequence[PoolMember],
    values: Mapping[rc.Key, float | int | None],
) -> tuple[list[PoolMember], list[tuple[PoolMember, str]]]:
    """(kept, dropped with the reason) at Start: a member must still be k x place 1's
    value (within 2%, or 1 for whole numbers) or the game moved on since the search.
    Kept members get the factor read now (``v / v1``; the group's when ``v1`` is 0)."""
    kept: list[PoolMember] = []
    dropped: list[tuple[PoolMember, str]] = []
    for m in pool:
        v = values.get(m.key)
        if v is None:
            dropped.append((m, NOT_READ))
            continue
        if not (_finite(v) and _finite(v1)):
            dropped.append((m, NOT_LINKED))
            continue
        assert v1 is not None
        e = m.factor * v1
        slack = max(LINK_TOL * abs(e), FLOAT_START_SLACK if m.vtype.is_float else INT_START_SLACK)
        if abs(v - e) > slack:
            dropped.append((m, NOT_LINKED))
            continue
        k = v / v1 if v1 != 0 else m.factor
        if not (math.isfinite(k) and k > 0):
            dropped.append((m, BAD_FACTOR))
            continue
        kept.append(replace(m, factor=k))
    return kept, dropped


def plan_spans(
    watched: Sequence[PoolMember],
    copies: Sequence[PoolMember] = (),
    ref: rc.Target | None = None,
    max_spans: int = MAX_SPANS,
) -> list[list[rc.Target]]:
    """The reads of one test: 4 KiB spans holding the watched members (and ``ref``,
    place 1, when it is read with them), then spans holding only copies while there
    are fewer than ``max_spans``. Copies left out are "not watched"."""
    must = {m.key for m in watched}
    if ref is not None:
        must.add((ref.address, ref.vtype))
    every = [m.target for m in (*watched, *copies)] + ([ref] if ref is not None else [])
    cut = rc.spans(every)
    needed = [s for s in cut if any((t.address, t.vtype) in must for t in s)]
    extra = [s for s in cut if s not in needed]
    return needed + extra[: max(0, max_spans - len(needed))]


# -- judging one test (L3) ------------------------------------------------------------
def _usable(r: Reading) -> bool:
    return _finite(r.value) and _finite(r.ref)


def _expected(m: PoolMember, r: Reading) -> float:
    assert r.ref is not None
    return m.factor * r.ref


def _gap(m: PoolMember, r: Reading) -> float:
    assert r.value is not None
    return _expected(m, r) - r.value


def _behind_at(m: PoolMember, r: Reading) -> bool:
    gap = _gap(m, r)
    if m.vtype.is_float:
        return gap > LINK_TOL * abs(_expected(m, r))
    return gap > 1


def judge_member(m: PoolMember, readings: Sequence[Reading], end_t: float) -> Judged:
    """Kept up or fell behind over one test, or unknown (too few readings, or none in
    its last 2 s). Behind: the last readable reading is behind, or readings in a row
    are, from the first to the last at least BEHIND_S apart by their times (an
    unreadable reading breaks the run). Expected = factor x place 1's value then; behind
    means lower than that by more than 2% (decimals) or more than 1 (whole numbers)."""
    usable = [r for r in readings if _usable(r)]
    worst = max(usable, key=lambda r: _gap(m, r)).value if usable else None
    if len(usable) < 2 or not any(r.t >= end_t - UNKNOWN_END_S for r in usable):
        return Judged(m, Status.UNKNOWN, worst)
    if _behind_at(m, usable[-1]):
        return Judged(m, Status.BEHIND, worst)
    since: float | None = None  # when the current run of behind readings began
    for r in readings:
        if not (_usable(r) and _behind_at(m, r)):
            since = None
            continue
        since = r.t if since is None else since
        if r.t - since >= BEHIND_S:
            return Judged(m, Status.BEHIND, worst)
    return Judged(m, Status.KEPT_UP, worst)


def judge_later(m: PoolMember, readings: Sequence[Reading], end_t: float) -> Judged:
    """``judge_member`` for the test of a later place (L5: judged again with every fix
    in). A member that fell behind in an earlier test starts this one already lower
    than factor x place 1, so only what it loses against place 1 during this test
    counts: its readings are shifted so the first readable one is on the line. ``worst``
    is the member's own value."""
    first = next((r for r in readings if _usable(r)), None)
    if first is None:
        return judge_member(m, readings, end_t)
    assert first.value is not None
    shift = _expected(m, first) - first.value
    moved = [
        Reading(r.t, r.ref, None if not _finite(r.value) else r.value + shift)  # type: ignore[operator]
        for r in readings
    ]
    j = judge_member(m, moved, end_t)
    return Judged(m, j.status, None if j.worst is None else j.worst - shift)


# -- which place next (L5) -------------------------------------------------------------
def _steady(rs: Sequence[Reading], t1: float, t2: float) -> bool:
    """The member's value can't have changed from ``t1`` to ``t2``: a reading at or
    before ``t1`` and one at or after ``t2`` hold the same value, as does every reading
    between them. A reading taken across a hit fails this, as does the test's last
    reading when its pair is later (nothing after it says it didn't change)."""
    ok = [r for r in rs if _finite(r.value)]
    lo = next((r for r in reversed(ok) if r.t <= t1), None)
    hi = next((r for r in ok if r.t >= t2), None)
    if lo is None or hi is None:
        return False
    return all(r.value == lo.value for r in ok if lo.t <= r.t <= hi.t)


def _same_ratio(a: Sequence[Reading], b: Sequence[Reading]) -> bool:
    """a / b stays the same (within 2% of the median ratio) over readings taken at
    about the same time. A pair is compared only when neither member can have changed
    between its two readings (members in other spans are read up to 0.5 s apart)."""
    rb_ok = [r for r in b if _finite(r.value)]
    ratios: list[float] = []
    for ra in a:
        if not _finite(ra.value):
            continue
        near = min(rb_ok, key=lambda rb: abs(rb.t - ra.t), default=None)
        if near is None or abs(near.t - ra.t) > PAIR_S:
            continue
        t1, t2 = sorted((ra.t, near.t))
        if not (_steady(a, t1, t2) and _steady(b, t1, t2)):
            continue
        x, y = ra.value, near.value
        assert x is not None and y is not None
        if x == 0 and y == 0:
            continue
        if x == 0 or y == 0:
            return False
        ratios.append(x / y)
    if not ratios:
        return False
    mid = statistics.median(ratios)
    return all(abs(r - mid) <= LINK_TOL * abs(mid) for r in ratios)


def clusters(
    behind: Sequence[PoolMember], readings: Mapping[rc.Key, Sequence[Reading]]
) -> list[list[PoolMember]]:
    """Members that fell behind, grouped by keeping the same ratio to each other in the
    test: one value the game keeps plus its copies. A member joins the first cluster
    whose first member it matches (no chains), else starts its own."""
    out: list[list[PoolMember]] = []
    for m in behind:
        mine = readings.get(m.key, ())
        home = next(
            (c for c in out if _same_ratio(readings.get(c[0].key, ()), mine)),
            None,
        )
        if home is None:
            out.append([m])
        else:
            home.append(m)
    return out


FIXABLE = (rc.Verdict.REAL, rc.Verdict.KEPT)


def _key(m: PoolMember, size: int) -> tuple:
    return (m.mark is not rc.Verdict.REAL, not m.likely, -size, m.address)


def pick(cs: Sequence[Sequence[PoolMember]]) -> list[tuple[PoolMember, list[PoolMember]]]:
    """(the member to fix, its cluster), best first. Only members marked real or kept
    are fixed; a cluster without one is left out (its members are copies or couldn't
    be checked). Order: real before kept, likely real first, bigger clusters, then the
    lowest address."""
    out = []
    for c in cs:
        ok = [m for m in c if m.mark in FIXABLE]
        if ok:
            rep = min(ok, key=lambda m: _key(m, len(c)))
            out.append((rep, list(c)))
    out.sort(key=lambda rc_: _key(rc_[0], len(rc_[1])))
    return out


def scaled_full(full1: float | int, k: float, vtype: ValueType) -> float | int:
    """L8: a linked place's Full is place 1's Full x k, as the game would store it."""
    x = full1 * k
    if vtype.is_float:
        return vtype.decode(vtype.encode(x))
    return vtype.clamp(round(x))


__all__ = [
    "MAX_LINKED_FIXES",
    "MAX_PLACES",
    "MAX_TESTS",
    "POOL_MAX",
    "Done",
    "Judged",
    "LinkedGroup",
    "PoolData",
    "PoolMember",
    "PoolRow",
    "Reading",
    "Status",
    "check_start",
    "clusters",
    "from_group",
    "judge_later",
    "judge_member",
    "pick",
    "plan_spans",
    "pool_for",
    "scaled_full",
]

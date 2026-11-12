"""Check which is real (spec-real-check AC-R1 to AC-R5): tell the game's own value from
its copies by writing a test value and watching what the game does with it.

Pure logic over a ConsoleClient, no UI. Works the same for every game:

- A copy (HUD, menu, a value worked out from another) is written by the game from the
  real one again and again, so a test value written to it is soon put back.
- The real value keeps a test value (nothing else writes it), and its linked copies
  follow it: they change to the test value times their usual factor.

One round writes one member of every group at once, waits a moment while the game
runs, then reads them all. Whatever still holds the test value gets its original bytes
back; what the game already put back is left as the game wrote it.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from cheatmaker.console.client import ConsoleClient
from cheatmaker.core.grouping import Group
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ReadFailed, WriteFailed

log = logging.getLogger(__name__)

SETTLE_S = 1.0
"""How long the game runs between the write and the read back. Games refresh copies
every frame (16-33 ms); a second leaves room for ones that refresh a few times a second."""
CHECK_LIMIT = 64  # addresses written in one check: ~64 x 36 ms writes plus the waits
ROUNDS_LIMIT = 8  # members tried per group (the best first)
LIST_LIMIT = 16  # rows tried when the list isn't grouped (one row per round)
FOLLOW_TOL = 0.02  # a follower is within 2% of test value x its factor
READ_SPAN = 0x1000  # addresses this close together are read in one request
TEST_STEP = 7  # whole numbers 0 or 1 go up by this much


class Verdict(Enum):
    REAL = "real: the game uses it"
    KEPT = "kept the test value"
    COPY = "copy: the game put it back"
    FAILED = "couldn't be checked"

    @property
    def short(self) -> str:
        return {
            Verdict.REAL: "checked: real",
            Verdict.KEPT: "checked: kept the test value",
            Verdict.COPY: "checked: copy",
            Verdict.FAILED: "not checked",
        }[self]


Key = tuple[int, ValueType]


@dataclass(frozen=True)
class Target:
    address: int
    vtype: ValueType


@dataclass(frozen=True)
class Result:
    verdict: Verdict
    followers: int = 0  # members of its group that changed to match it


def probe_value(vtype: ValueType, current: float | int) -> float | int:
    """A value the game would not write by itself: about three quarters of the current
    one (lower, so a game that caps a value at its maximum doesn't put it back). Whole
    numbers -1 to 1 and decimals 0, NaN and infinity go up a little instead."""
    if vtype.is_float:
        if not math.isfinite(current) or current == 0:
            return float(TEST_STEP)
        return float(current) * 0.75
    cur = int(current)
    if cur > 1 or cur < -1:
        v = cur * 3 // 4 if cur > 0 else -((-cur) * 3 // 4)
        return v if v != cur else cur - 1
    _lo, hi = vtype.int_range()
    return min(hi, cur + TEST_STEP)


def targets_from_groups(
    groups: Sequence[Group], skip: set[int] | None = None
) -> list[list[Target]]:
    """Each group's members to check, best first, at most ROUNDS_LIMIT each and
    CHECK_LIMIT in all. Matches that never changed (shown on their own, last) and
    addresses in ``skip`` (e.g. frozen by the app, which would put the test value back
    itself) are left out."""
    skip = skip or set()
    out: list[list[Target]] = []
    total = 0
    for g in groups:
        if not g.head.changed:
            continue
        members = [
            Target(m.address, m.vtype)
            for m in g.members
            if not any(m.address - 7 <= a < m.address + m.vtype.size for a in skip)
        ]
        members = members[:ROUNDS_LIMIT]
        members = members[: CHECK_LIMIT - total]
        if members:
            out.append(members)
            total += len(members)
        if total >= CHECK_LIMIT:
            break
    return out


def targets_from_list(
    rows: Sequence[tuple[int, ValueType]], skip: set[int] | None = None
) -> list[list[Target]]:
    """A list that isn't grouped: its rows are one group, tried one per round, so a row
    that changes with the one being tried shows up as its copy."""
    skip = skip or set()
    seen: set[Key] = set()
    out = []
    for addr, vt in rows:
        frozen = any(addr - 7 <= a < addr + vt.size for a in skip)
        if addr and not frozen and (addr, vt) not in seen:
            seen.add((addr, vt))
            out.append(Target(addr, vt))
    return [out[:LIST_LIMIT]] if out else []


@dataclass
class _Written:
    target: Target
    original: bytes
    test: bytes
    failed: bool = False  # the console refused the write (part may have landed)


def _overlap(a: Target, b: Target) -> bool:
    return a.address < b.address + b.vtype.size and b.address < a.address + a.vtype.size


def _follows(
    o: Target, before: bytes | None, after: bytes | None, old: float | int, test: float | int
) -> bool:
    """``o`` changed the way the tried value did: from x to about x * test / old (a copy
    keeps the same factor to the value it copies). When the tried value was 0 the factor
    isn't known, so a copy that was 0 and changed the same way (sign) counts."""
    if before is None or after is None or before == after:
        return False
    x, y = o.vtype.decode(before), o.vtype.decode(after)
    if not (math.isfinite(x) and math.isfinite(y)):
        return False
    if old == 0 or not math.isfinite(old):
        # A copy of 0 is 0 at any factor: only a 0 that changed can be following it.
        return x == 0 and y != 0 and (y > 0) == (test > 0)
    if x == 0:
        return False  # a copy of a value that isn't 0 isn't 0
    want = x * (test / old)
    slack = FOLLOW_TOL * abs(want) + (1e-6 if o.vtype.is_float else 1.0)
    return y != x and abs(y - want) <= slack


class RealChecker:
    """Run with ``run()`` on a worker thread; ``cancel`` stops it after the current
    round, with everything put back."""

    def __init__(
        self,
        client: ConsoleClient,
        pid: int,
        groups: list[list[Target]],
        settle_s: float | None = None,
        wait: Callable[[float], None] | None = None,
    ) -> None:
        self.client, self.pid = client, pid
        self.groups = [g for g in groups if g]
        if not self.groups:
            raise ValueError("Nothing to check: search until a few matches are listed.")
        self.settle_s = SETTLE_S if settle_s is None else settle_s
        self.wait = wait or time.sleep
        self.results: dict[Key, Result] = {}
        self.not_restored: list[int] = []  # addresses a lost link kept from being put back

    @property
    def rounds(self) -> int:
        return max(len(g) for g in self.groups)

    def run(
        self,
        progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
        rounds: int | None = None,
    ) -> dict[Key, Result]:
        """``rounds``: stop after that many rounds (None = every member). With 1, only
        each group's first member is written and the rest are its followers (the
        tracer checks one source against the copies it was found behind)."""
        if rounds is not None and rounds < 1:
            raise ValueError("rounds must be at least 1")
        total = self.rounds if rounds is None else min(rounds, self.rounds)
        for r in range(total):
            if cancel is not None and cancel.is_set():
                break
            self._round(r)
            if progress is not None:
                progress((r + 1) / total)
        log.info(
            "action=real_check groups=%d checked=%d real=%d copies=%d",
            len(self.groups),
            len(self.results),
            sum(1 for x in self.results.values() if x.verdict is Verdict.REAL),
            sum(1 for x in self.results.values() if x.verdict is Verdict.COPY),
        )
        return self.results

    def best(self, group: Sequence[Target]) -> Target | None:
        """The member found real in ``group`` (the one most others followed), if any."""
        real = [
            (self.results[(t.address, t.vtype)].followers, -i, t)
            for i, t in enumerate(group)
            if (t.address, t.vtype) in self.results
            and self.results[(t.address, t.vtype)].verdict is Verdict.REAL
        ]
        return max(real, key=lambda x: x[:2])[2] if real else None

    # -- one round ---------------------------------------------------------------
    def _round(self, r: int) -> None:
        tested = [(g, g[r]) for g in self.groups if r < len(g)]
        everyone = [t for g, _t in tested for t in g]
        before = self._read_all(everyone)
        written: list[tuple[list[Target], _Written]] = []
        try:
            for g, t in tested:
                key = (t.address, t.vtype)
                if key not in before or any(_overlap(t, w.target) for _g, w in written):
                    # gone, or the same bytes as another one this round (one place read
                    # as two types in two groups): writing both would mix them up
                    self.results[key] = Result(Verdict.FAILED)
                    continue
                # Read again right before the write: earlier writes this round took
                # time, and the game may have changed it since (picked up ammo).
                try:
                    orig = self.client.read(self.pid, t.address, t.vtype.size)
                except ReadFailed:
                    self.results[key] = Result(Verdict.FAILED)
                    continue
                before[key] = orig
                try:
                    data = t.vtype.encode(probe_value(t.vtype, t.vtype.decode(orig)))
                except (ValueError, OverflowError):
                    data = orig
                if data == orig:  # e.g. the tiniest decimal: 3/4 of it is itself
                    self.results[key] = Result(Verdict.FAILED)
                    continue
                w = _Written(t, orig, data)
                written.append((g, w))  # noted first: a write that times out may land
                try:
                    self.client.write(self.pid, t.address, data)
                except WriteFailed:
                    # Refused, but part of it may have landed (NG checks after sending):
                    # it stays noted, so the put-back puts the original back if it did.
                    w.failed = True
            self.wait(self.settle_s)
            after = self._read_all(everyone)
        except Exception:
            self._put_back([w for _g, w in written])
            raise
        # Members written this round are someone else's test value, not followers.
        busy = [w.target for _g, w in written]
        for g, w in written:
            key = (w.target.address, w.target.vtype)
            if w.failed:
                self.results[key] = Result(Verdict.FAILED)
                continue
            # its own bytes read as another type would "follow" it exactly
            others = [w.target] + [o for o in g if not any(_overlap(o, b) for b in busy)]
            self.results[key] = self._judge(others, w, before, after)
        self._put_back([w for _g, w in written])
        if written:
            self.wait(self.settle_s / 2)  # let the copies follow the put-back value

    def _judge(
        self, group: list[Target], w: _Written, before: dict[Key, bytes], after: dict[Key, bytes]
    ) -> Result:
        t = w.target
        now = after.get((t.address, t.vtype))
        if now is None:
            return Result(Verdict.FAILED)
        if now != w.test:
            return Result(Verdict.COPY)
        old, test = t.vtype.decode(w.original), t.vtype.decode(w.test)
        followers = sum(
            1
            for o in group
            if o != t
            and _follows(
                o, before.get((o.address, o.vtype)), after.get((o.address, o.vtype)), old, test
            )
        )
        return Result(Verdict.REAL if followers else Verdict.KEPT, followers)

    def _put_back(self, written: list[_Written]) -> None:
        """Original bytes back where the test value still is, read again right before
        each write. Where the game already wrote its own value, that one stays. Tries
        all, then raises the first error; ``not_restored`` lists what may still hold a
        test value."""
        first: Exception | None = None
        for w in written:
            t = w.target
            try:
                try:
                    now = self.client.read(self.pid, t.address, t.vtype.size)
                except ReadFailed:
                    continue  # gone (an area unloaded): nothing to put back
                if now != w.test:
                    continue
                self.client.write(self.pid, t.address, w.original)
            except Exception as exc:  # a lost link: try the rest, report the first
                log.warning("action=real_check_restore_failed addr=%#x", t.address)
                self.not_restored.append(t.address)
                first = first or exc
        if first is not None:
            raise first

    def _read_all(self, targets: list[Target]) -> dict[Key, bytes]:
        """One read per cluster of nearby addresses."""
        out: dict[Key, bytes] = {}
        for ts in spans(targets):
            out.update(read_span(self.client, self.pid, ts))
        return out


def spans(targets: Sequence[Target], span: int = READ_SPAN) -> list[list[Target]]:
    """``targets`` (no repeats) in address order, cut into runs that one read of at
    most ``span`` bytes covers."""
    ts = sorted(set(targets), key=lambda t: t.address)
    out: list[list[Target]] = []
    for t in ts:
        if out and t.address + t.vtype.size - out[-1][0].address <= span:
            out[-1].append(t)
        else:
            out.append([t])
    return out


def read_span(client: ConsoleClient, pid: int, ts: Sequence[Target]) -> dict[Key, bytes]:
    """One read covering ``ts`` (one of ``spans``). If part of it is gone (an area
    unloaded), each member is read on its own; members that can't be read are left out."""
    out: dict[Key, bytes] = {}
    start = ts[0].address
    end = max(t.address + t.vtype.size for t in ts)
    try:
        data = client.read(pid, start, end - start)
    except ReadFailed:
        for t in ts:
            with contextlib.suppress(ReadFailed):
                out[(t.address, t.vtype)] = client.read(pid, t.address, t.vtype.size)
        return out
    for t in ts:
        o = t.address - start
        out[(t.address, t.vtype)] = data[o : o + t.vtype.size]
    return out

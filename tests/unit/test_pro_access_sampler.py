"""What does this instruction access (spec PR-3, AC-3.4/3.5) on the fake game, whose
CPU really runs the sampler cave."""

from __future__ import annotations

import threading

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import cave as cv
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.pro import access_sampler as sm
from cheatmaker.core.pro.compile import ProWriter
from cheatmaker.core.pro.sites import Overlap
from cheatmaker.errors import LockFailed
from tests import fakegame as fg


@pytest.fixture
def g():
    return fg.build(1)


def _sample(g, **kw):
    kw.setdefault("seconds", 0.5)
    kw.setdefault("sleep", g.fight(1))
    return sm.sample(g.session, fg.WRITER, ProWriter(g.session), **kw)


def test_lists_every_object_the_writer_touches(g):  # AC-3.4
    result = _sample(g)
    objs = {fg.PLAYER_AT[1], *fg.ENEMIES_AT[1]}
    seen = {r.object for r in result.rows}
    # the two enemies share a table slot ((obj >> 4) & 63): the later one is kept
    assert fg.PLAYER_AT[1] in seen and len(seen) == 2 and seen <= objs
    assert {r.address for r in result.rows} == {o + fg.HP for o in seen}
    assert all(r.count == 1 for r in result.rows) and result.polls == 2
    assert result.operand.text == "vmovss [r12+0x2ac], xmm1"


def test_hook_table_and_cave_are_cleared_at_the_end(g):  # AC-3.4
    cave = cv.find_cave(g.session, cv.CAVE_ROOM)
    table = cv.find_table(g.session)
    _sample(g)
    assert g.fake.peek(fg.PID, fg.WRITER, len(fg.VMOVSS_W)) == fg.VMOVSS_W
    assert g.fake.peek(fg.PID, table, cv.TABLE_SIZE) == bytes(cv.TABLE_SIZE)
    assert g.fake.peek(fg.PID, cave, cv.CAVE_ROOM) == bytes(cv.CAVE_ROOM)


def test_counts_grow_with_each_poll_that_sees_the_object(g):
    seen: list[int] = []
    result = _sample(g, sleep=g.fight(3), seconds=1.0, on_update=lambda r, n: seen.append(n))
    assert all(r.count == 3 for r in result.rows) and seen == [1, 2, 3, 4]


def test_cancel_still_unhooks(g):
    cancel = threading.Event()
    cancel.set()
    result = _sample(g, cancel=cancel)
    assert result.cancelled and result.polls == 0
    assert g.fake.peek(fg.PID, fg.WRITER, len(fg.VMOVSS_W)) == fg.VMOVSS_W


def test_no_room_is_refused_and_nothing_hooked(g, monkeypatch):  # AC-3.5
    def full(*_a, **_k):
        raise LockFailed("no spare space at the end of the game's code for the patch")

    monkeypatch.setattr(sm.cv, "find_cave", full)
    n = len(g.fake.writes)
    with pytest.raises(sm.NoRoomForSampler, match="No room for the sampler"):
        _sample(g)
    assert len(g.fake.writes) == n


def test_an_instruction_a_cheat_patches_is_refused(g):
    off = fg.WRITER - fg.BASE
    lock = Cheat("Lock HP", [Location(LocationKind.CODE_PATCH, off, b"\x90" * 10, fg.VMOVSS_W)])
    with pytest.raises(Overlap, match="Lock HP"):
        _sample(g, enabled=[lock])


def test_operand_rules():
    op = sm.operand_at(bytes.fromhex("8b4b10"), 0x1000)  # mov ecx, [rbx+0x10]
    assert (op.base_name, op.disp, op.length) == ("rbx", 0x10, 3)
    neg = sm.operand_at(bytes.fromhex("8b43f8"), 0x1000)  # mov eax, [rbx-8]
    assert neg.disp == -8
    for code, why in (
        (bytes.fromhex("c3"), "doesn't read or write memory"),
        (bytes.fromhex("8b0488"), "one register plus a number"),  # [rax+rcx*4]
        (bytes.fromhex("8b0510000000"), "one register plus a number"),  # [rip+0x10]
        (bytes.fromhex("678b4310"), "one register plus a number"),  # [ebx+0x10]
    ):
        with pytest.raises(LockFailed, match=why):
            sm.operand_at(code, 0x1000)


def test_execute_watch_hits_give_the_same_rows():  # AC-3.5 fallback
    op = sm.operand_at(bytes.fromhex("8b4b10"), 0x1000)
    hits = [WatchHit(0x1000, {"rbx": 0x500}), WatchHit(0x1000, {"rbx": 0x500})]
    hits.append(WatchHit(0x1000, {"rbx": 0x900}))
    hits.append(WatchHit(0x1000))  # no registers: left out
    rows = sm.accesses_from_hits(hits, op)
    assert [(r.address, r.object, r.count) for r in rows] == [(0x510, 0x500, 2), (0x910, 0x900, 1)]

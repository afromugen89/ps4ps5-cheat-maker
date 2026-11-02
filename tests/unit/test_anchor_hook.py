"""Anchor hook: choose the instruction, prove the slot, install / read / remove (AC-14.3)."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import cave as cv
from cheatmaker.core.project import HookFilter, ObjectSpec
from cheatmaker.errors import FindFailed
from tests import fakegame as fg


def _player_object(game: fg.Game) -> ObjectSpec:
    cands = ah.find_hooks(game.session, game.health_addr, 4)
    hook = ah.plan_hook(game.session, cands[0], fg.PLAYER_VT - fg.BASE)
    return ObjectSpec("player", "You", fg.PLAYER_VT - fg.BASE, anchors=[hook])


def test_candidates_prefer_a_confirmed_reader():
    game = fg.build()
    cands = ah.find_hooks(game.session, game.health_addr, 4)
    assert [c.address for c in cands] == [fg.READER, fg.WRITER]
    r, w = cands
    assert (r.role, r.reg, r.disp, r.hits, r.confirmed) == ("reader", "r12", fg.HP, 2, True)
    assert (w.role, w.confirmed) == ("writer", True)
    assert r.text == "vmovss xmm0,[r12+2ACh]" and r.object_for(game.health_addr) == game.player
    assert game.fake.watched[-1] == (game.health_addr, 4, "access", ah.WATCH_SECONDS)


def test_candidates_skip_forms_without_one_object_register():
    code = {
        0x400100: bytes.fromhex("890c88"),  # mov [rax+rcx*4],ecx: index register
        0x400200: bytes.fromhex("8b0581000000"),  # mov eax,[rip+81h]: static, no object
        0x400300: bytes.fromhex("8b4310"),  # mov eax,[rbx+10h]: unconfirmed
    }

    def read(a, n):
        for start, c in code.items():
            end = start + len(c)
            if a + n == end:
                return b"\xcc" * (n - len(c)) + c
        raise AssertionError(hex(a))

    hits = [WatchHit(s + len(c), {"rbx": 0x1000}) for s, c in code.items()]
    [only] = ah.candidates_from_hits(hits, 0x5000, read)
    assert only.address == 0x400300 and not only.confirmed and only.reg == "rbx"


def test_object_vtable_inside_module_only():
    game = fg.build()
    assert ah.object_vtable(game.session, game.player) == fg.PLAYER_VT - fg.BASE
    assert ah.object_vtable(game.session, game.health_addr) is None  # a float, not a vtable
    assert ah.object_vtable(game.session, 0x10) is None  # unmapped


def test_plan_uses_vtable_filter_and_spare_places():
    game = fg.build()
    obj = _player_object(game)
    hook = obj.hook
    assert hook.hook == fg.READER - fg.BASE and hook.filter == HookFilter("vtable")
    assert hook.orig == fg.VMOVSS_R and hook.role == "reader"
    assert fg.BASE + hook.slot == cv.find_slot(game.session)
    assert 0x1000 <= hook.cave < fg.CODE_SIZE and hook.cave % 16 == 0
    assert hook.signature.startswith("C4 C1 7A 10 84 24 AC 02 00 00")
    no_vt = ah.plan_hook(game.session, ah.find_hooks(game.session, game.health_addr, 4)[0], None)
    assert no_vt.filter == HookFilter("none")


def test_install_fills_slot_for_the_player_only_and_remove_undoes_it():
    game = fg.build()
    obj = _player_object(game)
    hook = obj.hook
    assert ah.read_slot(game.session, hook) == (0, 0)
    assert ah.install(game.session, obj) == "installed"
    assert game.fake.peek(fg.PID, fg.READER, 1) == b"\xe9"
    slot_obj, count = ah.read_slot(game.session, hook)  # the "game" ran the routine
    assert slot_obj == game.player and count >= 1, "enemies are filtered out by vtable"
    assert ah.is_installed(game.session, obj)
    assert ah.install(game.session, obj) == "already", "never patches twice"
    ah.remove(game.session, obj, sleep=lambda _s: None)
    assert game.fake.peek(fg.PID, fg.READER, 10) == fg.VMOVSS_R
    assert game.fake.peek(fg.PID, fg.BASE + hook.cave, 4) != bytes(4), "routine left in place"
    assert game.fake.peek(fg.PID, fg.BASE + hook.slot, 16) == bytes(16)
    ah.remove(game.session, obj, sleep=lambda _s: None)  # not ours any more: no-op
    assert ah.install(game.session, obj) == "installed", "reuses the routine left behind"
    assert ah.read_slot(game.session, hook)[0] == game.player


def test_two_runs_same_anchor_follows_the_player():  # AC-14.3
    run1 = fg.build(1)
    obj = _player_object(run1)
    run2 = fg.build(2)  # restart: same code, player elsewhere
    assert run2.player != run1.player
    ah.install(run2.session, obj)
    assert ah.read_slot(run2.session, obj.hook)[0] == run2.player
    run2.move_player(fg.HEAP + 0x6000)  # new biome
    assert ah.read_slot(run2.session, obj.hook)[0] == fg.HEAP + 0x6000


def test_already_loaded_by_cheatrunner_is_detected():  # C10d
    game = fg.build()
    obj = _player_object(game)
    cheat = ah.hook_cheat(game.session, obj, "Find it again hook")
    for loc in cheat.locations:  # what CheatRunner writes before the PC connects
        game.fake.poke(fg.PID, fg.BASE + loc.offset, loc.on)
    writes = len(game.fake.writes)
    assert ah.install(game.session, obj) == "already" and len(game.fake.writes) == writes


def test_install_refuses_when_something_else_patched_the_instruction():
    game = fg.build()
    obj = _player_object(game)
    game.fake.poke(fg.PID, fg.READER, b"\x90" * 10)  # e.g. a Lock it on the same place
    with pytest.raises(FindFailed, match="Another cheat"):
        ah.install(game.session, obj)
    game.fake.poke(fg.PID, fg.READER, fg.VMOVSS_R)
    game.fake.poke(fg.PID, fg.BASE + obj.hook.slot, b"\x01")
    with pytest.raises(FindFailed, match="spare memory"):
        ah.install(game.session, obj)


def test_hook_cheat_lines_and_keep_value_variant():
    game = fg.build()
    obj = _player_object(game)
    plain = ah.hook_cheat(game.session, obj, "hook")
    cave, jmp = plain.locations
    assert plain.saveable and cave.off == cave.on and jmp.off == fg.VMOVSS_R
    keep = ah.hook_cheat(game.session, obj, "Infinite Health", struct.pack("<f", 999.0))
    for loc in keep.locations:
        game.fake.poke(fg.PID, fg.BASE + loc.offset, loc.on)
    game.set_health(10.0)
    for e in game.enemies:
        game.set_health(5.0, e)
    game.fake.read(fg.PID, fg.BASE, 1)  # the game runs a frame
    assert game.health() == 999.0, "player kept at the value"
    assert all(
        struct.unpack("<f", game.fake.peek(fg.PID, e + fg.HP, 4))[0] == 5.0 for e in game.enemies
    )
    with pytest.raises(FindFailed):
        ah.hook_cheat(game.session, ObjectSpec("x", "x", None), "none")


def test_prove_slot():
    game = fg.build()
    slot = cv.find_slot(game.session)
    ah.prove_slot(game.session, slot, seconds=1)
    assert game.fake.watched[-1] == (slot, 8, "write", 1)
    game.fake.write_hits[slot] = [WatchHit(fg.BASE + 0x300)]
    with pytest.raises(FindFailed, match="uses the spare memory"):
        ah.prove_slot(game.session, slot, seconds=1)
    game.fake.poke(fg.PID, slot + 8, b"\x01")
    with pytest.raises(FindFailed, match="isn't free"):
        ah.prove_slot(game.session, slot, seconds=1)


def test_short_instruction_is_never_hooked():  # review blocker
    from cheatmaker.errors import LockFailed

    game = fg.build()
    short = fg.BASE + 0x300
    game.fake.poke(fg.PID, short, bytes.fromhex("894808c34883ec28"))  # ...; ret; sub
    cand = ah.HookCandidate(
        short, bytes.fromhex("894808"), "mov [rax+8],ecx", 9, "writer", "rax", 8, True
    )
    with pytest.raises(LockFailed, match="too short"):
        ah.plan_hook(game.session, cand, fg.PLAYER_VT - fg.BASE)


def test_candidates_prefer_long_instructions_and_skip_odd_forms():
    code = {
        0x400100: bytes.fromhex("894808"),  # mov [rax+8],ecx: 3 bytes, confirmed
        0x400200: bytes.fromhex("898808000000"),  # mov [rax+8],ecx with disp32: 6 bytes
        0x400300: bytes.fromhex("64894808"),  # mov fs:[rax+8],ecx: segment
        0x400400: bytes.fromhex("8948f8"),  # mov [rax-8],ecx: negative disp
        0x400500: bytes.fromhex("67894808"),  # mov [eax+8],ecx: 32-bit base
    }

    def read(a, n):
        for start, c in code.items():
            if a + n == start + len(c):
                return b"\xcc" * (n - len(c)) + c
        raise AssertionError(hex(a))

    regs = {"rax": 0x5000 - 8}
    hits = [WatchHit(s + len(c), regs) for s, c in code.items()]
    cands = ah.candidates_from_hits(hits, 0x5000, read)
    assert [c.address for c in cands] == [0x400200, 0x400100]


def test_pc_off_cheat_has_its_own_room_and_leaves_the_capture_routine_alone():  # review M2
    from cheatmaker.core.freezer import Freezer, LiveCheats

    game = fg.build()
    obj = _player_object(game)
    ah.install(game.session, obj)
    capture = game.fake.peek(fg.PID, fg.BASE + obj.hook.cave, 64)
    keep = ah.hook_cheat(game.session, obj, "Infinite Health", struct.pack("<f", 999.0))
    cave_loc, _jmp = keep.locations
    assert cave_loc.offset == ah.keep_cave(obj.hook) == obj.hook.cave - cv.CAVE_ROOM
    live = LiveCheats(game.session, Freezer(game.session))
    live.turn_on(keep)
    assert game.fake.peek(fg.PID, fg.BASE + obj.hook.cave, 64) == capture, "not rewritten"
    game.set_health(5.0)
    game.fake.read(fg.PID, fg.BASE, 1)
    assert game.health() == 999.0
    assert ah.read_slot(game.session, obj.hook)[0] == game.player, "still captures"
    live.turn_off(keep)
    assert game.fake.peek(fg.PID, fg.READER, 10) == fg.VMOVSS_R
    assert ah.install(game.session, obj) == "installed", "the app can put its hook back"

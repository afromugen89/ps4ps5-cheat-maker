"""The fake game's second version (spec-game-update AC-U11): code shifted, fields moved."""

from __future__ import annotations

import dataclasses
import struct

import iced_x86 as ix

from cheatmaker.core import cave as cv
from cheatmaker.core.locker import Writer
from tests import fakegame as fg


def _insn(game: fg.Game, at: int) -> ix.Instruction:
    return ix.Decoder(64, game.fake.peek(fg.PID, at, 15), ip=at).decode()


def test_v1_is_todays_game():
    v1 = fg.V1
    assert v1.version == fg.VERSION
    assert (v1.writer, v1.reader, v1.sub_site, v1.mov64_site) == (
        fg.WRITER,
        fg.READER,
        fg.SUB_SITE,
        fg.MOV64_SITE,
    )
    assert (v1.hp, v1.hp32, v1.hp64) == (fg.HP, fg.HP32, fg.HP64)
    assert (v1.player_vt, v1.enemy_vt) == (fg.PLAYER_VT, fg.ENEMY_VT)
    assert (v1.vmovss_w, v1.vmovss_r, v1.sub_w, v1.mov64_w) == (
        fg.VMOVSS_W,
        fg.VMOVSS_R,
        fg.SUB_W,
        fg.MOV64_W,
    )
    assert v1.forms == fg.FORMS
    g = fg.build()
    assert g.layout is fg.V1 and g.session.require_game().version == fg.VERSION
    assert g.fake.peek(fg.PID, fg.WRITER, 10) == fg.VMOVSS_W


def test_v2_moves_code_fields_and_classes():
    g = fg.build(layout=fg.V2)
    lay = g.layout
    assert g.session.require_game().version == "03.001.000"
    assert lay.writer == fg.WRITER + 0x80 and lay.reader == fg.READER + 0x80
    for site in (lay.writer, lay.reader):
        insn = _insn(g, site)
        assert insn.memory_base == ix.Register.R12 and insn.memory_displacement == 0x2B4
    assert _insn(g, lay.sub_site).memory_displacement == fg.HP32 + 8
    assert _insn(g, lay.mov64_site).memory_displacement == fg.HP64 + 8
    assert g.fake.peek(fg.PID, fg.WRITER, 10) == b"\xcc" * 10, "nothing at the old place"
    (vt,) = struct.unpack("<Q", g.fake.peek(fg.PID, g.player, 8))
    assert vt == fg.PLAYER_VT + 0x40
    (ident,) = struct.unpack("<I", g.fake.peek(fg.PID, g.player + 8, 4))
    assert ident == 0x40048, "the ident the Player-only guess uses doesn't move"
    assert g.health_addr == g.player + 0x2B4 and g.health() == 100.0
    assert g.value(g.player, "int32") == 100 and g.value(g.player, "int64") == 100_000


def test_v2_hit_writes_the_new_field_through_the_new_code():
    g = fg.build(layout=fg.V2)
    g.hit(g.player, 75.0)
    assert g.health() == 75.0
    assert g.fake.peek(fg.PID, g.player + fg.HP, 4) == bytes(4), "the old field is untouched"
    g.hit(g.player, 60, "int32")
    assert g.value(g.player, "int32") == 60


def test_run_routines_walks_the_v2_sites():
    g = fg.build(layout=fg.V2)
    lay = g.layout
    cave = fg.BASE + 0x2000
    slot = fg.DATA + 0x100
    # a tiny "capture" routine: mov [slot], obj ; jmp back
    rel = slot - (cave + 7)
    g.fake.poke(fg.PID, cave, bytes.fromhex("4C8925") + struct.pack("<i", rel) + b"\xe9\0\0\0\0")
    g.fake.poke(fg.PID, lay.reader, b"\xe9" + struct.pack("<i", cave - (lay.reader + 5)))
    fg.run_routines(g)
    (got,) = struct.unpack("<Q", g.fake.peek(fg.PID, slot, 8))
    assert got in g.touched


def test_changed_reader_uses_another_register():
    lay = dataclasses.replace(fg.V2, changed=True)
    g = fg.build(layout=lay)
    insn = _insn(g, lay.reader)
    assert insn.memory_base == ix.Register.R14 and insn.memory_displacement == 0x2B4
    assert len(lay.vmovss_r) == len(fg.VMOVSS_R)
    assert _insn(g, lay.writer).memory_base == ix.Register.R12


def test_static_value_and_chain_root_follow_the_layout():
    g = fg.build(static=True, chain=True)
    assert struct.unpack("<i", g.fake.peek(fg.PID, fg.DATA + 0x40, 4))[0] == fg.STATIC_VALUE
    moved = dataclasses.replace(fg.V2, static_off=fg.DATA + 0x60, chain_root=fg.DATA + 0x28)
    g2 = fg.build(static=True, chain=True, layout=moved)
    assert g2.fake.peek(fg.PID, fg.DATA + 0x40, 4) == bytes(4)
    assert struct.unpack("<i", g2.fake.peek(fg.PID, fg.DATA + 0x60, 4))[0] == fg.STATIC_VALUE
    assert g2.fake.peek(fg.PID, fg.STATIC_PTR, 8) == bytes(8)
    assert g2.fake.peek(fg.PID, fg.DATA + 0x28, 8) != bytes(8)
    assert fg.build().fake.peek(fg.PID, fg.DATA + 0x40, 4) == bytes(4), "off by default"


def test_fight_drives_the_real_collect_on_v2():
    g = fg.build(layout=fg.V2)
    writer = Writer(g.layout.writer, g.layout.vmovss_w, "vmovss [r12+2B4h],xmm1", 1)
    objs = cv.collect_objects(g.session, writer, 0.5, sleep=g.fight(1))
    # the sampler's table is hashed by address: run 1's two enemies share one entry
    assert g.player in objs and len(objs) == 2 and set(objs) <= {g.player, *g.enemies}
    assert g.health() == 99.0
    assert g.fake.peek(fg.PID, g.layout.writer, 10) == g.layout.vmovss_w, "unhooked after"


def test_fight_also_reads_through_the_reader():
    g = fg.build(3, layout=fg.V2)
    writer = Writer(g.layout.reader, g.layout.vmovss_r, "vmovss xmm0,[r12+2B4h]", 1)
    objs = cv.collect_objects(g.session, writer, 0.5, sleep=g.fight(2))
    assert objs == [g.player] and g.health() == 98.0, "two rounds"

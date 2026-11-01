import struct

import iced_x86 as ix
import pytest

from cheatmaker.console.fake import PROT_R, PROT_W, PROT_X, FakeConsole
from cheatmaker.core import cave as cv
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.locker import Writer
from cheatmaker.core.session import Session
from cheatmaker.errors import LockFailed

PID = 9
BASE = 0x400000
HOOK = BASE + 0x100
DATA = BASE + 0x4000
HEAP = 0x2000_0000
PLAYER, ENEMY1, ENEMY2 = HEAP + 0x100, HEAP + 0x400, HEAP + 0x700
HP = 0x2AC
VMOVSS = bytes.fromhex("C4C17A118C24AC020000")  # vmovss [r12+0x2AC], xmm1


def game():
    fake = FakeConsole()
    fake.add_process(PID, "eboot.bin", "PPSA01285", "Returnal", "03.000.005")
    code = bytearray(b"\xcc" * 0x1000) + bytearray(0x3000)  # code, then spare zeros
    code[0x100:0x10A] = VMOVSS
    fake.add_region(PID, "executable", BASE, bytes(code), PROT_R | PROT_X)
    fake.add_region(PID, "executable", DATA, bytes(0x1000), PROT_R | PROT_W)
    heap = bytearray(0x1000)
    for obj, ident in ((PLAYER, 0x40048), (ENEMY1, 0x30010), (ENEMY2, 0x30010)):
        o = obj - HEAP
        heap[o + 8 : o + 12] = struct.pack("<I", ident)
        heap[o + 0x24 : o + 0x28] = struct.pack("<I", 0x14 if obj != PLAYER else 7)
        heap[o + HP : o + HP + 4] = struct.pack("<f", 100.0)
    fake.add_region(PID, "[anon]", HEAP, bytes(heap), PROT_R | PROT_W)
    s = Session(lambda ip, port: fake)
    s.connect("1.2.3.4")
    s.refresh_game()
    return fake, s


WRITER = Writer(HOOK, VMOVSS, "vmovss [r12+2ACh],xmm1", 3)


def test_target_and_refusals():
    t = cv.writer_target(WRITER)
    assert t.base == ix.Register.R12 and t.disp == HP and t.base_name == "r12"
    with pytest.raises(LockFailed):  # [rax+rcx*4]: can't tell characters apart
        cv.writer_target(Writer(HOOK, bytes.fromhex("890c88"), "mov", 1))


def test_player_only_code_matches_the_known_returnal_cheat():
    span = cv.decode_at(VMOVSS, 0x2B7B43F)
    code = cv.player_only_code(
        span, cv.writer_target(WRITER), 8, 0x40048, struct.pack("<f", 999.0), 0x65D7DB0
    )
    f = ix.Formatter(ix.FormatterSyntax.INTEL)
    text = [f.format(i) for i in ix.Decoder(64, code, ip=0x65D7DB0)]
    assert text[0] == "cmp dword ptr [r12+8],40048h"
    assert text[1].startswith("jne")
    assert text[2] == "mov dword ptr [r12+2ACh],4479C000h"  # 999.0
    assert text[3] == "jmp 0000000002B7B449h"  # back after the hooked instruction
    assert text[4] == "vmovss [r12+2ACh],xmm1"  # everyone else: original write


def test_collect_compare_and_build_cheat():
    fake, s = game()
    table_seen = {}

    def fight(_t):  # the "game" runs the sampler: record the three objects
        if not table_seen:
            table = cv.find_table(s)
            for obj in (PLAYER, ENEMY1, ENEMY2):
                slot = (obj >> 4) & (cv.TABLE_SLOTS - 1)
                fake.poke(PID, table + slot * 8, struct.pack("<Q", obj))
            table_seen["t"] = table
            assert fake.peek(PID, HOOK, 1) == b"\xe9", "hooked while collecting"

    objs = cv.collect_objects(s, WRITER, 1.0, sleep=fight)
    assert objs == [PLAYER, ENEMY1, ENEMY2]
    assert fake.peek(PID, HOOK, 10) == VMOVSS, "unhooked afterwards"
    assert fake.peek(PID, table_seen["t"], cv.TABLE_SIZE) == bytes(cv.TABLE_SIZE), "cleaned"

    guesses = cv.compare_objects(s, PLAYER, objs)
    assert guesses[0].offset == 8 and guesses[0].player_value == 0x40048
    cheat = cv.make_player_only_cheat(s, WRITER, guesses[0], struct.pack("<f", 999.0), "HP")
    cave_loc, hook_loc = cheat.locations
    assert cheat.saveable and hook_loc.offset == HOOK - BASE
    assert hook_loc.on[0] == 0xE9 and hook_loc.off == VMOVSS
    assert cave_loc.off == bytes(len(cave_loc.on)), "cave goes into spare zero bytes"

    live = LiveCheats(s, Freezer(s))
    live.turn_on(cheat)
    assert fake.peek(PID, HOOK, 1) == b"\xe9"
    live.turn_off(cheat)
    assert fake.peek(PID, HOOK, 10) == VMOVSS
    assert fake.peek(PID, BASE + cave_loc.offset, len(cave_loc.on)) == cave_loc.off


def test_only_player_seen_asks_to_hit_an_enemy():
    _fake, s = game()
    with pytest.raises(LockFailed, match="hit an enemy"):
        cv.compare_objects(s, PLAYER, [PLAYER])


def test_guess_fields_prefers_id_like_field():
    player = struct.pack("<4I", 1, 0x40048, 5, 9)
    e1 = struct.pack("<4I", 1, 0x30010, 6, 9)
    e2 = struct.pack("<4I", 1, 0x30010, 7, 9)
    g = cv.guess_fields(player, [e1, e2])
    assert [x.offset for x in g] == [4, 8]

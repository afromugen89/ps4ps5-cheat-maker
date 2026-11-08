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
    with pytest.raises(LockFailed, match="shared"):  # vmovss [r14],xmm0 (Returnal crash)
        cv.writer_target(Writer(HOOK, bytes.fromhex("c4c17a1106"), "vmovss", 1))


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


# -- v3: capture routine ("find it again") -------------------------------------------
CAVE_IP, SLOT = 0x65D7DB0, 0x7F3FFF0
FMT = ix.Formatter(ix.FormatterSyntax.INTEL)


def _text(code: bytes, ip: int = CAVE_IP) -> list[str]:
    return [FMT.format(i) for i in ix.Decoder(64, code, ip=ip)]


def test_capture_code_vtable_filter_on_the_returnal_writer():
    span = cv.decode_at(VMOVSS, 0x2B7B43F)
    code = cv.capture_code(span, cv.writer_target(WRITER), SLOT, CAVE_IP, vtable=0x5A1B2C0)
    t = _text(code)
    assert t[:3] == ["lea rsp,[rsp-80h]", "pushfq", "cmp qword ptr [r12],5A1B2C0h"]
    assert t[3].startswith("jne")
    assert t[4:8] == [
        "mov [7F3FFF0h],r12",  # object address into the slot
        "inc qword ptr [7F3FFF8h]",  # counter: the routine ran
        "popfq",
        "lea rsp,[rsp+80h]",
    ]
    assert t[8:] == ["vmovss [r12+2ACh],xmm1", "jmp 0000000002B7B449h"]
    jne = list(ix.Decoder(64, code, ip=CAVE_IP))[3]
    assert jne.near_branch_target == CAVE_IP + code.index(b"\x9d"), "skips to popfq"


def test_capture_and_keep_value_for_a_writer_writes_over_the_game():  # BUG-04
    span = cv.decode_at(VMOVSS, 0x2B7B43F)
    code = cv.capture_code(
        span,
        cv.writer_target(WRITER),
        SLOT,
        CAVE_IP,
        "field",
        field=8,
        field_value=0x40048,
        value=struct.pack("<f", 1000.0),
    )
    t = _text(code)
    assert t[2] == "cmp dword ptr [r12+8],40048h"
    assert t[6:11] == [
        "popfq",
        "lea rsp,[rsp+80h]",
        "vmovss [r12+2ACh],xmm1",  # the game's write still runs (flags, neighbours)
        "mov dword ptr [r12+2ACh],447A0000h",  # then 1000.0 over it
        "jmp 0000000002B7B449h",
    ]
    assert t[11:] == [
        "popfq",
        "lea rsp,[rsp+80h]",
        "vmovss [r12+2ACh],xmm1",
        "jmp 0000000002B7B449h",
    ]


def test_capture_on_a_reader_that_overwrites_its_register():
    read = bytes.fromhex("488b80ac020000")  # mov rax,[rax+2ACh]
    span = cv.decode_at(read + b"\x90" * 8, 0x401000)[:1]
    target = cv.writer_target(Writer(0x401000, read, "", 1))
    code = cv.capture_code(span, target, SLOT, CAVE_IP, vtable=0x500000, value=struct.pack("<i", 7))
    t = _text(code)
    assert t[4] == "mov [7F3FFF0h],rax", "captured before the read overwrites rax"
    assert t[8:10] == ["mov dword ptr [rax+2ACh],7", "mov rax,[rax+2ACh]"], (
        "reader: value stored, then the game's own read"
    )


def test_capture_without_filter_and_refusals():
    span = cv.decode_at(VMOVSS, 0x2B7B43F)
    target = cv.writer_target(WRITER)
    t = _text(cv.capture_code(span, target, SLOT, CAVE_IP, "none"))
    assert t[2:4] == ["mov [7F3FFF0h],r12", "inc qword ptr [7F3FFF8h]"]
    with pytest.raises(LockFailed):  # no vtable known
        cv.capture_code(span, target, SLOT, CAVE_IP, vtable=None)
    with pytest.raises(LockFailed, match="tell you apart"):
        cv.capture_code(span, target, SLOT, CAVE_IP, "none", value=b"\0\0\0\0")
    with pytest.raises(ValueError):
        cv.capture_code(span, target, SLOT, CAVE_IP, "magic")
    with pytest.raises(LockFailed):
        cv.capture_code(span, target, SLOT, CAVE_IP, "field", value=b"\0\0\0")


def test_find_slot_below_the_table():
    fake, s = game()
    slot = cv.find_slot(s)
    assert slot == DATA + 0x1000 - cv.TABLE_SIZE - cv.SLOT_SIZE and slot % 16 == 0
    fake.poke(PID, slot + 4, b"\x01")
    with pytest.raises(LockFailed, match="spare room"):
        cv.find_slot(s)


def test_wide_vtable_is_compared_through_a_scratch_register():  # e.g. a higher module base
    span = cv.decode_at(VMOVSS, 0x2B7B43F)
    code = cv.capture_code(span, cv.writer_target(WRITER), SLOT, CAVE_IP, vtable=0x8_0040_1000)
    t = _text(code)
    assert t[2:7] == [
        "push rax",
        "mov rax,800401000h",
        "cmp [r12],rax",
        "pop rax",
        t[6],
    ] and t[6].startswith("jne")
    read = bytes.fromhex("488b80ac020000")  # object in rax: the scratch must be another one
    rspan = cv.decode_at(read + b"\x90" * 8, 0x401000)[:1]
    rtarget = cv.writer_target(Writer(0x401000, read, "", 1))
    t = _text(cv.capture_code(rspan, rtarget, SLOT, CAVE_IP, vtable=0x8_0040_1000))
    assert t[2:5] == ["push rbx", "mov rbx,800401000h", "cmp [rax],rbx"]


def _reader(code: bytes, site: int = 0x401000):
    buf = code + b"\xcc" * 32
    return lambda a, n: buf[a - site : a - site + n]


def test_hook_span_refuses_spans_that_cover_a_return_or_a_call():  # review blocker
    with pytest.raises(LockFailed, match="too short"):  # mov [rax+8],ecx; ret; sub rsp,28h
        cv.hook_span(_reader(bytes.fromhex("894808c34883ec28")), 0x401000)
    with pytest.raises(LockFailed, match="too short"):  # mov [rax],ecx; jmp short; ...
        cv.hook_span(_reader(bytes.fromhex("8908eb0290909090")), 0x401000)
    with pytest.raises(LockFailed, match="calls"):  # mov [rax],ecx; call rel32
        cv.hook_span(_reader(bytes.fromhex("8908e800000000")), 0x401000)
    two = bytes.fromhex("8948084889c3")  # mov [rax+8],ecx; mov rbx,rax: plain code
    assert len(cv.hook_span(_reader(two), 0x401000)) == 2
    with pytest.raises(LockFailed, match="too short"):
        cv.hook_span(_reader(two), 0x401000, single=True)
    assert len(cv.hook_span(_reader(VMOVSS), 0x401000, single=True)) == 1


def test_collect_leaves_the_sampler_code_for_threads_still_inside():
    fake, s = game()
    cave = cv.find_cave(s, cv.CAVE_ROOM)
    cv.collect_objects(s, WRITER, 0.25, sleep=lambda _t: None)
    assert fake.peek(PID, HOOK, 10) == VMOVSS, "unhooked"
    assert fake.peek(PID, cave, 4) != bytes(4), "cave code left in place"

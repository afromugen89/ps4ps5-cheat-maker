import pytest

from cheatmaker.core.cheats import LocationKind
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.locker import (
    Writer,
    find_writers,
    instruction_before,
    make_lock_cheat,
    patch_landed,
    verify_original,
)
from cheatmaker.errors import LockFailed
from tests.conftest import HEALTH_ADDR, HEAP_BASE, MODULE_BASE, PID, WRITER_ADDR


def test_instruction_before_finds_writer():
    code = b"\xcc" * 13 + b"\x89\x08"  # ... mov [rax],ecx
    start, raw, text = instruction_before(code, 0x1000)
    assert (start, raw, text) == (0x1000 - 2, b"\x89\x08", "mov [rax],ecx")


def test_instruction_before_prefers_longest():
    # mov dword [rbx+0x10], 5  = C7 43 10 05 00 00 00 (7 bytes)
    code = b"\x90" * 8 + bytes.fromhex("c7431005000000")
    start, raw, _ = instruction_before(code, 0x2000)
    assert raw == bytes.fromhex("c7431005000000") and start == 0x2000 - 7


def test_instruction_before_ignores_non_writes():
    assert instruction_before(b"\x90" * 14 + b"\xc3", 0x10) is None  # ret


def test_find_writers_and_make_cheat(session, fake):
    [w] = find_writers(session, HEALTH_ADDR, 4, 1)
    assert w.address == WRITER_ADDR and w.code == b"\x89\x08" and w.hits == 2
    c = make_lock_cheat(session, w, "Infinite Health")
    loc = c.locations[0]
    assert loc.kind is LocationKind.CODE_PATCH and loc.offset == WRITER_ADDR - MODULE_BASE
    assert loc.on == b"\x90\x90" and loc.off == b"\x89\x08" and c.saveable
    verify_original(session, c)
    live = LiveCheats(session, Freezer(session))
    live.turn_on(c)  # live test before save
    assert fake.peek(PID, WRITER_ADDR, 2) == b"\x90\x90"
    verify_original(session, c)  # still fine when already patched
    live.turn_off(c)
    assert fake.peek(PID, WRITER_ADDR, 2) == b"\x89\x08"


def test_nothing_wrote(session, fake):
    fake.writers.clear()
    with pytest.raises(LockFailed):
        find_writers(session, HEALTH_ADDR, 4, 1)


def test_unreadable_or_undecodable_rips(session, fake):
    fake.writers[HEALTH_ADDR] = [0x10, MODULE_BASE + 0x20]  # unmapped, then int3 padding
    with pytest.raises(LockFailed):
        find_writers(session, HEALTH_ADDR, 4, 1)


def test_writer_outside_module_refused(session, fake):

    with pytest.raises(LockFailed):
        make_lock_cheat(session, Writer(HEAP_BASE, b"\x89\x08", "mov", 1), "x")


def test_verify_original_detects_changed_code(session, fake):
    [w] = find_writers(session, HEALTH_ADDR, 4, 1)
    c = make_lock_cheat(session, w, "x")
    fake.poke(PID, WRITER_ADDR, b"\x00\x00")
    with pytest.raises(LockFailed):
        verify_original(session, c)


def test_patch_landed_reads_code_back(session, fake, monkeypatch):
    [w] = find_writers(session, HEALTH_ADDR, 4, 1)
    c = make_lock_cheat(session, w, "x")
    live = LiveCheats(session, Freezer(session))
    live.turn_on(c)
    assert patch_landed(session, c)
    # A console that acks code writes without applying them (C4 suspicion).
    fake.poke(PID, WRITER_ADDR, b"\x89\x08")
    assert not patch_landed(session, c)


def test_instruction_before_finds_readers_with_touches_memory():
    from cheatmaker.core.locker import touches_memory

    read = bytes.fromhex("418b8424ac020000")  # mov eax,[r12+2ACh]
    rip = 0x400108
    writer = instruction_before(read, rip)  # default: writers only, so not the mov
    assert writer is None or writer[0] != 0x400100
    start, code, text = instruction_before(read, rip, want=touches_memory)
    assert start == 0x400100 and code == read and text == "mov eax,[r12+2ACh]"
    lea = bytes.fromhex("498d442410")  # lea rax,[r12+10h]: names an address, no access
    assert instruction_before(lea, 0x400105, want=touches_memory) is None


def test_instruction_before_does_not_eat_previous_instruction():
    # mov eax,0x41000000 ; mov [rbx],ecx. "41 89 0B" (mov [r11],ecx) also ends at rip,
    # but starts inside the mov eax: NOPing it would corrupt the game's code.
    code = bytes.fromhex("b800000041890b")
    start, raw, text = instruction_before(code, 0x1007)
    assert (start, raw, text) == (0x1005, b"\x89\x0b", "mov [rbx],ecx")


def test_instruction_before_resyncs_in_real_context():
    # A run of real instructions before the writer; the last byte before it is 0x41.
    prefix = bytes.fromhex(
        "4889c34883c010c7400800000041"
    )  # mov rbx,rax; add rax,10h; mov dword [rax+8],41000000h
    code = prefix + bytes.fromhex("890b")
    start, raw, _ = instruction_before(code, 0x5000)
    assert start == 0x5000 - 2 and raw == b"\x89\x0b"


@pytest.mark.parametrize(
    ("hexcode", "unsafe"),
    [
        ("890b", False),  # mov [rbx],ecx
        ("c5fa118c24ac020000", False),  # vmovss [rsp+2ACh],xmm1
        ("ff00", False),  # inc dword [rax]
        ("f00fb10b", True),  # lock cmpxchg [rbx],ecx
        ("8708", True),  # xchg [rax],ecx
        ("f3aa", True),  # rep stosb
        ("8f00", True),  # pop qword [rax]
    ],
)
def test_nop_unsafe(hexcode, unsafe):
    from cheatmaker.core.locker import decode_one, nop_unsafe

    assert (nop_unsafe(decode_one(bytes.fromhex(hexcode), 0)) is not None) is unsafe


def test_make_lock_cheat_refuses_unsafe_writer(session):
    w = Writer(MODULE_BASE + 0x100, bytes.fromhex("f00fb10b"), "lock cmpxchg", 1, "atomic")
    with pytest.raises(LockFailed, match="freeze"):
        make_lock_cheat(session, w, "x")


def test_bare_pointer_writers_are_flagged_shared():
    from cheatmaker.core.locker import bare_pointer, decode_one

    assert bare_pointer(decode_one(bytes.fromhex("890b"), 0))  # mov [rbx],ecx
    assert bare_pointer(decode_one(bytes.fromhex("c4c17a1106"), 0))  # vmovss [r14],xmm0
    assert not bare_pointer(decode_one(bytes.fromhex("c5fa118f28070000"), 0))  # [rdi+728h]
    assert not bare_pointer(decode_one(bytes.fromhex("890c88"), 0))  # [rax+rcx*4]

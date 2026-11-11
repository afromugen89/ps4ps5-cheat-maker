import pytest

from cheatmaker.core.cheats import LocationKind
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.locker import (
    FLAGS_UNSAFE,
    FlagsUse,
    Writer,
    find_writers,
    instruction_before,
    make_lock_cheat,
    patch_landed,
    verify_original,
)
from cheatmaker.errors import LockFailed, ReadFailed
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


# -- flags the game reads after a writer (spec-cheat-builder S3) -----------------------
SITE = 0x401000
SUB = "2988ac020000"  # sub [rax+2ACh],ecx: sets all six status flags


def _code_at(hexcode: str, site: int = SITE):
    buf = bytes.fromhex(hexcode)

    def read(addr: int, size: int) -> bytes:
        if not site <= addr < site + len(buf):
            raise ReadFailed("not mapped")
        return buf[addr - site : addr - site + size]

    return read


def _flags(hexcode: str, budget: int = 32):
    from cheatmaker.core.locker import decode_one, flags_read_after

    insn = decode_one(bytes.fromhex(hexcode), SITE)
    writer = Writer(SITE, bytes.fromhex(hexcode)[: insn.len], "", 1)
    return flags_read_after(_code_at(hexcode), writer, budget)


@pytest.mark.parametrize(
    ("hexcode", "read", "unknown"),
    [
        ("898808000000" + "7e00c3", set(), False),  # mov: no flags to read
        (SUB + "7e00c3", {"ZF", "SF", "OF"}, False),  # sub ; jle: the classic
        (SUB + "89c883c001c3", set(), False),  # mov leaves them, add rewrites all
        (SUB + "c3", set(), False),  # ret: flags are dead
        (SUB + "e800000000", set(), False),  # call: flags are dead
        (SUB + "ffe0", set(), True),  # jmp rax: can't follow
        (SUB + "06", set(), True),  # doesn't decode
        (SUB + "0f0b", set(), True),  # ud2: can't follow
        (SUB + "ebfe", set(), True),  # jmp to itself: runs out of budget
        (SUB + "eb0190" + "72fec3", {"CF"}, False),  # jmp over a byte, then jb
        (SUB + "740483c001c3" + "7200c3", {"ZF", "CF"}, False),  # jz both ways
        ("ff4008" + "720039c0c3", set(), False),  # inc leaves CF: jb reads the old one
        ("ff4008" + "7c00c3", {"SF", "OF"}, False),  # inc ; jl
        (SUB + "9c", {"CF", "PF", "AF", "ZF", "SF", "OF"}, True),  # pushfq, then ends
    ],
)
def test_flags_read_after(hexcode, read, unknown):
    use = _flags(hexcode)
    assert set(use.read) == read and use.unknown is unknown
    assert use.used is (bool(read) or unknown)


def test_flags_read_after_budget():
    code = SUB + "90" * 40 + "7e00c3"
    assert _flags(code).unknown
    assert _flags(code, budget=64) == FlagsUse(frozenset({"ZF", "SF", "OF"}), False)


def test_find_writers_marks_a_writer_whose_flags_are_read(session, fake):
    # sub [rax+8],ecx ; jle: switching it off leaves the jle on stale flags
    fake.poke(PID, WRITER_ADDR, bytes.fromhex("294808" + "7e00c3"))
    fake.writers[HEALTH_ADDR] = [WRITER_ADDR + 3]
    [w] = find_writers(session, HEALTH_ADDR, 4, 1)
    assert w.unsafe == FLAGS_UNSAFE and w.flags == FlagsUse(frozenset({"ZF", "SF", "OF"}))
    with pytest.raises(LockFailed, match="right after it"):
        make_lock_cheat(session, w, "x")


def test_find_writers_keeps_safe_and_earlier_reasons(session, fake):
    [w] = find_writers(session, HEALTH_ADDR, 4, 1)  # mov [rax],ecx
    assert w.unsafe is None and w.flags == FlagsUse()
    fake.poke(PID, WRITER_ADDR, bytes.fromhex("f00fc108" + "7e00c3"))  # lock xadd ; jle
    fake.writers[HEALTH_ADDR] = [WRITER_ADDR + 4]
    [w] = find_writers(session, HEALTH_ADDR, 4, 1)
    assert "atomic" in w.unsafe and w.flags.used


def test_find_writers_passes_until_to_the_watch(session, fake):
    asked = []
    [w] = find_writers(session, HEALTH_ADDR, 4, 1, until=lambda h, t: asked.append(h) or True)
    assert w.address == WRITER_ADDR and len(asked) == 1 and len(asked[0]) == 2
    assert fake.until_answers == [True]

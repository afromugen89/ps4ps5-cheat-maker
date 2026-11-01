import pytest

from cheatmaker.core.cheats import LocationKind
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.locker import (
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
    from cheatmaker.core.locker import Writer

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

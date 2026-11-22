"""Pro step 8: compile, placement, budget, on/off order, read-back, 0xFF warning,
overlap, live variable rewrite (AC-8.2 to AC-8.8) on FakeConsole."""

from __future__ import annotations

import logging
import struct

import pytest

from cheatmaker.console.fake import PROT_R, PROT_W, PROT_X, FakeConsole
from cheatmaker.core import project as pj
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.pro import compile as pc_
from cheatmaker.core.pro import sites
from cheatmaker.core.pro.asm import FakeAssembler
from cheatmaker.core.pro.model import ProCheat, ProEntry, Variable
from cheatmaker.core.session import Session
from cheatmaker.core.values import ValueType
from cheatmaker.errors import LockFailed, WriteFailed

PID = 7
BASE = 0x400000
CODE = 0x2000  # code, the last 0x1000 of it spare zeros
HOOK = 0x100  # movss [rax+0x10], xmm1 ; mov ecx, 1
HOOK_CODE = bytes.fromhex("f30f114810")  # what the jump covers
CALL_SITE = 0x200  # call +0 right after a 2-byte instruction
NOP_SITE = 0x300  # mov [rbx+8], ecx
NOP_CODE = bytes.fromhex("894b08")
MULT = Variable("mult", ValueType.FLOAT, 2.0, "damage taken x")
SOURCE = "nop\noriginal\njmp return\n"


def _fake() -> FakeConsole:
    fake = FakeConsole()
    fake.add_process(PID, "eboot.bin", "PPSA01234", "Test Game", "01.000")
    code = bytearray(b"\xcc" * CODE)
    code[0x1000:] = bytes(0x1000)
    code[HOOK : HOOK + 10] = HOOK_CODE + bytes.fromhex("b901000000")
    code[CALL_SITE : CALL_SITE + 7] = bytes.fromhex("89c8") + bytes.fromhex("e800000000")
    code[NOP_SITE : NOP_SITE + 3] = NOP_CODE
    fake.add_region(PID, "eboot.bin", BASE, bytes(code), PROT_R | PROT_X)
    fake.add_region(PID, "eboot.bin", BASE + CODE, bytes(0x100), PROT_R | PROT_W)
    return fake


@pytest.fixture
def fake() -> FakeConsole:
    return _fake()


@pytest.fixture
def s(fake) -> Session:
    session = Session(lambda ip, port: fake)
    session.connect("192.168.1.50")
    session.refresh_game()
    return session


def _cheat(s: Session, source: str = SOURCE, variables=(MULT,), editable="mult") -> ProCheat:
    entry = pc_.new_cave_entry(s, HOOK, source)
    pc = ProCheat("p1", "Health x mult", [entry], list(variables), editable)
    assert pc_.build_all(s, pc, FakeAssembler()) is None
    return pc


# -- building and placement -----------------------------------------------------------------
def test_new_cave_entry_covers_whole_instructions(s):
    entry = pc_.new_cave_entry(s, HOOK)
    assert entry.off == HOOK_CODE and entry.source == pc_.DEFAULT_SOURCE and entry.cave is None


def test_hook_over_a_call_is_refused(s):
    """AC-8.3: the existing hook_span message."""
    with pytest.raises(LockFailed, match="calls another part of the game"):
        pc_.new_cave_entry(s, CALL_SITE)


def test_build_places_the_cave_and_makes_the_jump(s):
    pc = _cheat(s)
    (entry,) = pc.entries
    assert entry.cave is not None and 0x1000 <= entry.cave < CODE and entry.cave % 16 == 0
    assert entry.on[0] == 0xE9 and len(entry.on) == len(HOOK_CODE)
    assert struct.unpack_from("<i", entry.on, 1)[0] == entry.cave - (HOOK + 5)
    assert entry.cave_bytes.startswith(b"\x90" + HOOK_CODE)
    assert entry.cave_bytes[-4:] == struct.pack("<f", 2.0)
    assert pc_.variable_addresses(pc) == {"mult": entry.cave + len(entry.cave_bytes) - 4}


def test_assemble_error_leaves_the_entry(s):
    entry = pc_.new_cave_entry(s, HOOK, "mov eax, 1\n")
    pc = ProCheat("p1", "Bad", [entry])
    result = pc_.build_all(s, pc, FakeAssembler())
    assert result is not None and result.error is not None and result.error[0] == 1
    assert entry.cave is None and entry.cave_bytes == b""


def test_too_big_a_cave_is_refused_with_the_budget(s):
    """AC-8.2: "needs N bytes, M free"."""
    free = sites.free_room(s)
    assert 0x1000 - 64 <= free < 0x1000
    big = "db " + ", ".join(["0x90"] * (free + 1)) + "\n"
    entry = pc_.new_cave_entry(s, HOOK, big)
    with pytest.raises(pc_.NoRoom, match=rf"needs {free + 1} bytes, {free} free"):
        pc_.build(s, ProCheat("p1", "Big", [entry]), entry, FakeAssembler())
    assert entry.cave is None


def test_two_caves_never_share_room(s):
    first = _cheat(s)
    other = ProCheat("p2", "Other", [ProEntry("cave", HOOK, HOOK_CODE, source="ret\n")])
    pc_.build(s, other, other.entries[0], FakeAssembler(), [first])
    a, b = first.entries[0], other.entries[0]
    assert b.cave + len(b.cave_bytes) <= a.cave or a.cave + len(a.cave_bytes) <= b.cave


def test_a_cave_grows_only_into_free_zeros(s, fake):
    pc = _cheat(s, "nop\n", variables=(), editable=None)
    entry = pc.entries[0]
    room = sites.room_at(s, entry.cave, len(entry.cave_bytes))
    assert room >= 0x100
    # something right after the cave: only up to the spacer before it
    fake.poke(PID, BASE + entry.cave + 0x20, b"\x01")
    assert sites.room_at(s, entry.cave, 1) == 0x20 - sites.SPACER
    entry.source = "db " + ", ".join(["0x90"] * 0x40) + "\n"
    with pytest.raises(pc_.NoRoom, match=r"needs 64 bytes, 16 free"):
        pc_.build(s, pc, entry, FakeAssembler())


# -- compile --------------------------------------------------------------------------------
def test_compile_lines_and_editable_value(s):
    pc = _cheat(s)
    cheat = pc_.compile_cheat(pc)
    entry = pc.entries[0]
    cave_line, hook_line = cheat.locations
    assert (cave_line.offset, cave_line.on, cave_line.off) == (
        entry.cave,
        entry.cave_bytes,
        entry.cave_bytes,
    )
    assert (hook_line.offset, hook_line.on, hook_line.off) == (HOOK, entry.on, HOOK_CODE)
    assert cheat.pro is pc and cheat.saveable and cheat.template == "pro"
    assert cheat.value_type is ValueType.FLOAT and cheat.value == 2.0
    cheat.set_value(5.0)
    pc_.take_value(cheat)
    assert pc.variable("mult").value == 5.0
    assert entry.cave_bytes[-4:] == struct.pack("<f", 5.0)


def test_compile_refuses_unbuilt_caves():
    entry = ProEntry("cave", HOOK, HOOK_CODE, source="ret")
    with pytest.raises(LockFailed, match="isn't assembled"):
        pc_.compile_cheat(ProCheat("p1", "X", [entry]))
    with pytest.raises(LockFailed, match="nothing"):
        pc_.compile_cheat(ProCheat("p1", "X", []))


def test_bytes_and_nop_entries_compile(s):
    nop = pc_.new_nop_entry(s, NOP_SITE)
    assert nop.off == NOP_CODE and nop.on == b"\x90" * 3
    patch = ProEntry("bytes", 0x1F00, b"\0\0", b"\1\2")
    cheat = pc_.compile_cheat(ProCheat("p3", "N", [nop, patch]))
    assert [(loc.offset, loc.on) for loc in cheat.locations] == [
        (NOP_SITE, b"\x90" * 3),
        (0x1F00, b"\1\2"),
    ]
    assert cheat.value_type is None


def test_other_caves_see_the_variables_as_symbols(s):
    home = pc_.new_cave_entry(s, HOOK, "original\njmp return\n")
    second = ProEntry("cave", NOP_SITE, NOP_CODE + b"\xcc\xcc", source="jmp mult\n")
    pc = ProCheat("p1", "Two", [home, second], [MULT])
    assert pc_.build_all(s, pc, FakeAssembler()) is None
    target = second.cave + 5 + struct.unpack_from("<i", second.cave_bytes, 1)[0]
    assert target == pc_.variable_addresses(pc)["mult"]


# -- turning on and off ---------------------------------------------------------------------
def test_enable_writes_cave_then_jump_and_reads_back(s, fake):
    pc = _cheat(s)
    pc.entries.append(pc_.new_nop_entry(s, NOP_SITE))
    entry = pc.entries[0]
    pc_.enable(s, pc, pc_.ProWriter(s))
    assert [a - BASE for _, a, _ in fake.writes] == [entry.cave, HOOK, NOP_SITE]
    assert fake.peek(PID, BASE + entry.cave, len(entry.cave_bytes)) == entry.cave_bytes
    assert fake.peek(PID, BASE + HOOK, len(entry.on)) == entry.on


def test_enable_refuses_when_a_site_changed(s, fake):
    """AC-8.4: nothing written, and the site with expected and found bytes."""
    pc = _cheat(s)
    fake.poke(PID, BASE + HOOK, b"\x90")
    with pytest.raises(pc_.SiteChanged) as err:
        pc_.enable(s, pc, pc_.ProWriter(s))
    assert fake.writes == []
    addr, want, got = err.value.found[0]
    assert addr == BASE + HOOK and want == HOOK_CODE and got[0] == 0x90
    assert "expected f3 0f 11" in str(err.value)


def test_enable_accepts_a_cave_left_by_a_cheat_file(s, fake):
    pc = _cheat(s)
    entry = pc.entries[0]
    fake.poke(PID, BASE + entry.cave, entry.cave_bytes)
    pc_.enable(s, pc, pc_.ProWriter(s))
    assert fake.peek(PID, BASE + HOOK, 1) == b"\xe9"


def test_enable_fails_on_a_bad_read_back(s, fake, monkeypatch):
    pc = _cheat(s)
    real_write = fake.write
    monkeypatch.setattr(fake, "write", lambda pid, addr, data: real_write(pid, addr, data[:-1]))
    with pytest.raises(WriteFailed, match="didn't take"):
        pc_.enable(s, pc, pc_.ProWriter(s))


def test_disable_restores_jump_first_then_waits_then_zeroes_the_cave(s, fake):
    """AC-8.5."""
    pc = _cheat(s)
    pc.entries.append(pc_.new_nop_entry(s, NOP_SITE))
    entry = pc.entries[0]
    pc_.enable(s, pc, pc_.ProWriter(s))
    fake.writes.clear()
    order: list[str] = []
    real_write = fake.write

    def write(pid, addr, data):
        order.append(f"write {addr - BASE:#x}")
        real_write(pid, addr, data)

    fake.write = write  # type: ignore[method-assign]
    pc_.disable(s, pc, pc_.ProWriter(s), sleep=lambda t: order.append(f"sleep {t}"))
    assert order == [
        f"write {HOOK:#x}",
        f"write {NOP_SITE:#x}",
        "sleep 0.5",
        f"write {entry.cave:#x}",
    ]
    assert fake.peek(PID, BASE + HOOK, len(HOOK_CODE)) == HOOK_CODE
    assert fake.peek(PID, BASE + entry.cave, len(entry.cave_bytes)) == bytes(len(entry.cave_bytes))


def test_writes_over_an_ff_address_warn_once(s, caplog):
    """AC-8.6."""
    caplog.set_level(logging.INFO)
    shown: list[str] = []
    w = pc_.ProWriter(s, warn=shown.append)
    w.write(BASE + 0x10FE, b"\0\0\0", "t")
    w.write(BASE + 0x11FF, b"\0\0", "t")
    w.write(BASE + 0x1200, b"\0\0", "t")
    assert shown == [pc_.FF_WARNING]
    assert sum("action=pro_write_ff" in r.message for r in caplog.records) == 2
    assert sum("action=pro_write " in r.message for r in caplog.records) == 3


@pytest.mark.parametrize(
    ("addr", "size", "crosses"),
    [
        (0xFF, 1, False),
        (0xFF, 2, True),
        (0xFE, 2, False),
        (0x1F0, 0x20, True),
        (0x100, 0x100, False),
    ],
)
def test_crosses_ff(addr, size, crosses):
    assert pc_.crosses_ff(addr, size) is crosses


def test_overlapping_cheat_is_refused_by_name(s):
    """AC-8.8."""
    pc = _cheat(s)
    guided = Cheat(
        "Infinite health", [Location(LocationKind.CODE_PATCH, HOOK + 2, b"\x90", b"\x11")]
    )
    guided.enabled = True
    with pytest.raises(sites.Overlap, match="Infinite health"):
        pc_.enable(s, pc, pc_.ProWriter(s), [guided])
    other = _cheat(s)
    other.id = "p9"
    with pytest.raises(sites.Overlap, match="Health x mult"):
        pc_.enable(s, other, pc_.ProWriter(s), [pc_.compile_cheat(pc)])
    heap = Cheat("Gold", [Location(LocationKind.SESSION_ADDRESS, 0x2000_0000, b"\1", b"\0")])
    pc_.enable(s, pc, pc_.ProWriter(s), [heap])  # elsewhere: fine


# -- live variable rewrite (AC-8.7) -----------------------------------------------------------
def test_variable_change_off_only_changes_stored_bytes(s, fake):
    pc = _cheat(s)
    assert pc_.set_variable(pc, "mult", 5.0, FakeAssembler()) == pc_.SAVED
    assert pc.entries[0].cave_bytes[-4:] == struct.pack("<f", 5.0)
    assert fake.writes == []


def test_variable_change_on_rewrites_only_its_bytes(s, fake):
    pc = _cheat(s)
    writer = pc_.ProWriter(s)
    pc_.enable(s, pc, writer)
    fake.writes.clear()
    result = pc_.set_variable(pc, "mult", 5.0, FakeAssembler(), session=s, writer=writer, on=True)
    assert result == pc_.WRITTEN
    addr = BASE + pc_.variable_addresses(pc)["mult"]
    assert [(a, d) for _, a, d in fake.writes] == [(addr, struct.pack("<f", 5.0))]
    assert fake.peek(PID, addr, 4) == struct.pack("<f", 5.0)


def test_variable_change_patches_the_built_cave_only(s):
    """QA BUG-06: an edited but unbuilt source never reaches the cave."""
    pc = _cheat(
        s, "nop\noriginal\njmp return\n", variables=(MULT, Variable("n", ValueType.INT32, 1))
    )
    entry = pc.entries[0]
    built = entry.cave_bytes
    entry.source = "nop\nnop\noriginal\njmp return\n"  # edited, not rebuilt
    assert pc_.set_variable(pc, "mult", 3.0) == pc_.SAVED
    assert len(entry.cave_bytes) == len(built)
    assert entry.cave_bytes[:-8] == built[:-8]
    assert entry.cave_bytes[-8:] == struct.pack("<fi", 3.0, 1)
    with pytest.raises(ValueError):
        pc_.set_variable(pc, "n", 1 << 40)
    with pytest.raises(ValueError, match="session and the writer"):
        pc_.set_variable(pc, "mult", 4.0, on=True)
    assert pc.variable("mult").value == 3.0, "nothing changed by a refused call"
    unbuilt = ProCheat("p2", "U", [ProEntry("cave", HOOK, HOOK_CODE)], [MULT])
    with pytest.raises(LockFailed, match="no built cave"):
        pc_.set_variable(unbuilt, "mult", 1.0)


def test_a_stale_cheat_compiles_to_no_lines(s):
    """QA BUG-05."""
    pc = _cheat(s)
    pc.entries[0].stale = True
    cheat = pc_.compile_cheat(pc)
    assert cheat.locations == [] and not cheat.saveable and cheat.hint == pc_.STALE_HINT


# -- the project (Cheat.pro saved via format 5) --------------------------------------------------
def test_store_and_restore_through_the_cmproj(s, tmp_path):
    pc = _cheat(s)
    cheat = pc_.compile_cheat(pc)
    cheat.set_value(4.0)
    project = pj.Project("PPSA01234", "01.000", "eboot.bin")
    assert pc_.store(project, [cheat, Cheat("guided", [])])
    assert not pc_.store(project, [cheat])
    path = tmp_path / "game.cmproj"
    pj.save(path, project)
    loaded = pj.load(path)
    assert [p.to_json() for p in loaded.pro_cheats] == [pc.to_json()]
    cheats: list[Cheat] = []
    (again,) = pc_.restore(loaded, cheats)
    assert again == cheat and again.value == 4.0 and again.pro is not None
    assert pc_.restore(loaded, cheats) == []
    pc_.forget(loaded, "p1")
    assert loaded.pro_cheats == []


def test_restore_skips_an_unbuilt_cheat(caplog):
    project = pj.Project("PPSA01234", "01.000", "eboot.bin")
    project.pro_cheats.append(ProCheat("p1", "X", [ProEntry("cave", HOOK, HOOK_CODE)]))
    assert pc_.restore(project, []) == []
    assert "action=pro_restore" in caplog.text


def test_cave_rooms_and_ranges(s):
    pc = _cheat(s)
    entry = pc.entries[0]
    assert sites.cave_rooms([pc]) == [(entry.cave, entry.cave + len(entry.cave_bytes))]
    assert sites.cave_rooms([pc], skip="p1") == []
    assert sites.pro_ranges(pc) == [
        (HOOK, HOOK + len(HOOK_CODE)),
        (entry.cave, entry.cave + len(entry.cave_bytes)),
    ]
    heap = Cheat("H", [Location(LocationKind.SESSION_ADDRESS, BASE + 0x50, b"\1\2", b"\0\0")])
    assert sites.cheat_ranges(heap, BASE) == [(0x50, 0x52)]


# -- review fixes ---------------------------------------------------------------------------
class _FailOn:
    """FakeAssembler that fails one source."""

    def __init__(self, bad: str) -> None:
        self.fake, self.bad = FakeAssembler(), bad

    def assemble(self, source, ctx, variables=()):
        if source == self.bad:
            return pc_.AsmResult(b"", {}, (1, "boom"))
        return self.fake.assemble(source, ctx, variables)


def _two_caves(s: Session) -> ProCheat:
    home = pc_.new_cave_entry(s, HOOK, "original\njmp return\n")
    second = ProEntry("cave", NOP_SITE, NOP_CODE + b"\xcc\xcc", source="jmp mult\n")
    pc = ProCheat("p1", "Two", [home, second], [MULT])
    assert pc_.build_all(s, pc, FakeAssembler()) is None
    return pc


def _target(entry: ProEntry) -> int:
    return entry.cave + 5 + struct.unpack_from("<i", entry.cave_bytes, 1)[0]


def test_moving_the_variables_rebuilds_the_other_caves(s):
    """Review M2."""
    pc = _two_caves(s)
    home, second = pc.entries
    home.source = "nop\nnop\nnop\nnop\noriginal\njmp return\n"
    pc_.build(s, pc, home, FakeAssembler())
    assert _target(second) == pc_.variable_addresses(pc)["mult"] and not second.stale


def test_a_cave_that_cant_be_rebuilt_is_stale_and_refused(s):
    pc = _two_caves(s)
    home, second = pc.entries
    home.source = "nop\nnop\nnop\nnop\noriginal\njmp return\n"
    pc_.build(s, pc, home, _FailOn("jmp mult\n"))
    assert second.stale and not home.stale
    assert ProEntry.from_json(second.to_json()).stale
    with pytest.raises(LockFailed, match="needs assembling again"):
        pc_.enable(s, pc, pc_.ProWriter(s))
    pc_.build(s, pc, second, FakeAssembler())
    assert not second.stale and _target(second) == pc_.variable_addresses(pc)["mult"]


def test_stale_and_hook_rules_in_the_model():
    with pytest.raises(ValueError, match="jump at its hook"):
        ProEntry("cave", HOOK, HOOK_CODE, b"", cave=0x1F00, cave_bytes=b"\xc3")
    with pytest.raises(ValueError, match="stale"):
        ProEntry("bytes", 1, b"\0", b"\1", stale=True)
    assert "stale" not in ProEntry("cave", HOOK, HOOK_CODE).to_json()


def test_take_value_keeps_a_float_that_encodes_the_same(s):
    """Review M4: 2.1 is stored as a float32; reading it back gives 2.0999999..."""
    pc = _cheat(s, variables=(Variable("mult", ValueType.FLOAT, 2.1),))
    cheat = pc_.compile_cheat(pc)
    assert cheat.value != 2.1
    pc_.take_value(cheat)
    assert pc.variable("mult").value == 2.1
    cheat.set_value(2.5)
    pc_.take_value(cheat)
    assert pc.variable("mult").value == 2.5


def test_a_failed_read_back_undoes_the_writes_in_reverse(s, fake, monkeypatch):
    pc = _cheat(s)
    pc.entries.append(pc_.new_nop_entry(s, NOP_SITE))
    entry = pc.entries[0]
    real_write = fake.write

    def write(pid, addr, data):  # the NOP site doesn't take the new bytes
        real_write(pid, addr, data[:-1] + b"\xcc" if addr == BASE + NOP_SITE else data)

    monkeypatch.setattr(fake, "write", write)
    with pytest.raises(WriteFailed):
        pc_.enable(s, pc, pc_.ProWriter(s))
    order = [a - BASE for _, a, _ in fake.writes]
    assert order == [entry.cave, HOOK, NOP_SITE, NOP_SITE, HOOK, entry.cave]
    assert fake.peek(PID, BASE + HOOK, 5) == HOOK_CODE
    assert fake.peek(PID, BASE + entry.cave, 4) == bytes(4)
    assert fake.peek(PID, BASE + NOP_SITE, 3)[:2] == NOP_CODE[:2]


def test_restore_skips_a_damaged_cheat(caplog, monkeypatch):
    project = pj.Project("PPSA01234", "01.000", "eboot.bin")
    project.pro_cheats.append(ProCheat("p1", "X", [ProEntry("nop", NOP_SITE, NOP_CODE)]))

    def bad(_pc):
        raise ValueError("damaged")

    monkeypatch.setattr(pc_, "compile_cheat", bad)
    assert pc_.restore(project, []) == []
    assert "damaged" in caplog.text


def test_build_keeps_out_of_open_cheats_lines(s):
    entry = pc_.new_cave_entry(s, HOOK, "ret\n")
    first = pc_.new_cave_entry(s, HOOK, "ret\n")
    pc_.build(s, ProCheat("p8", "A", [first]), first, FakeAssembler())
    guided = Cheat("G", [Location(LocationKind.CODE_PATCH, first.cave, b"\x90", b"\0")])
    pc = ProCheat("p1", "B", [entry])
    pc_.build(s, pc, entry, FakeAssembler(), open_rooms=sites.open_rooms([guided]))
    assert not entry.cave <= first.cave < entry.cave + len(entry.cave_bytes)
    alone = pc_.new_cave_entry(s, HOOK, "ret\n")
    pc_.build(s, ProCheat("p9", "C", [alone]), alone, FakeAssembler())
    assert alone.cave == first.cave, "without the open lines it would take that room"
    assert sites.open_rooms([pc_.compile_cheat(pc)], skip="p1") == []


def test_sources_are_checked_against_the_module_size(s):
    entry = pc_.new_cave_entry(s, HOOK, "jmp 0x3000\n")
    result = pc_.build(s, ProCheat("p1", "Far", [entry]), entry, FakeAssembler())
    assert result.error is not None and "outside the game's module" in result.error[1]

"""Pro cheats: building, compiling to ordinary cheats, turning on and off.

(architecture-pro "Export pipeline" and "Turning cheats on and off"; spec PR-8.)

- **Compile.** ``compile_cheat(pc)`` makes the ``Cheat`` that My cheats lists and that
  cheat files hold: per entry a cave line then its jump line, or a Bytes/NOP line.
  The cave line is the same on and off (as the guided builder's routines): a file
  turned off must never zero code a game thread may still be running. The editable
  variable becomes the cheat's value (``value_type`` + ``value_at``).
- **Variables** live at the end of the first cave entry's code (``align 4``, then each
  in order). Other caves reach them as ``equ`` symbols.
- **Sources are assembled at module offsets** (see ``asm_text``): a cave's ``org`` is
  its module offset.
- **On/off** (AC-8.4, AC-8.5) go through ``ProWriter``, which logs every write
  (``action=pro_write``; the hook point for R2's journal) and warns once about writes
  crossing an address ending in 0xFF (AC-8.6).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace

import iced_x86 as ix

from cheatmaker.core import cave as cv
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.pro import sites
from cheatmaker.core.pro.asm import AsmResult, Assembler, CaveContext
from cheatmaker.core.pro.model import JMP_REL32_SIZE, ProCheat, ProEntry, Variable
from cheatmaker.core.session import Session
from cheatmaker.core.values import ValueType
from cheatmaker.errors import CheatMakerError, LockFailed, ReadFailed, WriteFailed

log = logging.getLogger(__name__)

DISABLE_WAIT = 0.5
"""Seconds between taking the jumps out and zeroing the caves (AC-8.5): a game thread
inside a cave gets out first."""
FAR = 1 << 30
"""A cave is first assembled this far from its hook, so its size is the worst case."""
TEMPLATE = "pro"
DEFAULT_SOURCE = "original\njmp return\n"
STALE_HINT = "A cave needs assembling again in Pro before this cheat can be used."
FF_WARNING = (
    "A write crosses an address ending in FF: the console may write it in two steps, "
    "so the game could briefly see half of it."
)


class NoRoom(LockFailed):
    """The cave doesn't fit (AC-8.2: "needs N bytes, M free")."""


class SiteChanged(CheatMakerError):
    """A site doesn't hold the bytes recorded for it (AC-8.4); nothing was written."""

    def __init__(self, found: Sequence[tuple[int, bytes, bytes]]) -> None:
        self.found = list(found)  # (address, expected, found)
        addr, want, got = self.found[0]
        super().__init__(
            f"The game's code at {addr:#x} isn't what this cheat expects "
            f"(expected {want.hex(' ')}, found {got.hex(' ')}). Nothing was changed."
        )


# -- variables ------------------------------------------------------------------------
def home(pc: ProCheat) -> ProEntry | None:
    """The cave entry that holds the variables: the first cave."""
    return next((e for e in pc.entries if e.kind == "cave"), None)


def variable_offsets(variables: Sequence[Variable], code_len: int) -> dict[str, int]:
    """Where each variable sits in its cave's bytes (they end the cave)."""
    total = sum(v.vtype.size for v in variables)
    at, out = code_len - total, {}
    for v in variables:
        out[v.name] = at
        at += v.vtype.size
    return out


def record_layout(pc: ProCheat, entry: ProEntry) -> None:
    """Remember where the variables went when ``entry`` (just assembled with
    ``pc.variables`` if it is the home cave) was built."""
    if entry is not home(pc):
        entry.layout = ()
        return
    offs = variable_offsets(pc.variables, len(entry.cave_bytes))
    entry.layout = tuple((v.name, v.vtype.name.lower(), offs[v.name]) for v in pc.variables)


def built_variables(pc: ProCheat) -> dict[str, tuple[ValueType, int]]:
    """Variable name -> (type, offset in the home cave's bytes), as last built."""
    entry = home(pc)
    if entry is None or not entry.cave_bytes:
        return {}
    return {name: (ValueType[vtype.upper()], at) for name, vtype, at in entry.layout}


def layout_current(pc: ProCheat) -> bool:
    """The variable list is the one the home cave was built with (names and types)."""
    entry = home(pc)
    built = [(n, t) for n, t, _at in entry.layout] if entry is not None else []
    return built == [(v.name, v.vtype.name.lower()) for v in pc.variables]


def variable_addresses(pc: ProCheat) -> dict[str, int]:
    """Variable name -> module offset, as the home cave was built; empty while it
    isn't."""
    entry = home(pc)
    if entry is None or entry.cave is None:
        return {}
    return {name: entry.cave + at for name, (_t, at) in built_variables(pc).items()}


# -- compiling ------------------------------------------------------------------------
def _cave_lines(entry: ProEntry, pc: ProCheat) -> list[Location]:
    if entry.cave is None or not entry.cave_bytes:
        raise LockFailed(f"{pc.name}: a cave isn't assembled yet")
    return [
        Location(LocationKind.CODE_PATCH, entry.cave, entry.cave_bytes, entry.cave_bytes),
        Location(LocationKind.CODE_PATCH, entry.offset, entry.on, entry.off),
    ]


def compile_cheat(pc: ProCheat) -> Cheat:
    """The ordinary cheat for ``pc`` (``Cheat.pro`` links back to it). A cheat with a
    stale cave (its bytes no longer match its variables) compiles to no lines, so it
    can't be turned on in My cheats nor saved in a cheat file until it is rebuilt."""
    if not pc.entries:
        raise LockFailed(f"{pc.name}: there is nothing to write")
    if any(e.stale for e in pc.entries):
        return Cheat(name=pc.name, locations=[], template=TEMPLATE, hint=STALE_HINT, pro=pc)
    lines: list[Location] = []
    home_line = None
    for entry in pc.entries:
        if entry.kind == "cave":
            if home_line is None:
                home_line = len(lines)
            lines += _cave_lines(entry, pc)
        else:
            lines.append(Location(LocationKind.CODE_PATCH, entry.offset, entry.on, entry.off))
    value_type = value_at = None
    built = built_variables(pc)
    if pc.editable is not None and home_line is not None and pc.editable in built:
        vtype, at = built[pc.editable]  # where the cave really holds it
        value_type = vtype
        value_at = [(home_line, at + i) for i in range(vtype.size)]
    return Cheat(
        name=pc.name,
        locations=lines,
        template=TEMPLATE,
        value_type=value_type,
        value_at=value_at,
        hint=pc.comment or None,
        pro=pc,
    )


def refresh(cheat: Cheat) -> None:
    """Re-compile ``cheat`` from its Pro source after an edit (keeps its on/off state)."""
    if cheat.pro is None:
        return
    fresh = compile_cheat(cheat.pro)
    cheat.name, cheat.locations, cheat.hint = fresh.name, fresh.locations, fresh.hint
    cheat.value_type, cheat.value_at = fresh.value_type, fresh.value_at


def take_value(cheat: Cheat) -> None:
    """Copy the editable value back into the Pro source when it was changed through the
    ordinary cheat (My cheats' value box, ``Cheat.set_value``)."""
    pc = cheat.pro
    entry = home(pc) if pc is not None else None
    if pc is None or pc.editable is None or entry is None or cheat.value is None:
        return
    var = pc.variable(pc.editable)
    if not layout_current(pc):  # the cave holds another layout: build it first
        log.info("action=pro_take_value id=%s skipped=layout_changed", pc.id)
        return
    try:
        if var.vtype.encode(cheat.value) == var.encoded():  # same bytes: 2.1 stays 2.1
            return
    except (ValueError, OverflowError):
        return
    k = pc.variables.index(var)
    pc.variables[k] = replace(var, value=cheat.value)
    at = built_variables(pc)[var.name][1]
    data = pc.variables[k].encoded()
    entry.cave_bytes = entry.cave_bytes[:at] + data + entry.cave_bytes[at + len(data) :]


# -- project (.cmproj format 5) -----------------------------------------------------------
def store(project, cheats: Iterable[Cheat]) -> bool:
    """Put the Pro sources of ``cheats`` into ``project.pro_cheats`` (added or replaced
    by id; a Pro cheat missing from ``cheats`` stays, only ``forget`` drops one).
    True when anything changed."""
    changed = False
    by_id = {pc.id: k for k, pc in enumerate(project.pro_cheats)}
    for cheat in cheats:
        if cheat.pro is None:
            continue
        take_value(cheat)
        k = by_id.get(cheat.pro.id)
        if k is None:
            by_id[cheat.pro.id] = len(project.pro_cheats)
            project.pro_cheats.append(cheat.pro)
            changed = True
        else:
            changed = changed or project.pro_cheats[k].to_json() != cheat.pro.to_json()
            project.pro_cheats[k] = cheat.pro
    return changed


def forget(project, pro_id: str) -> None:
    project.pro_cheats = [pc for pc in project.pro_cheats if pc.id != pro_id]


def restore(project, cheats: list[Cheat]) -> list[Cheat]:
    """Compiled cheats for the project's Pro cheats that ``cheats`` doesn't hold yet,
    appended to ``cheats``; a Pro cheat that can't compile (an unplaced cave) is
    logged and left out of the list (it stays in the project)."""
    have = {c.pro.id for c in cheats if c.pro is not None}
    added = []
    for pc in project.pro_cheats:
        if pc.id in have:
            continue
        try:
            cheat = compile_cheat(pc)
        except (LockFailed, ValueError) as exc:  # unplaced cave, damaged entry
            log.warning("action=pro_restore id=%s skipped: %s", pc.id, exc)
            continue
        cheats.append(cheat)
        added.append(cheat)
    return added


# -- building entries -------------------------------------------------------------------
def _reader(session: Session) -> Callable[[int, int], bytes]:
    game = session.require_game()
    client = session.require_client()
    return lambda addr, n: client.read(game.pid, addr, n)


def new_cave_entry(session: Session, hook: int, source: str = DEFAULT_SOURCE) -> ProEntry:
    """A cave entry hooked at module offset ``hook`` (not placed or assembled yet).
    Refused with the guided ``hook_span`` message where the jump would cover a call
    or a branch (AC-8.3)."""
    base = session.require_game().module_base
    read = _reader(session)
    span = cv.hook_span(read, base + hook)
    off = read(base + hook, sum(i.len for i in span))
    nops = b"\x90" * len(off)  # placeholder until built: never written unbuilt
    return ProEntry("cave", hook, off, nops, source=source)


def new_nop_entry(session: Session, offset: int) -> ProEntry:
    """NOPs over the one instruction at ``offset`` (instruction-aligned)."""
    base = session.require_game().module_base
    code = _reader(session)(base + offset, 16)
    insn = cv.decode_at(code, base + offset)[0]
    if insn.code == ix.Code.INVALID:
        raise LockFailed("couldn't read the game code there")
    return ProEntry("nop", offset, code[: insn.len])


def context(
    pc: ProCheat, entry: ProEntry, org: int | None = None, module_size: int | None = None
) -> CaveContext:
    """What a cave's source is assembled with: its place, its hook, (for a cave other
    than the home cave) the variables' addresses as symbols, and the module's size
    (bare numbers in the source are module offsets and must be inside it)."""
    where = entry.cave if org is None else org
    if where is None:
        raise LockFailed("the cave isn't placed yet")
    symbols = {} if entry is home(pc) else variable_addresses(pc)
    return CaveContext(where, entry.offset, entry.off, symbols, module_size)


def assemble(
    pc: ProCheat,
    entry: ProEntry,
    assembler: Assembler,
    org: int | None = None,
    module_size: int | None = None,
) -> AsmResult:
    variables = pc.variables if entry is home(pc) else ()
    return assembler.assemble(entry.source, context(pc, entry, org, module_size), variables)


def module_size(session: Session) -> int:
    game = session.require_game()
    return game.module_end - game.module_base


def _round16(n: int) -> int:
    return (n + 15) & ~15


def build(
    session: Session,
    pc: ProCheat,
    entry: ProEntry,
    assembler: Assembler,
    others: Iterable[ProCheat] = (),
    open_rooms: Iterable[sites.Range] = (),
) -> AsmResult:
    """Assemble ``entry``, place its cave the first time, check the cave budget, and
    store the cave bytes and the jump. Returns the assembler's result; on an error the
    entry is unchanged. NoRoom when it doesn't fit (AC-8.2). ``open_rooms``: the code
    lines of the cheats in My cheats (``sites.open_rooms``), never used for a cave.
    When the home cave's variables move, the cheat's other caves are re-assembled;
    one that can't be is marked ``stale`` (it can't be turned on until rebuilt)."""
    if entry.kind != "cave":
        raise ValueError("only a cave entry is assembled")
    size_limit = module_size(session)
    mine = ProCheat(pc.id, pc.name, [e for e in pc.entries if e is not entry])
    reserved = [
        *sites.cave_rooms(others, skip=pc.id),
        *sites.cave_rooms([mine]),
        *open_rooms,
    ]
    before = variable_addresses(pc)
    if entry.cave is None:
        trial = assemble(pc, entry, assembler, entry.offset + FAR, size_limit)
        if not trial.ok:
            return trial
        size = len(trial.code)
        free = sites.free_room(session, reserved)
        if size > free:
            raise NoRoom(f"The cave needs {size} bytes, {free} free.")
        base = session.require_game().module_base
        try:  # room to grow by later edits, like a guided cave's
            addr = cv.find_cave(session, max(_round16(size), cv.CAVE_ROOM), extra_reserved=reserved)
        except LockFailed:
            addr = cv.find_cave(session, _round16(size), extra_reserved=reserved)
        entry.cave = addr - base
        log.info("action=pro_place id=%s cave=%#x size=%d", pc.id, entry.cave, size)
    result = assemble(pc, entry, assembler, module_size=size_limit)
    if not result.ok:
        return result
    room = sites.room_at(session, entry.cave, len(entry.cave_bytes), reserved)
    if len(result.code) > room:
        raise NoRoom(f"The cave needs {len(result.code)} bytes, {room} free.")
    entry.cave_bytes = result.code
    entry.on = cv.jmp_patch(entry.offset, entry.cave, len(entry.off))
    entry.stale = False
    record_layout(pc, entry)
    if entry is home(pc) and variable_addresses(pc) != before:
        _rebuild_users(session, pc, assembler, reserved)
    return result


def _rebuild_users(
    session: Session, pc: ProCheat, assembler: Assembler, reserved: list[sites.Range]
) -> None:
    """Re-assemble the caves other than the home cave at their places (they use the
    variables' addresses); one that fails or no longer fits is marked stale."""
    for e in pc.entries:
        if e.kind != "cave" or e is home(pc) or e.cave is None or not e.cave_bytes:
            continue
        result = assemble(pc, e, assembler, module_size=module_size(session))
        own = [r for r in reserved if r[0] != e.cave]
        if result.ok and len(result.code) <= sites.room_at(session, e.cave, len(e.cave_bytes), own):
            e.cave_bytes, e.stale = result.code, False
        else:
            e.stale = True
            log.warning("action=pro_build id=%s cave=%#x result=stale", pc.id, e.cave)


def build_all(
    session: Session,
    pc: ProCheat,
    assembler: Assembler,
    others: Iterable[ProCheat] = (),
    open_rooms: Iterable[sites.Range] = (),
) -> AsmResult | None:
    """Build every cave, the home cave first (the others use its variables). Returns
    the first failed result, else None."""
    others = list(others)
    for entry in (e for e in pc.entries if e.kind == "cave"):
        result = build(session, pc, entry, assembler, others, open_rooms)
        if not result.ok:
            return result
    return None


# -- writing -----------------------------------------------------------------------------
def crosses_ff(addr: int, size: int) -> bool:
    """True when a write of ``size`` bytes at ``addr`` continues past an address ending
    in 0xFF (NG may split it there)."""
    return size > 1 and (addr & 0xFF) + size > 0x100


class ProWriter:
    """Every Pro write goes through here (architecture-pro step 6 rule)."""

    def __init__(self, session: Session, warn: Callable[[str], None] | None = None) -> None:
        self.session = session
        self.warn = warn
        self._warned = False

    def write(self, addr: int, data: bytes, source: str) -> None:
        game = self.session.require_game()
        if crosses_ff(addr, len(data)):
            log.warning("action=pro_write_ff addr=%#x len=%d source=%s", addr, len(data), source)
            if not self._warned:
                self._warned = True
                if self.warn is not None:
                    self.warn(FF_WARNING)
        log.info("action=pro_write addr=%#x len=%d source=%s", addr, len(data), source)
        self.session.require_client().write(game.pid, addr, data)

    def write_checked(self, addr: int, data: bytes, source: str) -> None:
        """Write, then read back; WriteFailed when the console holds other bytes."""
        self.write(addr, data, source)
        game = self.session.require_game()
        got = self.session.require_client().read(game.pid, addr, len(data))
        if got != data:
            raise WriteFailed(f"{addr:#x} didn't take the new bytes (read {got.hex(' ')})")


@dataclass(frozen=True)
class _Write:
    addr: int
    on: bytes
    off: bytes
    what: str  # "cave", "jump" or "bytes"


def _writes(pc: ProCheat, base: int) -> list[_Write]:
    out = []
    for e in pc.entries:
        if e.kind == "cave":
            if e.cave is None or not e.cave_bytes or len(e.on) < JMP_REL32_SIZE:
                raise LockFailed(f"{pc.name}: a cave isn't assembled yet")
            out.append(_Write(base + e.cave, e.cave_bytes, bytes(len(e.cave_bytes)), "cave"))
            out.append(_Write(base + e.offset, e.on, e.off, "jump"))
        else:
            out.append(_Write(base + e.offset, e.on, e.off, "bytes"))
    order = {"cave": 0, "jump": 1, "bytes": 2}
    return sorted(out, key=lambda w: order[w.what])  # caves, then jumps, then the rest


def enable(
    session: Session, pc: ProCheat, writer: ProWriter, enabled: Iterable[Cheat] = ()
) -> bool:
    """Turn ``pc`` on (AC-8.4). Every site must hold its off-bytes (a cave: zeros, or
    the same cave a cheat file left there); otherwise SiteChanged and nothing is
    written. Then caves, jumps, other entries, each read back. Overlap (AC-8.8) when
    an enabled cheat writes any of the same bytes.

    When every site already holds its on-bytes (the cheat is in the game, e.g. after
    a Refresh or an app restart while the game ran) nothing is written and True is
    returned ("was already on"); otherwise False. Some sites on and some off is still
    SiteChanged (QA BUG-03)."""
    game = session.require_game()
    client = session.require_client()
    if any(e.stale for e in pc.entries):
        raise LockFailed(f"{pc.name}: a cave needs assembling again before it can go on")
    sites.refuse_overlap(pc, enabled, game.module_base)
    writes = _writes(pc, game.module_base)
    changed, before = [], []
    for w in writes:
        found = client.read(game.pid, w.addr, len(w.on))
        before.append(found)
        if found != w.off and not (w.what == "cave" and found == w.on):
            changed.append((w.addr, w.off, found))
    if all(found == w.on for w, found in zip(writes, before, strict=True)):
        log.info("action=pro_enable id=%s result=already_on", pc.id)
        return True
    if changed:
        raise SiteChanged(changed)
    done: list[tuple[_Write, bytes]] = []
    try:
        for w, found in zip(writes, before, strict=True):
            done.append((w, found))
            writer.write_checked(w.addr, w.on, f"{pc.id}:{w.what}")
    except CheatMakerError:
        _undo(writer, pc, done)
        raise
    log.info("action=pro_enable id=%s entries=%d", pc.id, len(pc.entries))
    return False


def is_on(session: Session, pc: ProCheat) -> bool:
    """Every site of ``pc`` holds its on-bytes right now (it runs in the game). A
    stale or unbuilt cheat, or one that can't be read, is not on."""
    game = session.require_game()
    client = session.require_client()
    if any(e.stale for e in pc.entries):
        return False
    try:
        writes = _writes(pc, game.module_base)
        return all(client.read(game.pid, w.addr, len(w.on)) == w.on for w in writes)
    except (LockFailed, ReadFailed) as exc:
        log.info("action=pro_probe id=%s result=unknown reason=%s", pc.id, exc)
        return False


def _undo(writer: ProWriter, pc: ProCheat, done: list[tuple[_Write, bytes]]) -> None:
    """Put back what a failed ``enable`` wrote, last write first (jumps before caves)."""
    for w, found in reversed(done):
        try:
            writer.write(w.addr, found, f"{pc.id}:undo")
        except CheatMakerError as exc:  # keep going: put back as much as we can
            log.warning("action=pro_enable_undo addr=%#x reason=%s", w.addr, exc)


def disable(
    session: Session,
    pc: ProCheat,
    writer: ProWriter,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Turn ``pc`` off (AC-8.5): jumps and other entries back first, then after
    ``DISABLE_WAIT`` the caves are zeroed; every site is read back. (The UI thread
    uses ``restore_sites`` and, ``DISABLE_WAIT`` later, ``clear_caves`` instead, so the
    window doesn't stop for the wait.)"""
    if restore_sites(session, pc, writer):
        sleep(DISABLE_WAIT)
        clear_caves(session, pc, writer)


def restore_sites(session: Session, pc: ProCheat, writer: ProWriter) -> bool:
    """The first half of ``disable``: jumps and other entries back, read back. True
    when caves are left to zero (after ``DISABLE_WAIT``, with ``clear_caves``)."""
    game = session.require_game()
    writes = _writes(pc, game.module_base)
    for w in (w for w in writes if w.what != "cave"):
        writer.write_checked(w.addr, w.off, f"{pc.id}:{w.what}")
    log.info("action=pro_disable id=%s", pc.id)
    return any(w.what == "cave" for w in writes)


def clear_caves(session: Session, pc: ProCheat, writer: ProWriter) -> None:
    """The second half of ``disable``: zero the caves (no jump leads there any more)."""
    game = session.require_game()
    for w in (w for w in _writes(pc, game.module_base) if w.what == "cave"):
        writer.write_checked(w.addr, w.off, f"{pc.id}:cave")
    log.info("action=pro_disable id=%s caves=cleared", pc.id)


# -- live variable edit (AC-8.7) -----------------------------------------------------------
WRITTEN, SAVED = "written", "saved"


def set_variable(
    pc: ProCheat,
    name: str,
    value: float | int,
    assembler: Assembler | None = None,
    *,
    session: Session | None = None,
    writer: ProWriter | None = None,
    on: bool = False,
) -> str:
    """Give variable ``name`` a new value (AC-8.7). Variables sit at fixed offsets at
    the end of the built home cave, so the value's bytes are patched there and nothing
    is re-assembled: a source edited but not built (or refused for lack of room) never
    reaches the cave, and the cave never changes size (QA BUG-06). ``assembler`` isn't
    needed for that and is ignored. Returns ``SAVED`` (the cheat is off: only the
    stored bytes changed) or ``WRITTEN`` (on: the variable's bytes were written live and
    read back). ValueError for a value that doesn't fit."""
    del assembler
    var = pc.variable(name)
    new_var = Variable(var.name, var.vtype, value, var.comment)  # validates the value
    entry = home(pc)
    if entry is None or entry.cave is None or not entry.cave_bytes:
        raise LockFailed(f"{pc.name}: there is no built cave for the variable")
    if not layout_current(pc):
        raise LockFailed(
            f"{pc.name}: the variables changed since the cave was built; build the cave first"
        )
    k = pc.variables.index(var)
    at = built_variables(pc)[name][1]
    data = new_var.encoded()
    if on and (session is None or writer is None):
        raise ValueError("a live rewrite needs the session and the writer")
    pc.variables[k] = new_var
    entry.cave_bytes = entry.cave_bytes[:at] + data + entry.cave_bytes[at + len(data) :]
    if not on:
        return SAVED
    assert session is not None and writer is not None
    base = session.require_game().module_base
    writer.write_checked(base + entry.cave + at, data, pc.id)
    return WRITTEN


__all__ = [
    "DEFAULT_SOURCE",
    "DISABLE_WAIT",
    "FF_WARNING",
    "SAVED",
    "WRITTEN",
    "NoRoom",
    "ProWriter",
    "SiteChanged",
    "assemble",
    "build",
    "build_all",
    "built_variables",
    "clear_caves",
    "compile_cheat",
    "context",
    "crosses_ff",
    "disable",
    "enable",
    "forget",
    "home",
    "is_on",
    "layout_current",
    "module_size",
    "new_cave_entry",
    "new_nop_entry",
    "record_layout",
    "refresh",
    "restore",
    "restore_sites",
    "set_variable",
    "store",
    "take_value",
    "variable_addresses",
    "variable_offsets",
]

"""Anchor hook (architecture v3, option A): let the game's own code say where a value is.

1. **Choose a hook.** A read-write watchpoint on the found value reports every
   instruction that touches it, with the registers. An instruction like
   ``vmovss [r12+0x2AC], xmm1`` gives the object (``r12``) and the field offset
   (``0x2AC``). Readers are preferred: a HUD reads health every frame, so the slot
   fills without taking damage.
2. **Prove the slot.** 16 zero bytes at the end of the game's data area hold the
   object address and a counter. A write watchpoint checks the game never writes
   there (on the console: test C10b).
3. **Install / read / remove.** A capture routine (``cave.capture_code``) in spare
   code bytes, plus a ``jmp`` over the hooked instruction. Both are plain byte
   patches, so they also fit a GoldHEN / CheatRunner file. If they are already in the
   game (CheatRunner loaded them), nothing is patched twice.

Everything is stored as module offsets in the ``.cmproj`` (``project.HookAnchor``).
"""

from __future__ import annotations

import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

import iced_x86 as ix

from cheatmaker.console.client import WatchHit
from cheatmaker.core import cave as cv
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.locker import MAX_INSN, instruction_before, touches_memory, writes_memory
from cheatmaker.core.project import HookAnchor, HookFilter, ObjectSpec, Role
from cheatmaker.core.session import Session
from cheatmaker.errors import FindFailed, LockFailed, ReadFailed

log = logging.getLogger(__name__)

WATCH_SECONDS = 10.0
PROVE_SECONDS = 60.0
"""In-app slot proof. Console test C10b runs it for 10 minutes and a biome change."""
CAPTURE_CAVE_SKIP = 3 * cv.CAVE_ROOM
"""Below the Player-only sampler (skip 0) and Player-only cave (skip CAVE_ROOM)."""

Read = Callable[[int, int], bytes]


@dataclass(frozen=True)
class HookCandidate:
    address: int  # where the instruction starts
    code: bytes
    text: str
    hits: int
    role: Role
    reg: str  # object register, e.g. "r12"
    disp: int
    confirmed: bool  # a hit's registers showed reg + disp == the value's address

    def object_for(self, value_addr: int) -> int:
        return value_addr - self.disp


_GPR64 = frozenset(
    getattr(ix.Register, n)
    for n in ("RAX", "RBX", "RCX", "RDX", "RSI", "RDI", "RBP", *(f"R{i}" for i in range(8, 16)))
)
"""Object registers we accept: 64-bit, not RSP (the stack) or RIP."""


def _reg_value(name: str) -> int:
    return getattr(ix.Register, name.upper())


def candidates_from_hits(hits: list[WatchHit], address: int, read: Read) -> list[HookCandidate]:
    """The instructions behind ``hits`` that touch ``[reg + disp]`` (one 64-bit object
    register, no index, no fs/gs segment, disp >= 0 so ``reg`` is the object's start).
    Best first: confirmed by the registers, 5+ bytes long (patched alone, see
    cave.hook_span), readers, then most hits."""
    by_rip: dict[int, list[WatchHit]] = {}
    for h in hits:
        by_rip.setdefault(h.rip, []).append(h)
    out: list[HookCandidate] = []
    for rip, group in by_rip.items():
        try:
            before = read(rip - MAX_INSN, MAX_INSN)
        except ReadFailed:
            log.warning("action=hook_pick can't read code before rip=%#x", rip)
            continue
        found = instruction_before(before, rip, want=touches_memory)
        if found is None:
            log.info("action=hook_pick no memory access ends at rip=%#x", rip)
            continue
        start, code, text = found
        insn = ix.Decoder(64, code, ip=start).decode()
        base, index = insn.memory_base, insn.memory_index
        disp = insn.memory_displacement
        if disp >= 1 << 63:
            disp -= 1 << 64
        if (
            base not in _GPR64
            or index != ix.Register.NONE
            or insn.memory_segment in (ix.Register.FS, ix.Register.GS)
            or disp < 0
        ):
            log.info("action=hook_pick skip %s (not [reg+disp])", text)
            continue
        reg = cv.Target(base, disp).base_name
        confirmed = any(
            (v := h.reg(reg)) is not None and (v + disp) & ((1 << 64) - 1) == address for h in group
        )
        role: Role = "writer" if writes_memory(insn) else "reader"
        out.append(HookCandidate(start, code, text, len(group), role, reg, disp, confirmed))
    out.sort(
        key=lambda c: (
            not c.confirmed,
            len(c.code) < cv.JMP_LEN,
            c.role != "reader",
            -c.hits,
            c.address,
        )
    )
    return out


def find_hooks(
    session: Session, address: int, size: int, seconds: float = WATCH_SECONDS
) -> list[HookCandidate]:
    """Watch ``address`` for reads and writes while the player plays."""
    game = session.require_game()
    client = session.require_client()
    hits = client.watch(game.pid, address, size, "access", seconds)
    log.info("action=hook_watch address=%#x hits=%d", address, len(hits))
    read = lambda a, n: client.read(game.pid, a, n)  # noqa: E731
    cands = [
        c
        for c in candidates_from_hits(hits, address, read)
        if game.module_base <= c.address < game.module_end
    ]
    for c in cands:
        log.info(
            "action=hook_candidate at=%#x insn=%r role=%s hits=%d confirmed=%s",
            c.address,
            c.text,
            c.role,
            c.hits,
            c.confirmed,
        )
    return cands


def object_vtable(session: Session, obj: int) -> int | None:
    """The object's first 8 bytes as a module offset, if they point into the module
    (a C++ vtable). None otherwise."""
    game = session.require_game()
    try:
        (vt,) = struct.unpack("<Q", session.require_client().read(game.pid, obj, 8))
    except ReadFailed:
        return None
    if game.module_base <= vt < game.module_end:
        return vt - game.module_base
    return None


# -- the routine ----------------------------------------------------------------------
def _read(session: Session) -> Read:
    game = session.require_game()
    client = session.require_client()
    return lambda a, n: client.read(game.pid, a, n)


def plan_hook(
    session: Session,
    cand: HookCandidate,
    vtable: int | None,
    fallback: HookFilter | None = None,
) -> HookAnchor:
    """Where the routine and slot go, and how it tells the object apart. The slot is
    not proven yet (see prove_slot). Only an instruction of 5+ bytes is hooked, so the
    jump never covers a following ``ret`` or a place other code jumps to. Raises
    LockFailed."""
    game = session.require_game()
    read = _read(session)
    span = cv.hook_span(read, cand.address, single=True)
    orig = b"".join(read(i.ip, i.len) for i in span)
    filt = HookFilter("vtable") if vtable is not None else fallback or HookFilter("none")
    # Room for two routines: keep-value (PC off) below, capture above (see keep_cave).
    cave = cv.find_cave(session, 2 * cv.CAVE_ROOM, skip=CAPTURE_CAVE_SKIP) + cv.CAVE_ROOM
    slot = cv.find_slot(session)
    try:
        sig = up.make_signature(read(cand.address, up.SIG_MAX + MAX_INSN), cand.address).text()
    except (ReadFailed, ValueError):  # near the end of the code: no signature, still usable
        sig = ""
    anchor = HookAnchor(
        cand.address - game.module_base,
        cand.text,
        orig,
        cand.reg,
        cand.disp,
        cand.role,
        filt,
        cave - game.module_base,
        slot - game.module_base,
        sig,
    )
    log.info(
        "action=hook_plan hook=%#x cave=%#x slot=%#x filter=%s",
        cand.address,
        cave,
        slot,
        filt.kind,
    )
    return anchor


def keep_cave(anchor: HookAnchor) -> int:
    """Module offset of the keep-value (PC-off) routine: its own room just below the
    capture routine, so turning it on never rewrites code the game may be running."""
    return anchor.cave - cv.CAVE_ROOM


def _build(
    session: Session, obj: ObjectSpec, anchor: HookAnchor, value: bytes | None = None
) -> tuple[int, bytes, int, bytes]:
    """(cave address, routine bytes, hook address, jump bytes)."""
    game = session.require_game()
    base = game.module_base
    site = base + anchor.hook
    cave_off = anchor.cave if value is None else keep_cave(anchor)
    span = cv.decode_at(anchor.orig, site)
    if sum(i.len for i in span) != len(anchor.orig) or any(i.code == ix.Code.INVALID for i in span):
        raise FindFailed("The saved game code doesn't decode. Press Find it next time again.")
    target = cv.Target(_reg_value(anchor.reg), anchor.disp)
    f = anchor.filter
    try:
        code = cv.capture_code(
            span,
            target,
            base + anchor.slot,
            base + cave_off,
            f.kind,
            vtable=None if obj.vtable is None else base + obj.vtable,
            field=f.off,
            field_value=f.value,
            value=value,
        )
    except (LockFailed, ValueError, RuntimeError) as exc:  # iced raises ValueError
        raise FindFailed(f"Can't build the find-it-again hook: {exc}.") from exc
    if len(code) > cv.CAVE_ROOM:
        raise FindFailed("The find-it-again hook doesn't fit the spare space.")
    try:
        jmp = cv.jmp_patch(site, base + cave_off, len(anchor.orig))
    except LockFailed as exc:
        raise FindFailed(f"Can't build the find-it-again hook: {exc}.") from exc
    return base + cave_off, code, site, jmp


def hook_cheat(session: Session, obj: ObjectSpec, name: str, value: bytes | None = None) -> Cheat:
    """The routine and its jump as two CODE_PATCH lines, for a GoldHEN / CheatRunner
    file (D14, AC-13.1). With ``value`` it also keeps the field at that value for the
    object it captures (one routine: never two jumps on one instruction), in its own
    spare room (``keep_cave``), so switching between the two only re-aims the jump."""
    anchor = obj.hook
    if anchor is None:
        raise FindFailed("This value has no game-code hook to save.")
    game = session.require_game()
    cave, code, site, jmp = _build(session, obj, anchor, value)
    base = game.module_base
    if value is not None and _read(session)(cave, len(code)) not in (bytes(len(code)), code):
        raise FindFailed("There's no spare room in the game's code for this cheat.")
    return Cheat(
        name=name,
        locations=[
            # Off leaves the routine in place: a game thread may be inside it when
            # the jump goes back, so only the jump line really toggles.
            Location(LocationKind.CODE_PATCH, cave - base, code, code),
            Location(LocationKind.CODE_PATCH, site - base, jmp, anchor.orig),
        ],
        template="lock" if value is not None else None,
        hint=(
            "Keeps your value; everyone else is unchanged."
            if value is not None
            else "Lets Cheat Maker find your values again right away. Does nothing on its own."
        ),
    )


def is_installed(session: Session, obj: ObjectSpec) -> bool:
    anchor = obj.hook
    if anchor is None:
        return False
    cave, code, site, jmp = _build(session, obj, anchor)
    read = _read(session)
    return read(site, len(jmp)) == jmp and read(cave, len(code)) == code


def install(session: Session, obj: ObjectSpec) -> str:
    """Put the routine in the game. Returns "installed", or "already" when the exact
    bytes are there (e.g. CheatRunner loaded them): then nothing is written."""
    anchor = obj.hook
    if anchor is None:
        raise FindFailed("This value has no game-code hook.")
    game = session.require_game()
    client = session.require_client()
    read = _read(session)
    cave, code, site, jmp = _build(session, obj, anchor)
    now_site, now_cave = read(site, len(jmp)), read(cave, len(code))
    if now_site == jmp and now_cave == code:
        log.info("action=hook_install hook=%#x already in the game", site)
        return "already"
    if now_site != anchor.orig:
        raise FindFailed(
            "Another cheat already changes the game's code where Cheat Maker looks for "
            f"{obj.label}. Turn that cheat off, then try again."
        )
    slot = game.module_base + anchor.slot
    if now_cave != code:  # our routine left from an earlier remove is fine as it is
        if now_cave != bytes(len(code)):
            raise FindFailed("The spare space in the game's code is in use. Restart the game.")
        if read(slot, cv.SLOT_SIZE) != bytes(cv.SLOT_SIZE):
            raise FindFailed("The game is using the spare memory Cheat Maker needs. Restart it.")
        client.write(game.pid, cave, code)  # the routine first, then the jump into it
    # UNVERIFIED (C10): the 5-byte jump is not written atomically; a thread could run
    # a torn instruction at the site in that instant (same as Lock it / Player only).
    client.write(game.pid, site, jmp)
    log.info("action=hook_install hook=%#x cave=%#x slot=%#x", site, cave, slot)
    return "installed"


def remove(session: Session, obj: ObjectSpec, sleep: Callable[[float], None] = time.sleep) -> None:
    """Undo install: put the original instruction back and clear the slot. The routine's
    bytes stay (a thread preempted inside it, or returning into it, must still find
    valid code); the next install reuses them. Leaves the code alone if it isn't ours."""
    anchor = obj.hook
    if anchor is None:
        return
    game = session.require_game()
    client = session.require_client()
    read = _read(session)
    _cave, _code, site, jmp = _build(session, obj, anchor)
    if read(site, len(jmp)) != jmp:
        return
    client.write(game.pid, site, anchor.orig)
    sleep(0.5)  # let the last pass through the routine finish before clearing the slot
    client.write(game.pid, game.module_base + anchor.slot, bytes(cv.SLOT_SIZE))
    log.info("action=hook_remove hook=%#x", site)


def read_slot(session: Session, anchor: HookAnchor) -> tuple[int, int]:
    """(object address, how many times the routine captured it). (0, 0) = not yet."""
    game = session.require_game()
    raw = session.require_client().read(game.pid, game.module_base + anchor.slot, cv.SLOT_SIZE)
    obj, count = struct.unpack("<QQ", raw)
    return obj, count


def prove_slot(session: Session, slot: int, seconds: float = PROVE_SECONDS) -> None:
    """Check the game never writes the 16 slot bytes: they are zero, a write
    watchpoint on the first 8 sees nothing for ``seconds``, and all 16 are still zero
    after (one hardware watchpoint is used, so the counter half is checked by value).
    Raises FindFailed."""
    game = session.require_game()
    client = session.require_client()
    if client.read(game.pid, slot, cv.SLOT_SIZE) != bytes(cv.SLOT_SIZE):
        raise FindFailed("The spare memory Cheat Maker wanted to use isn't free.")
    hits = client.watch(game.pid, slot, 8, "write", seconds)
    still_zero = client.read(game.pid, slot, cv.SLOT_SIZE) == bytes(cv.SLOT_SIZE)
    log.info(
        "action=slot_prove slot=%#x seconds=%.0f writes=%d zero=%s",
        slot,
        seconds,
        len(hits),
        still_zero,
    )
    if hits or not still_zero:
        raise FindFailed(
            "The game uses the spare memory Cheat Maker wanted, so the fast way to find "
            "this value again can't be used."
        )


__all__ = [
    "HookCandidate",
    "candidates_from_hits",
    "find_hooks",
    "hook_cheat",
    "install",
    "is_installed",
    "keep_cave",
    "object_vtable",
    "plan_hook",
    "prove_slot",
    "read_slot",
    "remove",
]

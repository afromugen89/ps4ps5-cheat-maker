"""Reference points: an AOB signature per Pro entry, and bringing Pro cheats to a new
game version (spec PR-9; architecture-pro step 9).

Signatures follow the updater's rules (``updater.make_signature``: whole instructions,
jump distances and RIP-relative addresses as ``??``, 16 to 96 bytes, grown until it
matches exactly once in the game's code; architecture-pro "Spec refinements"). They
are learned from the game's own bytes (the entries' off-bytes laid over the live code),
so it works with the cheats on or off.

Bring them over (AC-9.2) finds every entry by its signature (or, with none, by its
off-bytes alone if they are long enough: "found from original bytes only"), places each
cave again in the new version's spare code and **re-assembles its source** there, with
the new hook and the new covered instructions. Jumps in Bytes entries are re-aimed with
the updater's rules. A cave whose code reaches game code or data the app can't follow
fails its cheat, as in the updater: nothing is guessed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

import iced_x86 as ix

from cheatmaker.core import cave as cv
from cheatmaker.core import updater as up
from cheatmaker.core.pro import compile as pro_compile
from cheatmaker.core.pro.asm import Assembler, AssemblerNotFound
from cheatmaker.core.pro.model import ProCheat, ProEntry

log = logging.getLogger(__name__)

OK = "ok"
NOT_UNIQUE = "not unique"
NOT_CODE = "not in the game's code"
Range = tuple[int, int]


# -- learning ------------------------------------------------------------------------------
def overlay(cheats: Iterable[ProCheat]) -> list[tuple[int, bytes]]:
    """The game's own bytes under every entry: off-bytes at the sites, zeros over caves
    (for ``CodeImage.overlay``)."""
    out: list[tuple[int, bytes]] = []
    for pc in cheats:
        for e in pc.entries:
            out.append((e.offset, e.off))
            if e.kind == "cave" and e.cave is not None and e.cave_bytes:
                out.append((e.cave, bytes(len(e.cave_bytes))))
    return out


def matches_once(entry: ProEntry, image: up.CodeImage) -> bool:
    """The entry's AOB matches exactly once, at its own site."""
    if not entry.aob:
        return False
    try:
        sig = up.Signature.from_text(entry.aob)
    except ValueError:
        return False
    return image.find(sig, 2) == [entry.offset]


def learn(cheats: Iterable[ProCheat], image: up.CodeImage) -> dict[tuple[str, int], str]:
    """Give every entry an AOB that matches once at its site (AC-9.1). An AOB that
    already does is kept (it may be the player's own). Returns (cheat id, entry index)
    -> ``OK``, ``NOT_UNIQUE`` (no unique signature within the size limit: the AOB is
    cleared) or ``NOT_CODE``. ``image`` is the live code; it is cleaned here."""
    cheats = list(cheats)
    clean = image.overlay(overlay(cheats))
    status: dict[tuple[str, int], str] = {}
    for pc in cheats:
        for k, entry in enumerate(pc.entries):
            key = (pc.id, k)
            if matches_once(entry, clean):
                status[key] = OK
                continue
            if not clean.contains(entry.offset, len(entry.off)):
                status[key] = NOT_CODE
                continue
            sig = up.learn_at(clean, [(entry.offset, len(entry.off))]).get(entry.offset)
            entry.aob = sig.text() if sig is not None else ""
            status[key] = OK if sig is not None else NOT_UNIQUE
            log.info(
                "action=pro_learn_aob id=%s entry=%d offset=%#x result=%s",
                pc.id,
                k,
                entry.offset,
                status[key].replace(" ", "_"),
            )
    return status


def needs_learning(cheats: Iterable[ProCheat]) -> bool:
    """Decided without reading the game: some entry has no AOB yet."""
    return any(not e.aob for pc in cheats for e in pc.entries)


def copy_aobs(into: Iterable[ProCheat], learned: Iterable[ProCheat]) -> int:
    """Copy AOBs learned on copies back (by cheat id, entry index and site). Returns
    how many changed."""
    got = {(pc.id, k, e.offset): e.aob for pc in learned for k, e in enumerate(pc.entries) if e.aob}
    n = 0
    for pc in into:
        for k, e in enumerate(pc.entries):
            aob = got.get((pc.id, k, e.offset))
            if aob is not None and aob != e.aob:
                e.aob = aob
                n += 1
    return n


# -- bringing over -------------------------------------------------------------------------
class _Fail(Exception):
    pass


@dataclass
class BringReport:
    moved: list[str] = field(default_factory=list)
    guessed: list[str] = field(default_factory=list)  # found by original bytes only
    failed: dict[str, str] = field(default_factory=dict)  # name -> "can't find ..." etc.


def _find(entry: ProEntry, image: up.CodeImage) -> tuple[int, bytes, bool]:
    """(new site, its bytes now, guessed) with the updater's rules."""
    sig = None
    if entry.aob:
        try:
            sig = up.Signature.from_text(entry.aob)
        except ValueError:
            sig = None
    guessed = sig is None
    if sig is None:
        if len(entry.off) < up.FALLBACK_MIN:
            raise _Fail(f"can't find the code at +{entry.offset:X} (no signature)")
        sig = up.Signature(entry.off, b"\x01" * len(entry.off))
    hits = image.find(sig, 2)
    if not hits:
        raise _Fail(f"can't find the code at +{entry.offset:X} in this version")
    if len(hits) > 1:
        raise _Fail(f"the code at +{entry.offset:X} is in more than one place")
    now = image.read(hits[0], len(entry.off))
    head = up.Signature(sig.data[: len(entry.off)], sig.mask[: len(entry.off)])
    if len(now) != len(entry.off) or not head.matches(now):
        raise _Fail(f"the code at +{entry.offset:X} changed in this version")
    return hits[0], now, guessed


def _targets(code: bytes, ip: int) -> list[int]:
    out = []
    for insn in ix.Decoder(64, code, ip=ip):
        if insn.is_ip_rel_memory_operand:
            out.append(insn.ip_rel_memory_address)
        if insn.op_count and insn.op_kind(0) in (ix.OpKind.NEAR_BRANCH64, ix.OpKind.NEAR_BRANCH32):
            out.append(insn.near_branch_target)
    return out


def _check_reach(pc: ProCheat, entry: ProEntry, sites: list[Range]) -> None:
    """Every target of the new cave is inside the cheat (its caves, its sites up to
    ``return``) or one the covered instructions use themselves."""
    assert entry.cave is not None
    allowed = set(_targets(entry.off, entry.offset))
    inside = [(e.cave, e.cave + len(e.cave_bytes)) for e in pc.entries if e.cave is not None]
    for t in _targets(entry.cave_bytes, entry.cave):
        if t in allowed or any(lo <= t <= hi for lo, hi in [*inside, *sites]):
            continue
        raise _Fail(f"its cave uses game code or data at +{t:X} the app can't follow")


def _place(
    pc: ProCheat,
    entry: ProEntry,
    image: up.CodeImage,
    assembler: Assembler,
    rooms: list[Range],
    skip: int,
) -> None:
    trial = pro_compile.assemble(pc, entry, assembler, org=entry.offset + pro_compile.FAR)
    if not trial.ok:
        line, msg = trial.error  # type: ignore[misc]
        raise _Fail(f"its cave doesn't assemble here (line {line}: {msg})")
    size = (len(trial.code) + 15) & ~15
    try:
        try:
            cave = image.zero_run(max(size, cv.CAVE_ROOM), reserved=rooms, skip=skip)
        except up.UpdateError:
            cave = image.zero_run(size, reserved=rooms, skip=skip)
    except up.UpdateError as exc:
        raise _Fail(str(exc)) from None
    entry.cave = cave
    result = pro_compile.assemble(pc, entry, assembler)
    if not result.ok:
        line, msg = result.error  # type: ignore[misc]
        raise _Fail(f"its cave doesn't assemble here (line {line}: {msg})")
    entry.cave_bytes = result.code
    entry.on = cv.jmp_patch(entry.offset, cave, len(entry.off))
    rooms.append((cave, cave + max(size, len(result.code))))


def bring_one(
    pc: ProCheat,
    image: up.CodeImage,
    assembler: Assembler | None,
    rooms: list[Range],
    skip: int = cv.BUILD_CAVE_SKIP,
) -> tuple[ProCheat, bool]:
    """A copy of ``pc`` for the version whose code is ``image`` (and whether a site
    was found by its original bytes only). ``rooms`` (module ranges) is where new caves
    can't go; the caves placed here are added to it. Raises _Fail."""
    new = ProCheat.from_json(pc.to_json())
    moves: list[tuple[int, int, int]] = []  # old start, old end (incl.), delta
    guessed = False
    for old, entry in zip(pc.entries, new.entries, strict=True):
        hit, now, was_guess = _find(old, image)
        guessed = guessed or was_guess
        moves.append((old.offset, old.offset + len(old.off), hit - old.offset))
        entry.offset, entry.off = hit, now
        if entry.kind == "cave":
            entry.cave, entry.cave_bytes = None, b""
    caves = [e for e in new.entries if e.kind == "cave"]
    if caves and assembler is None:
        raise _Fail("Assembler not found")
    try:
        for entry in caves:  # the home cave first: the others use its variables
            _place(new, entry, image, assembler, rooms, skip)  # type: ignore[arg-type]
    except AssemblerNotFound as exc:
        raise _Fail(str(exc)) from None
    sites = [(e.offset, e.offset + len(e.off)) for e in new.entries]
    for entry in caves:
        _check_reach(new, entry, sites)
    for old, entry in zip(pc.entries, new.entries, strict=True):
        if old.kind == "cave" and old.cave is not None and entry.cave is not None:
            moves.append((old.cave, old.cave + len(old.cave_bytes), entry.cave - old.cave))

    def move(target: int) -> int | None:
        for start, end, delta in moves:
            if start <= target <= end:
                return target + delta
        return None

    for old, entry in zip(pc.entries, new.entries, strict=True):
        if entry.kind != "cave":
            try:
                entry.on = up.reaim(old.on, old.offset, entry.offset, move)
            except up.UpdateError as exc:
                raise _Fail(str(exc)) from None
    learn([new], image)
    return new, guessed


def bring_over(
    cheats: Iterable[ProCheat],
    image: up.CodeImage,
    assembler: Assembler | None,
    reserved: Iterable[Range] = (),
    skip: int = cv.BUILD_CAVE_SKIP,
) -> tuple[list[ProCheat], BringReport]:
    """The Pro cheats that could be brought to the running version (AC-9.2), and the
    report: each cheat is moved whole, or listed with why not."""
    rooms = list(reserved)
    out: list[ProCheat] = []
    report = BringReport()
    for pc in cheats:
        mark = len(rooms)
        try:
            new, guessed = bring_one(pc, image, assembler, rooms, skip)
        except _Fail as exc:
            del rooms[mark:]  # a failed cheat's caves free their room again
            report.failed[pc.name] = str(exc)
            log.info("action=pro_carry id=%s result=failed reason=%s", pc.id, exc)
            continue
        out.append(new)
        report.moved.append(pc.name)
        if guessed:
            report.guessed.append(pc.name)
        log.info("action=pro_carry id=%s result=moved guessed=%s", pc.id, guessed)
    return out, report


__all__ = [
    "NOT_CODE",
    "NOT_UNIQUE",
    "OK",
    "BringReport",
    "bring_one",
    "bring_over",
    "copy_aobs",
    "learn",
    "matches_once",
    "needs_learning",
    "overlay",
]

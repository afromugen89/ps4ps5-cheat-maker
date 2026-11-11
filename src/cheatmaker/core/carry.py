"""Bring your notes to a new game version (docs/spec-game-update.md, design
docs/architecture-game-update.md).

After a game update the notes (``.cmproj``) of the old version are copied to the new
version and every value's new place is found:

* **Hooks** (found by the game's code): the hooked instruction is looked for in the new
  code by its fingerprint, then by the fingerprint with the field number left open (the
  field may move inside the object), then, with no checked fingerprint, by the original
  bytes alone. Its number is the field's new offset; the other fields move with it.
  A new spare room and slot are picked and checked as Find it next time does, and the
  object's class is learned again from the objects the instruction touches.
* **Static values and memory paths**: tried as they are.
* **Fixed code parts** of combined cheats: moved with the v2 rules (``updater``).

The old notes are only read. Nothing is written to the game before ``place_hook``.
Each live step is one function, run by the UI in a worker thread; a step that needs
the player returns ``NeedPick``. Every brought-over object is marked ``check`` until the
player says Yes (spec AC-U8).

Reasons here are plain words for the summary ("Not brought over: Shield (...)").
"""

from __future__ import annotations

import logging
import re
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Literal

import iced_x86 as ix

from cheatmaker.console.client import ProgressFn
from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import cave as cv
from cheatmaker.core import pointers as ptr
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.core.locker import MAX_INSN, Writer, insn_text, writes_memory
from cheatmaker.core.project import (
    ChainAnchor,
    HookAnchor,
    HookFilter,
    ObjectSpec,
    Project,
    ProjectCheat,
    ProjectPart,
    StaticAnchor,
    VtableAnchor,
)
from cheatmaker.core.session import Session
from cheatmaker.errors import FindFailed, LockFailed, ProjectFileError, ReadFailed, ScanCancelled

log = logging.getLogger(__name__)

# -- reasons (plain words, they follow the cheat's name in the summary) ---------------
CODE_CHANGED = "the game's code changed where {label} was found"
NOTHING_TOUCHED = "nothing in the game touched {label} while we watched"
NOT_FOUND = "it couldn't be found in this version"
DATA_VALUE = "it changes a value in the game's data, which moves with an update"
OTHER_MODULE = "it changes another part of the game"
PART_FAILED = "part {part} couldn't be found ({why}); make it again"
NO_OBJECT = "its value isn't in the notes any more"
NOT_PICKED = "none of the places was picked"
HOOK_FILE_FIRST = "bring your notes over first (My cheats)"
PC_OFF_AGAIN = "make the PC-off cheat again (Find it next time)"
FIELDS_MOVED = "its numbers moved in a way that can't be followed"
NO_ROOM = "there was no spare room in the game's memory for it"
ROOM_USED = "the game used the spare memory it needs; restart the game and try again"
NOT_PUT_IN = "it couldn't be put in the game; turn other cheats off and try again"
CONFIRM_FIRST = "confirm {label} in My cheats first, then update again"
# the new hook is in but the game hasn't run that code yet (a sentence of its own)
WAITING = (
    "{label}: brought over, not seen yet; take a hit (or change it in the game) "
    "and it will be found."
)

HookHow = Literal["exact", "loose", "original"]


# -- versions and sources ---------------------------------------------------------------
_TOKEN = re.compile(r"\d+|[^\W\d_]+")


def version_key(v: str) -> tuple:
    """Sort key for game versions: numbers as numbers (``1.05`` = ``01.05``,
    ``03.000.005`` < ``03.001.000``), other text as text."""
    return tuple(
        # isdecimal, not isdigit: "²" is a digit to isdigit() but int() refuses it
        (0, int(t)) if t.isdecimal() else (1, t.casefold())
        for t in _TOKEN.findall(v.strip())
    )


def usable_version(v: str) -> bool:
    """A version notes can be brought to or from (not empty, not "unknown")."""
    return bool(version_key(v)) and v.strip().casefold() != "unknown"


@dataclass
class Source:
    """Old notes to offer: ``newer`` when only notes for a newer version exist."""

    project: Project
    newer: bool


def _other_notes(title_id: str, version: str) -> list[Project]:
    """Readable notes for ``title_id`` with another version and at least one object."""
    if not usable_version(version) or not title_id:
        return []
    folder = pj.project_path(title_id, version).parent
    if not folder.is_dir():
        return []
    want = version_key(version)
    out = []
    for path in sorted(folder.glob(f"{title_id.upper()}_*{pj.EXTENSION}")):
        try:
            p = pj.load(path)
        except ProjectFileError as exc:
            log.info("action=carry_source skip=%s reason=%s", path.name, exc)
            continue
        if p is None or p.title_id.upper() != title_id.upper() or not p.objects:
            continue
        if not usable_version(p.version) or version_key(p.version) == want:
            continue
        out.append(p)
    return out


def find_source(title_id: str, version: str) -> Source | None:
    """The notes to bring over: the closest older version, else the closest newer one."""
    want = version_key(version)
    notes = _other_notes(title_id, version)
    older = [p for p in notes if version_key(p.version) < want]
    if older:
        return Source(max(older, key=lambda p: version_key(p.version)), newer=False)
    newer = [p for p in notes if version_key(p.version) > want]
    if newer:
        return Source(min(newer, key=lambda p: version_key(p.version)), newer=True)
    return None


def list_sources(title_id: str, version: str) -> list[Project]:
    """Every other version's notes for Advanced, older ones first (closest first),
    then newer ones (closest first)."""
    want = version_key(version)
    notes = _other_notes(title_id, version)
    older = sorted(
        (p for p in notes if version_key(p.version) < want),
        key=lambda p: version_key(p.version),
        reverse=True,
    )
    newer = sorted(
        (p for p in notes if version_key(p.version) > want), key=lambda p: version_key(p.version)
    )
    return older + newer


# -- the hooked instruction in the new code ---------------------------------------------
class CarryFailed(FindFailed):
    """One value or cheat couldn't be brought over; ``reason`` is plain words."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class HookSite:
    """Where the hooked instruction is in the new code (module offset)."""

    offset: int
    code: bytes
    text: str
    disp: int  # the field's offset in the new version
    how: HookHow


def _signed(disp: int) -> int:
    return disp - (1 << 64) if disp >= 1 << 63 else disp


def _same_kind(insn: ix.Instruction, orig: ix.Instruction, hook: HookAnchor) -> bool:
    """Same operation and operand form, ``[hook.reg + disp]`` with disp >= 0, same role."""
    if insn.code == ix.Code.INVALID or insn.code != orig.code:
        return False
    try:
        reg = getattr(ix.Register, hook.reg.upper())
    except AttributeError:
        return False
    role = "writer" if writes_memory(insn) else "reader"
    return (
        insn.memory_base == reg
        and insn.memory_index == ix.Register.NONE
        and _signed(insn.memory_displacement) >= 0
        and role == hook.role
    )


def _signature(hook: HookAnchor) -> up.Signature | None:
    if not hook.signature.strip():
        return None
    try:
        return up.Signature.from_text(hook.signature)
    except ValueError:
        log.info("action=carry_hook bad signature %r", hook.signature)
        return None


def find_hook_site(hook: HookAnchor, image: up.CodeImage, label: str = "it") -> HookSite:
    """The hooked instruction in the new code (spec AC-U4): the fingerprint, then the
    fingerprint with the field number open, then (no checked fingerprint) the original
    bytes alone. Each must match exactly once and be the same kind of instruction.
    Raises CarryFailed."""
    failed = CarryFailed(CODE_CHANGED.format(label=label))
    sig = _signature(hook)
    hit: int | None = None
    how: HookHow = "exact"
    seen_any = False
    tries: list[tuple[up.Signature | None, HookHow]] = [(sig, "exact")]
    tries.append((up.loose(sig) if sig is not None else None, "loose"))
    for s, kind in tries:
        if s is None:
            continue
        hits = image.find(s, 2)
        seen_any = seen_any or bool(hits)
        if len(hits) == 1:
            hit, how = hits[0], kind
            break
    if hit is None:
        if seen_any or (sig is not None and hook.sig_unique) or len(hook.orig) < up.FALLBACK_MIN:
            raise failed
        hits = image.find(up.Signature(hook.orig, b"\x01" * len(hook.orig)), 2)
        if len(hits) != 1:
            raise failed
        hit, how = hits[0], "original"
    orig = ix.Decoder(64, hook.orig, ip=hook.hook).decode()
    insn = ix.Decoder(64, image.read(hit, MAX_INSN), ip=hit).decode()
    if not _same_kind(insn, orig, hook):
        log.info("action=carry_hook site=%#x not the same kind of instruction", hit)
        raise failed
    if insn.len < 5:  # the hook's 5-byte jump would cover the next instruction
        log.info("action=carry_hook site=%#x instruction too short (%d)", hit, insn.len)
        raise failed
    return HookSite(
        hit,
        image.read(hit, insn.len),
        insn_text(insn),
        _signed(insn.memory_displacement),
        how,
    )


# -- the new notes' objects -------------------------------------------------------------
def carried_object(obj: ObjectSpec) -> ObjectSpec:
    """A copy for the new notes: marked "check it"; memory paths count as new."""
    new = ObjectSpec.from_json(obj.to_json())
    new.check = True
    for a in new.anchors_of(ChainAnchor):
        a.runs_seen = 1
    return new


def shift_fields(obj: ObjectSpec, shift: int) -> list[str]:
    """Every field moves like the hooked field did (spec AC-U4 default). A field that
    would land before the object's start stays where it was; their names are returned
    (the object can't be trusted then)."""
    stuck = []
    for f in obj.fields:
        if f.off + shift < 0:
            stuck.append(f.name)
        else:
            f.off += shift
    return stuck


def reset_class(obj: ObjectSpec) -> None:
    """Forget the class (it moved with the update): no memory search, normal ranges
    around the last value. It is learned again at the player's first Yes."""
    if obj.vtable is not None:
        obj.relearn_class = True
    obj.vtable = None
    obj.anchors = [a for a in obj.anchors if not isinstance(a, VtableAnchor)]
    for f in obj.fields:
        if f.range is not None:
            f.range = rf.default_range(f.vtype, f.last if f.last is not None else 0)


# -- the plan, then the new notes -------------------------------------------------------
ItemKind = Literal["hook", "static", "chain", "none"]


@dataclass
class Item:
    """One object to bring over: ``obj`` is the new copy, ``old`` the old notes' one."""

    obj: ObjectSpec
    old: ObjectSpec
    kind: ItemKind
    site: HookSite | None = None
    reason: str = ""  # why its hook couldn't come over (it may still be found otherwise)
    placed: bool = False  # its new hook went in (slot proven): kept even if not seen yet

    @property
    def shift(self) -> int:
        hook = self.old.hook
        return 0 if self.site is None or hook is None else self.site.disp - hook.disp


@dataclass
class Plan:
    """What ``prepare`` found without writing anything. ``parts``: per old combined
    cheat (index into ``old.cheats``) its moved parts, or why they couldn't move.
    ``reserved``: module ranges new rooms must stay out of (grows as hooks go in)."""

    old: Project
    new: Project
    image: up.CodeImage | None
    items: list[Item] = field(default_factory=list)
    parts: dict[int, list[ProjectPart] | str] = field(default_factory=dict)
    reserved: list[tuple[int, int]] = field(default_factory=list)
    guessed_parts: set[int] = field(default_factory=set)  # moved by original bytes only

    def item(self, obj_id: str) -> Item:
        for it in self.items:
            if it.obj.id == obj_id:
                return it
        raise KeyError(obj_id)


def order_items(objects: list[ObjectSpec]) -> list[ObjectSpec]:
    """Objects found by the game's code first, then static ones, then memory paths."""

    def rank(o: ObjectSpec) -> int:
        if o.hook is not None:
            return 0
        if o.anchors_of(StaticAnchor):
            return 1
        if o.anchors_of(ChainAnchor):
            return 2
        return 3

    return sorted(objects, key=rank)


def item_kind(obj: ObjectSpec) -> ItemKind:
    if obj.hook is not None:
        return "hook"
    if obj.anchors_of(StaticAnchor):
        return "static"
    if obj.anchors_of(ChainAnchor):
        return "chain"
    return "none"


@dataclass
class CarryReport:
    """What came over, for the summary line and the log."""

    carried: list[str] = field(default_factory=list)  # cheats brought over
    check: list[str] = field(default_factory=list)  # brought over, to be checked (a question)
    guessed: list[str] = field(default_factory=list)  # found from original bytes only
    failed: dict[str, str] = field(default_factory=dict)  # cheat -> reason
    waiting: list[str] = field(default_factory=list)  # values hooked but not seen yet
    objects: int = 0
    found: int = 0

    def summary(self, version: str) -> str:
        total = len(self.carried) + len(self.failed)
        if total:
            text = f"Brought over {len(self.carried)} of {total} cheats for version {version}."
        else:
            text = f"Brought over {self.found} of {self.objects} values for version {version}."
        if self.check:
            text += " Check them: " + ", ".join(self.check) + "."
        if self.guessed:
            text += " Found from their original bytes only (test them): "
            text += ", ".join(self.guessed) + "."
        for label in self.waiting:
            text += " " + WAITING.format(label=label)
        if self.failed:
            text += " Not brought over: "
            text += "; ".join(f"{n} ({why})" for n, why in self.failed.items()) + "."
        return text


def _copy_cheat(pc: ProjectCheat) -> ProjectCheat:
    return ProjectCheat.from_json(pc.to_json())


def _object_reason(plan: Plan, obj_id: str, reasons: dict[str, str]) -> str:
    try:
        it = plan.item(obj_id)
    except KeyError:
        return NO_OBJECT
    return reasons.get(obj_id) or it.reason or NOT_FOUND


def assemble(
    plan: Plan,
    found: dict[str, int | None],
    reasons: dict[str, str],
    have: Iterable[str] = (),
) -> tuple[Project, CarryReport]:
    """The new notes (format 4) and the report. Every object is saved, found or not
    (AC-U7); single cheats are always copied; a combined cheat only if every fixed part
    moved. ``found``: object id -> where it is now (None: not found); ``reasons``:
    object id -> why not. An object whose new hook went in but wasn't seen yet counts
    as brought over (it is found once the game runs that code). ``have``: ids of values
    the notes brought into already hold (Advanced): cheats on them come over too."""
    old = plan.old
    objects = [it.obj for it in plan.items]
    for o in objects:
        o.check = True
        o.carried_from = old.version
    kept = set(have)
    waiting = {
        it.obj.id: it.obj.label
        for it in plan.items
        if it.placed and it.obj.hook is not None and not found.get(it.obj.id)
    }
    report = CarryReport(
        objects=len(objects),
        found=sum(1 for v in found.values() if v),
        waiting=list(waiting.values()),
    )
    guessed_objs = {
        it.obj.id for it in plan.items if it.site is not None and it.site.how == "original"
    }
    cheats: list[ProjectCheat] = []
    for i, pc in enumerate(old.cheats):
        new = _copy_cheat(pc)
        why = ""
        if pc.parts:
            moved = plan.parts.get(i)
            if isinstance(moved, str):
                report.failed[pc.name] = moved
                continue
            if moved:
                fixed = iter(moved)
                new.parts = [p if p.learned else next(fixed) for p in new.parts]
                new.test = any(p.kind == "code_patch" for p in new.parts if not p.learned)
        cheats.append(new)
        objs = [o for o, _f in new.anchors if o]
        missing = [o for o in objs if not found.get(o) and o not in waiting and o not in kept]
        if missing:
            why = _object_reason(plan, missing[0], reasons)
            report.failed[pc.name] = why
            continue
        report.carried.append(pc.name)
        if any(o not in kept for o in objs):
            report.check.append(pc.name)
        if any(o in guessed_objs for o in objs) or i in plan.guessed_parts:
            report.guessed.append(pc.name)
    project = Project(
        old.title_id,
        plan.new.version,
        old.process,
        objects,
        cheats,
        history=[],
        carried_from=old.version,
    )
    return project, report


def for_merge(source: Project, into: Project, avoid: Iterable[str] = ()) -> Project:
    """``source`` with only what ``into`` doesn't have yet (Advanced, AC-U7): objects
    whose label is new, and cheats whose name is new. A cheat on a value ``into``
    already holds uses that value. A value a failed bring-over left in ``into`` (still
    to be checked, no way to be found) is brought again under its own id, replacing it;
    the player's own values are never replaced. Other object ids
    are remapped to ids neither ``into`` nor ``avoid`` (e.g. objects whose hook is still
    in the game) uses."""
    here = {o.label: o for o in into.objects}
    names = {c.name for c in into.cheats}
    used = {o.id for o in into.objects} | set(avoid)
    ids: dict[str, str] = {}
    objects = []
    n = 1
    for o in source.objects:
        mine = here.get(o.label)
        if mine is not None and (mine.anchors or not mine.check):  # kept; new cheats use it
            ids[o.id] = mine.id
            continue
        if mine is not None:  # a failed bring-over (no way to be found): brought again
            new_id = mine.id
        else:
            while f"obj{n}" in used:
                n += 1
            new_id = f"obj{n}"
            used.add(new_id)
        ids[o.id] = new_id
        copy = ObjectSpec.from_json(o.to_json())
        copy.id = new_id
        objects.append(copy)
    cheats = []
    for pc in source.cheats:
        if pc.name in names or any(o not in ids for o, _f in pc.anchors):
            continue
        copy = _copy_cheat(pc)
        if copy.parts:
            for p in copy.parts:
                if p.object is not None:
                    p.object = ids[p.object]
        else:
            copy.object = ids[copy.object]
        cheats.append(copy)
    return Project(
        source.title_id,
        source.version,
        source.process,
        objects,
        cheats,
        history=[],
        carried_from=source.carried_from,
    )


# -- live steps (new version running) -----------------------------------------------------
def drop_hook(item: Item, reason: str) -> None:
    """The hook can't come over: the object keeps its other ways (if any), without a
    class until the first Yes."""
    item.reason = reason
    item.obj.anchors = [a for a in item.obj.anchors if not isinstance(a, HookAnchor)]
    reset_class(item.obj)
    log.info("action=carry_hook object=%s result=failed reason=%s", item.obj.id, reason)


def has_code_parts(project: Project) -> bool:
    """A combined cheat holds a fixed code part in the game's module: moving it needs
    the game's code (a fixed data value or another module's part just fails its cheat,
    see _carry_parts)."""
    return any(
        not p.learned and p.kind == "code_patch" and p.module == project.process
        for pc in project.cheats
        for p in pc.parts
    )


def prepare(
    session: Session,
    old: Project,
    *,
    reserved: Iterable[tuple[int, int]] = (),
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> Plan:
    """Everything that needs no writing: the new copies of the objects, each hook's
    new place, and the fixed code parts moved. Reads the game's code only when there
    are hooks or code parts. ``reserved``: rooms kept by others right now (the
    session's and the open cheats' code lines). Raises ScanCancelled."""
    game = session.require_game()
    items = [Item(carried_object(o), o, item_kind(o)) for o in order_items(old.objects)]
    plan = Plan(
        old,
        Project(old.title_id, game.version, old.process),
        None,
        items,
        reserved=[*reserved, *ah.reserved_rooms(old.objects), *part_rooms(old)],
    )
    if any(it.kind == "hook" for it in items) or has_code_parts(old):
        plan.image = up.read_code_image(session, progress, cancel)
    for it in items:
        hook = it.old.hook
        if hook is None:
            reset_class(it.obj)  # its class moved with the update: learned at the first Yes
            continue
        assert plan.image is not None
        try:
            it.site = find_hook_site(hook, plan.image)
        except CarryFailed as exc:
            drop_hook(it, exc.reason)
            continue
        stuck = shift_fields(it.obj, it.shift)
        if stuck:  # a cheat on such a field would write in front of the object
            it.site = None
            drop_hook(it, FIELDS_MOVED)
            it.obj.anchors = []
            log.info(
                "action=carry_fields object=%s shift=%d result=failed fields=%s",
                it.obj.id,
                it.shift,
                ",".join(stuck),
            )
    _carry_parts(plan, plan.image)
    return plan


def _part_log(name: str, part: ProjectPart, new: int | None, result: str) -> None:
    log.info(
        "action=carry_part cheat=%s part=%s kind=%s old=%#x new=%s result=%s",
        name,
        part.name,
        part.kind,
        part.offset,
        "-" if new is None else f"{new:#x}",
        result,
    )


def _carry_parts(plan: Plan, image: up.CodeImage | None) -> None:
    """Move the combined cheats' fixed code parts with the v2 rules (AC-U6). A fixed
    value in the game's data, or a part in another module, fails its cheat."""
    old = plan.old
    fixed: dict[int, list[ProjectPart]] = {}
    tmp: list[Cheat] = []
    for i, pc in enumerate(old.cheats):
        parts = [p for p in pc.parts if not p.learned]
        if not parts:
            continue
        bad = next((p for p in parts if p.kind != "code_patch" or p.module != old.process), None)
        if bad is not None:
            plan.parts[i] = DATA_VALUE if bad.kind == "module_value" else OTHER_MODULE
            _part_log(pc.name, bad, None, "failed reason=" + plan.parts[i])
            continue
        fixed[i] = parts
        tmp.append(
            Cheat(str(i), [Location(LocationKind.CODE_PATCH, p.offset, p.on, p.off) for p in parts])
        )
    if not tmp:
        return
    assert image is not None  # prepare reads the code when there are fixed code parts
    sigs = up.load_signatures(up.signatures_path(old.title_id, old.version))  # read only
    cf = CheatFile("", old.title_id, old.version, tmp, old.process)
    moved, report = up.update_file(
        cf, sigs, image, plan.new.version, reserved=plan.reserved, cave_skip=cv.BUILD_CAVE_SKIP
    )
    caves: list[tuple[int, int]] = []
    for c in moved.cheats:
        i = int(c.name)
        name = old.cheats[i].name
        new_parts = []
        for p, loc in zip(fixed[i], c.locations, strict=True):
            new_parts.append(replace(p, offset=loc.offset, on=loc.on, off=loc.off))
            _part_log(name, p, loc.offset, "moved")
            if up.is_cave(loc):
                caves.append((loc.offset, loc.offset + len(loc.on)))
        plan.parts[i] = new_parts
        if c.name in report.guessed:
            plan.guessed_parts.add(i)
    for tmp_name, why in report.failed.items():
        i = int(tmp_name)
        at = report.failed_at.get(tmp_name)
        part = next((p for p in fixed[i] if p.offset == at), fixed[i][0])
        plan.parts[i] = PART_FAILED.format(part=part.name, why=why)
        _part_log(old.cheats[i].name, part, None, "failed reason=" + why)
    if caves:  # the new cave block: no hook room or slot goes there
        plan.reserved.append((min(a for a, _ in caves), max(b for _, b in caves)))


@dataclass
class Classes:
    """The objects the hooked instruction touched while we watched (AC-U4).
    ``by_vtable``: plausible objects by class (module offset; None: no class asked);
    ``skipped``: the old hook kept every object, so nothing was collected."""

    by_vtable: dict[int | None, list[int]] = field(default_factory=dict)
    plausible: list[int] = field(default_factory=list)
    objects: list[int] = field(default_factory=list)
    skipped: bool = False


@dataclass(frozen=True)
class Choice:
    """How the new hook tells the object apart: its class, or a Player-only field."""

    vtable: int | None
    filter: HookFilter
    player: int | None = None


@dataclass
class NeedPick:
    """The player has to pick (UI thread: ``finder.ask_pick``), then ``answer``.
    ``kind``: "class" (one option per class) or "player" (objects the old Player-only
    guess matches)."""

    options: list[rf.Found]
    kind: Literal["class", "player"]


def _probe(item: Item) -> ObjectSpec:
    """The new copy without its class: fields at their new offsets."""
    probe = ObjectSpec.from_json(item.obj.to_json())
    probe.vtable = None
    return probe


def collect_classes(
    session: Session,
    item: Item,
    *,
    seconds: float = rf.COLLECT_SECONDS,
    cancel: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
    reserved: Iterable[tuple[int, int]] = (),
) -> Classes:
    """Collect what the new hook site touches for ``seconds`` (the Player-only
    collect) and keep objects in writable memory whose fields look right; with a
    class before, only those starting with a pointer into the module, grouped by it.
    Raises CarryFailed, ScanCancelled."""
    hook, site = item.old.hook, item.site
    if hook is None or site is None:
        raise CarryFailed(item.reason or NOT_FOUND)
    if hook.filter.kind == "none":
        return Classes(skipped=True)
    game = session.require_game()
    writer = Writer(game.module_base + site.offset, site.code, site.text, 0)
    try:
        objs = cv.collect_objects(
            session, writer, seconds, cancel, sleep=sleep, extra_reserved=reserved or ()
        )
    except LockFailed as exc:
        raise _failed(item.obj, "collect", NO_ROOM, exc) from exc
    maps = rf.MapIndex(session.require_client().maps(game.pid))
    probe = _probe(item)
    had_class = item.old.vtable is not None
    out = Classes(objects=objs)
    for o in objs:
        if rf.verify(session, probe, o, maps) is not None:
            continue
        vt = ah.object_vtable(session, o) if had_class else None
        if had_class and vt is None:
            continue
        out.plausible.append(o)
        out.by_vtable.setdefault(vt, []).append(o)
    log.info(
        "action=carry_collect object=%s objects=%d plausible=%d classes=%d",
        item.obj.id,
        len(objs),
        len(out.plausible),
        len(out.by_vtable),
    )
    return out


def _dword(session: Session, addr: int) -> int | None:
    game = session.require_game()
    try:
        return struct.unpack("<I", session.require_client().read(game.pid, addr, 4))[0]
    except ReadFailed:
        return None


def _field_choice(session: Session, player: int, objects: list[int]) -> Choice:
    """The Player-only guess made again from the objects seen (as Find it next time)."""
    if not [o for o in objects if o != player]:
        return Choice(None, HookFilter("none"), player)
    try:
        guesses = cv.compare_objects(session, player, objects)
    except LockFailed:
        guesses = []
    if not guesses:
        return Choice(None, HookFilter("none"), player)
    g = guesses[0]
    return Choice(None, HookFilter("field", g.offset, g.player_value), player)


def choose(session: Session, classes: Classes, item: Item) -> Choice | NeedPick:
    """The new class (or Player-only guess) from what was collected. Raises
    CarryFailed when nothing fits (spec AC-U4)."""
    hook = item.old.hook
    assert hook is not None
    if classes.skipped:
        return Choice(None, HookFilter("none"))
    nothing = CarryFailed(NOTHING_TOUCHED.format(label="it"))
    if hook.filter.kind != "field":
        groups = {vt: objs for vt, objs in classes.by_vtable.items() if vt is not None}
        same_site = item.site is not None and item.site.offset == hook.hook
        if same_site and item.old.vtable in groups:  # nothing moved (a dry run, a patch)
            return Choice(item.old.vtable, HookFilter("vtable"))
        if len(groups) == 1:
            return Choice(next(iter(groups)), HookFilter("vtable"))
        if groups:
            return NeedPick([rf.Found("hook_slot", objs[0]) for objs in groups.values()], "class")
        raise nothing
    f = hook.filter
    players: list[int] = []
    for off in dict.fromkeys((f.off, f.off + item.shift)):  # as is, then shifted
        players = [o for o in classes.plausible if _dword(session, o + off) == f.value]
        if players:
            break
    if len(players) == 1:
        return _field_choice(session, players[0], classes.objects)
    if players:
        return NeedPick([rf.Found("hook_slot", o) for o in players], "player")
    raise nothing


def answer(session: Session, classes: Classes, need: NeedPick, address: int | None) -> Choice:
    """The player's pick for ``need`` (None: none of them). Raises CarryFailed."""
    if address is None:
        raise CarryFailed(NOT_PICKED)
    if need.kind == "player":
        return _field_choice(session, address, classes.objects)
    for vt, objs in classes.by_vtable.items():
        if address in objs and vt is not None:
            return Choice(vt, HookFilter("vtable"))
    raise CarryFailed(NOT_PICKED)


def _failed(obj: ObjectSpec, step: str, reason: str, exc: Exception) -> CarryFailed:
    """A short reason for the summary (it reads inside its brackets); the app's own
    message goes to the log."""
    log.info("action=carry_hook object=%s step=%s result=failed error=%s", obj.id, step, exc)
    return CarryFailed(reason)


def place_hook(
    session: Session,
    item: Item,
    choice: Choice,
    *,
    reserved: list[tuple[int, int]],
    prove_seconds: float = ah.PROVE_SECONDS,
    cancel: threading.Event | None = None,
    on_install: Callable[[ObjectSpec, str], None] | None = None,
) -> None:
    """A new capture room and slot (never in ``reserved``, which gets the new rooms),
    the slot checked for ``prove_seconds``, then the hook put in (``on_install`` hears
    of it right away, so a later error or Cancel takes it out). The first write of a
    bring-over. Raises CarryFailed, ScanCancelled."""
    old, site, obj = item.old.hook, item.site, item.obj
    if old is None or site is None:
        raise CarryFailed(item.reason or NOT_FOUND)
    game = session.require_game()
    cand = ah.HookCandidate(
        game.module_base + site.offset, site.code, site.text, 0, old.role, old.reg, site.disp, True
    )
    try:
        anchor = ah.plan_hook(session, cand, choice.vtable, choice.filter, reserved)
    except (LockFailed, FindFailed) as exc:
        raise _failed(obj, "plan", NO_ROOM, exc) from exc
    try:
        ah.prove_slot(session, game.module_base + anchor.slot, prove_seconds, cancel=cancel)
    except (LockFailed, FindFailed) as exc:
        raise _failed(obj, "prove", ROOM_USED, exc) from exc
    obj.vtable = choice.vtable
    others = [a for a in obj.anchors if not isinstance(a, HookAnchor | VtableAnchor)]
    obj.anchors = [anchor, *others]
    if choice.vtable is not None:
        obj.anchors.append(VtableAnchor())
        obj.relearn_class = False
    else:
        reset_class(obj)
    reserved.extend(ah.reserved_rooms([obj]))
    # the slot watch can be long: nothing goes in after a Cancel or into another game
    if cancel is not None and cancel.is_set():
        raise ScanCancelled("bringing the notes over was cancelled")
    now = session.game
    if now is None or now.pid != game.pid:
        raise CarryFailed("the game was closed")
    try:
        state = ah.install(session, obj)
    except FindFailed as exc:
        raise _failed(obj, "install", NOT_PUT_IN, exc) from exc
    item.placed = True
    if on_install is not None:
        on_install(obj, state)
    log.info(
        "action=carry_hook object=%s old=%#x new=%#x how=%s field_shift=%s cave=%#x slot=%#x "
        "class=%s",
        obj.id,
        old.hook,
        anchor.hook,
        site.how,
        f"{item.shift:#x}" if item.shift >= 0 else f"-{-item.shift:#x}",
        anchor.cave,
        anchor.slot,
        "-" if choice.vtable is None else f"{choice.vtable:#x}",
    )


def locate(
    session: Session,
    obj: ObjectSpec,
    *,
    cancel: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> rf.Outcome:
    """Find the object with its new ways (no memory search, chains not counted yet).
    ``disagree`` goes to the player (``ask_pick``, then ``rf.confirm_pick``)."""
    return rf.find_again(
        session,
        obj,
        use_scan=False,
        learn_chains=False,
        cancel=cancel,
        sleep=sleep,
        clock=clock,
    )


def _anchor_log(obj: ObjectSpec, kind: str, kept: bool, reason: str = "") -> None:
    log.info(
        "action=carry_anchor object=%s kind=%s result=%s%s",
        obj.id,
        kind,
        "kept" if kept else "dropped",
        f" reason={reason}" if reason else "",
    )


def settle(session: Session, obj: ObjectSpec, address: int | None, *, placed: bool = False) -> None:
    """Keep only the ways that lead to ``address`` (AC-U5): a static place that is it,
    memory paths that end there (counted as new), the hook just placed. No address and
    a hook ``placed`` (slot proven): the hook (and its class) stays in, the object is
    found by Find again once the game runs that code. No address and nothing placed:
    every way is dropped (saved with no way to be found, AC-U7); take a hook placed in
    this run out *before* dropping it, or nothing knows where it is."""
    if address is None:
        for a in obj.anchors:
            if isinstance(a, StaticAnchor | ChainAnchor):
                _anchor_log(obj, a.kind, False, "not found")
        if placed and obj.hook is not None:
            obj.anchors = [a for a in obj.anchors if isinstance(a, HookAnchor | VtableAnchor)]
            _anchor_log(obj, "hook", True, "not seen yet")
            return
        obj.anchors = []
        reset_class(obj)
        return
    game = session.require_game()
    client = session.require_client()
    read_u64 = ptr.u64_reader(lambda a, n: client.read(game.pid, a, n))
    keep: list[pj.Anchor] = []
    for a in obj.anchors:
        if isinstance(a, StaticAnchor):
            ok = game.module_base + a.off == address
            _anchor_log(obj, "static", ok, "" if ok else "another place")
        elif isinstance(a, ChainAnchor):
            ok = _chain_object(obj, a, game.module_base, read_u64) == address
            if ok:
                a.runs_seen = 1
            _anchor_log(obj, "chain", ok, "" if ok else "leads elsewhere")
        elif isinstance(a, VtableAnchor):
            ok = obj.vtable is not None
        else:
            ok = True
        if ok:
            keep.append(a)
    obj.anchors = keep
    if obj.hook is None and obj.vtable is not None:
        reset_class(obj)


def _chain_object(obj: ObjectSpec, a: ChainAnchor, base: int, read_u64) -> int | None:
    try:
        f = obj.field(a.field)
    except KeyError:
        return None
    end = ptr.Chain(a.base, a.offsets).resolve(base, read_u64)
    return None if end is None else end - f.off


# -- cheat files holding the notes' hook (AC-U9) ------------------------------------------
@dataclass
class HookLine:
    """A cheat-file cheat that is a notes hook: ``find`` (capture routine + jump: find it
    again) or ``keep`` (keep-value routine + jump: the PC-off cheat) of ``label``."""

    cheat: Cheat
    label: str
    kind: Literal["find", "keep"]


def split_hook_cheats(cf: CheatFile, old: Project | None) -> tuple[CheatFile, list[HookLine]]:
    """The cheats that are the old notes' hook (two code lines: the hook's jump and its
    capture or keep-value room) apart from the rest, which v2 moves as usual."""
    if old is None:
        return cf, []
    hooks = [(o.label, a) for o in old.objects for a in o.anchors_of(HookAnchor)]
    rest: list[Cheat] = []
    lines: list[HookLine] = []
    for c in cf.cheats:
        offs = sorted(loc.offset for loc in c.locations if loc.kind is LocationKind.CODE_PATCH)
        kind: Literal["find", "keep"] | None = None
        label = ""
        if len(offs) == 2 and len(c.locations) == 2:
            for lbl, a in hooks:
                if offs == sorted((a.hook, a.cave)):
                    kind, label = "find", lbl
                elif offs == sorted((a.hook, ah.keep_cave(a))):
                    kind, label = "keep", lbl
                if kind:
                    break
        if kind is None:
            rest.append(c)
        else:
            lines.append(HookLine(c, label, kind))
    return replace(cf, cheats=rest, credits=list(cf.credits)), lines


def rebuild_hook_cheat(session: Session, line: HookLine, new: Project | None) -> Cheat:
    """The hook cheat made again from the brought-over notes (new place, room and slot).
    A PC-off cheat keeps its value, and is made only once the player confirmed the
    value (its class may still be unlearned, and a wrong one would change every
    object the code touches). Raises CarryFailed."""
    obj = None
    if new is not None and new.carried_from is not None:
        obj = next((o for o in new.objects if o.label == line.label), None)
    if obj is None:
        raise CarryFailed(HOOK_FILE_FIRST)
    if obj.hook is None:
        raise CarryFailed(CODE_CHANGED.format(label=line.label))
    if line.kind == "keep" and obj.check:
        raise CarryFailed(CONFIRM_FIRST.format(label=line.label))
    c = line.cheat
    try:
        if line.kind == "find":
            return ah.hook_cheat(session, obj, c.name)
        if c.value is None or c.value_type is None:
            raise CarryFailed(PC_OFF_AGAIN)
        return ah.hook_cheat(session, obj, c.name, c.value_type.encode(c.value), c.value_type)
    except CarryFailed:
        raise
    except (FindFailed, LockFailed, ReadFailed) as exc:
        raise CarryFailed(str(exc)) from exc


def update_with_hooks(
    session: Session,
    cf: CheatFile,
    sigs: dict[int, up.Signature],
    image: up.CodeImage,
    new_version: str,
    old: Project | None,
    new: Project | None,
    *,
    reserved: Iterable[tuple[int, int]] = (),
) -> tuple[CheatFile, up.UpdateReport]:
    """v2's Update for this version, where the notes' hook cheats are made again from
    the brought-over notes instead of moved (AC-U9). Cheats keep their order. Moved
    caves never go in ``reserved`` (module ranges: the notes' hook rooms and slots,
    their code parts, the session's)."""
    rest, lines = split_hook_cheats(cf, old)
    # v2 reports by name; tag each cheat with its place so two cheats with one name
    # can't be mixed up, then give the names back
    names = {str(i): c.name for i, c in enumerate(rest.cheats)}
    tagged = replace(rest, cheats=[replace(c, name=str(i)) for i, c in enumerate(rest.cheats)])
    moved, tag_report = up.update_file(tagged, sigs, image, new_version, reserved=reserved)
    moved_by_tag = {c.name: replace(c, name=names[c.name]) for c in moved.cheats}
    report = up.UpdateReport()
    failed: dict[int, str] = {}  # id(cheat in cf) -> reason
    guessed: set[int] = set()
    out: dict[int, Cheat] = {}
    for i, c in enumerate(rest.cheats):
        tag = str(i)
        if tag in moved_by_tag:
            out[id(c)] = moved_by_tag[tag]
        else:
            failed[id(c)] = tag_report.failed.get(tag, NOT_FOUND)
            if tag in tag_report.failed_at:
                report.failed_at[c.name] = tag_report.failed_at[tag]
        if tag in tag_report.guessed:
            guessed.add(id(c))
    for line in lines:
        try:
            out[id(line.cheat)] = rebuild_hook_cheat(session, line, new)
        except CarryFailed as exc:
            failed[id(line.cheat)] = exc.reason
            log.info("action=update_hook cheat=%s result=failed reason=%s", line.cheat.name, exc)
    cheats: list[Cheat] = []
    for c in cf.cheats:
        if id(c) in out:
            cheats.append(out[id(c)])
            report.moved.append(c.name)
            if id(c) in guessed:
                report.guessed.append(c.name)
        else:
            report.failed[c.name] = failed[id(c)]
    return replace(moved, cheats=cheats), report


# -- fingerprints for the notes (AC-U2) -------------------------------------------------
def _code_parts(project: Project) -> list[ProjectPart]:
    return [
        p
        for pc in project.cheats
        for p in pc.parts
        if not p.learned and p.kind == "code_patch" and p.module == project.process
    ]


def _parts_file(project: Project) -> CheatFile:
    """The notes' fixed code parts as one cheat file (each combined cheat one cheat)."""
    cheats = []
    for i, pc in enumerate(project.cheats):
        locs = [
            Location(LocationKind.CODE_PATCH, p.offset, p.on, p.off)
            for p in pc.parts
            if not p.learned and p.kind == "code_patch" and p.module == project.process
        ]
        if locs:
            cheats.append(Cheat(str(i), locs))
    return CheatFile("", project.title_id, project.version, cheats, project.process)


def part_rooms(project: Project) -> list[tuple[int, int]]:
    """Module ranges the notes' fixed code caves keep (new code over spare zeros)."""
    return [
        (p.offset, p.offset + len(p.on))
        for p in _code_parts(project)
        if not any(p.off) and any(p.on)
    ]


def _clean_overlay(project: Project, extra=()) -> list[tuple[int, bytes]]:
    """The game's own bytes under our hooks, routines and fixed code parts."""
    out: list[tuple[int, bytes]] = []
    for o in project.objects:
        for a in o.anchors_of(HookAnchor):
            out.append((a.hook, a.orig))
            out.append((ah.keep_cave(a), bytes(2 * cv.CAVE_ROOM)))  # both routines
    out += [(p.offset, p.off) for p in _code_parts(project)]
    out += list(extra)
    return out


def hooks_to_learn(project: Project, image: up.CodeImage | None = None) -> list[HookAnchor]:
    """Hooks whose fingerprint wasn't checked, or (with ``image``, clean) doesn't match
    exactly once at their own place."""
    out = []
    for o in project.objects:
        for a in o.anchors_of(HookAnchor):
            if not a.sig_unique:
                out.append(a)
                continue
            sig = _signature(a)
            if image is not None and (sig is None or image.find(sig, 2) != [a.hook]):
                out.append(a)
    return out


def parts_to_learn(project: Project, have: dict[int, up.Signature]) -> list[Location]:
    """The notes' fixed code patches with no fingerprint in the signatures file."""
    return [loc for loc in up.patch_lines(_parts_file(project)) if loc.offset not in have]


def learn_note_signatures(
    project: Project, image: up.CodeImage, extra=(), have: dict[int, up.Signature] | None = None
) -> tuple[int, dict[int, up.Signature]]:
    """Learn fingerprints for the notes while their version runs (AC-U2, AC-U7), from
    ``image`` with the game's own bytes laid over ours (works with cheats on or off).
    Hooks get ``signature`` and ``sig_unique`` set in place; returns how many changed,
    and the fixed code parts' fingerprints that ``have`` (the signatures file, read
    if not given) lacks, for ``up.save_signatures``."""
    clean = image.overlay(_clean_overlay(project, extra))
    by_hook: dict[int, str] = {
        a.hook: o.id for o in project.objects for a in o.anchors_of(HookAnchor)
    }
    changed = 0
    for a in hooks_to_learn(project, clean):
        sigs = up.learn_at(clean, [(a.hook, max(up.SIG_MIN, len(a.orig)))])
        sig = sigs.get(a.hook)
        if sig is None:
            log.info(
                "action=learn_signature source=notes object=%s offset=%#x result=not_unique",
                by_hook.get(a.hook, "?"),
                a.hook,
            )
            continue
        a.signature, a.sig_unique = sig.text(), True
        changed += 1
        log.info(
            "action=learn_signature source=notes object=%s offset=%#x",
            by_hook.get(a.hook, "?"),
            a.hook,
        )
    if have is None:
        have = up.load_signatures(up.signatures_path(project.title_id, project.version))
    lines = parts_to_learn(project, have)
    part_sigs = up.learn_at(clean, [(loc.offset, len(loc.off)) for loc in lines])
    for off in part_sigs:
        log.info("action=learn_signature source=notes part offset=%#x", off)
    return changed, part_sigs


__all__ = [
    "CODE_CHANGED",
    "CONFIRM_FIRST",
    "DATA_VALUE",
    "FIELDS_MOVED",
    "NOTHING_TOUCHED",
    "NOT_FOUND",
    "NOT_PUT_IN",
    "NO_ROOM",
    "OTHER_MODULE",
    "PART_FAILED",
    "ROOM_USED",
    "WAITING",
    "CarryFailed",
    "CarryReport",
    "Choice",
    "Classes",
    "HookLine",
    "HookSite",
    "Item",
    "NeedPick",
    "Plan",
    "Source",
    "answer",
    "assemble",
    "carried_object",
    "choose",
    "collect_classes",
    "drop_hook",
    "find_hook_site",
    "find_source",
    "for_merge",
    "has_code_parts",
    "hooks_to_learn",
    "learn_note_signatures",
    "list_sources",
    "locate",
    "order_items",
    "part_rooms",
    "parts_to_learn",
    "place_hook",
    "prepare",
    "rebuild_hook_cheat",
    "reset_class",
    "settle",
    "shift_fields",
    "split_hook_cheats",
    "update_with_hooks",
    "usable_version",
    "version_key",
]

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
from dataclasses import dataclass, field
from typing import Literal

import iced_x86 as ix

from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import cave as cv
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.core.locker import MAX_INSN, insn_text, writes_memory
from cheatmaker.core.project import (
    ChainAnchor,
    HookAnchor,
    ObjectSpec,
    Project,
    ProjectCheat,
    ProjectPart,
    StaticAnchor,
    VtableAnchor,
)
from cheatmaker.errors import FindFailed, ProjectFileError

log = logging.getLogger(__name__)

# -- reasons (plain words, they follow the cheat's name in the summary) ---------------
CODE_CHANGED = "the game's code changed where {label} was found"
NOTHING_TOUCHED = "nothing in the game touched {label} while we watched"
NOT_FOUND = "it couldn't be found in this version"
DATA_VALUE = "it changes a value in the game's data, which moves with an update"
OTHER_MODULE = "it changes another part of the game"
PART_FAILED = "part {part} couldn't be found ({why}); make it again"
NO_OBJECT = "its value isn't in the notes any more"

HookHow = Literal["exact", "loose", "original"]


# -- versions and sources ---------------------------------------------------------------
_TOKEN = re.compile(r"\d+|[^\W\d_]+")


def version_key(v: str) -> tuple:
    """Sort key for game versions: numbers as numbers (``1.05`` = ``01.05``,
    ``03.000.005`` < ``03.001.000``), other text as text."""
    return tuple(
        (0, int(t)) if t.isdigit() else (1, t.casefold()) for t in _TOKEN.findall(v.strip())
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


def shift_fields(obj: ObjectSpec, shift: int) -> None:
    """Every field moves like the hooked field did (spec AC-U4 default)."""
    for f in obj.fields:
        f.off += shift


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
    plan: Plan, found: dict[str, int | None], reasons: dict[str, str]
) -> tuple[Project, CarryReport]:
    """The new notes (format 4) and the report. Every object is saved, found or not
    (AC-U7); single cheats are always copied; a combined cheat only if every fixed part
    moved. ``found``: object id -> where it is now (None: not found); ``reasons``:
    object id -> why not."""
    old = plan.old
    objects = [it.obj for it in plan.items]
    for o in objects:
        o.check = True
    report = CarryReport(objects=len(objects), found=sum(1 for v in found.values() if v))
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
        missing = [o for o in objs if not found.get(o)]
        if missing:
            why = _object_reason(plan, missing[0], reasons)
            report.failed[pc.name] = why
            continue
        report.carried.append(pc.name)
        if objs:
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


def for_merge(source: Project, into: Project) -> Project:
    """``source`` with only the objects and cheats whose names ``into`` doesn't have
    yet, object ids remapped to ids ``into`` doesn't use (Advanced, AC-U7)."""
    labels = {o.label for o in into.objects}
    names = {c.name for c in into.cheats}
    used = {o.id for o in into.objects}
    ids: dict[str, str] = {}
    objects = []
    n = 1
    for o in source.objects:
        if o.label in labels:
            continue
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
    "DATA_VALUE",
    "NOTHING_TOUCHED",
    "NOT_FOUND",
    "OTHER_MODULE",
    "PART_FAILED",
    "CarryFailed",
    "CarryReport",
    "HookSite",
    "Item",
    "Plan",
    "Source",
    "assemble",
    "carried_object",
    "find_hook_site",
    "find_source",
    "for_merge",
    "hooks_to_learn",
    "learn_note_signatures",
    "list_sources",
    "order_items",
    "part_rooms",
    "parts_to_learn",
    "reset_class",
    "shift_fields",
    "usable_version",
    "version_key",
]

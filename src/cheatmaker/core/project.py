"""``.cmproj``: what Cheat Maker remembers to find a value again (architecture v3 §8).

One JSON file per game + version in app data ``projects/{titleid}_{version}.cmproj``.
It holds **objects** (e.g. "you"): the fields inside them and the **anchors** that lead
back to the object after a restart, plus the live cheats built on those fields. Only
the PC app reads it; GoldHEN / CheatRunner files still hold plain byte patches.

Anchor kinds, simplest first (the order Find it again tries them):

* ``static``: the object sits at a fixed offset in the game's module.
* ``chain``: a pointer chain from the module to one field (``runs_seen`` counts the
  restarts it survived).
* ``hook_slot``: a small capture routine on an instruction that touches the field
  stores the object address into a 16-byte slot in the module's spare data.
* ``vtable_scan``: search memory for the object's class (vtable) pointer.

All offsets marked "module" are from the module base, so they hold across restarts.
"""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cheatmaker.core.values import ValueType
from cheatmaker.errors import ProjectFileError

FORMAT = "cheatmaker-project"
# 2: a hook's routine has a spare room below it for the keep-value (PC-off) routine.
# Hooks saved by format 1 don't, so they are dropped on load (found again by memory
# search until the player presses Find it next time again).
# 3: a cheat may be combined: its "parts" are learned fields and fixed byte patches.
FORMAT_VERSION = 3
CLASS_FLOAT_LIMIT = 1e9  # top of a float's plausible range when the class is checked
EXTENSION = ".cmproj"

Role = Literal["reader", "writer"]
FilterKind = Literal["vtable", "field", "none"]


def _hex(v: int) -> str:
    return f"{v:#x}" if v >= 0 else f"-{-v:#x}"


def _int(v: str | int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise ValueError(f"not a number: {v!r}")
    return v if isinstance(v, int) else int(v, 16)


def _dict(v: object, what: str) -> dict:
    if not isinstance(v, dict):
        raise ValueError(f"{what} is not an object")
    return v


def _list(v: object, what: str) -> list:
    if not isinstance(v, list):
        raise ValueError(f"{what} is not a list")
    return v


def _str(v: object, what: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"{what} is not text")
    return v


def _number(v: object, what: str) -> float | int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{what} is not a number: {v!r}")
    return v


def class_top(vtype: ValueType) -> float | int:
    """Top of the plausible range for a field of an object with a class check."""
    return CLASS_FLOAT_LIMIT if vtype.is_float else vtype.int_range()[1]


@dataclass
class Field:
    name: str
    off: int  # from the object start
    vtype: ValueType
    range: tuple[float, float] | None = None  # plausible values, both included
    last: float | int | None = None  # value when last found: pre-fills the search (AC-12.4)

    def plausible(self, raw: bytes) -> bool:
        if len(raw) < self.vtype.size:
            return False
        v = self.vtype.decode(raw)
        if self.vtype.is_float and not math.isfinite(v):
            return False
        return self.range is None or self.range[0] <= v <= self.range[1]

    def to_json(self) -> dict:
        d: dict = {"name": self.name, "off": _hex(self.off), "type": self.vtype.name.lower()}
        if self.range is not None:
            d["range"] = list(self.range)
        if self.last is not None:
            d["last"] = self.last
        return d

    @classmethod
    def from_json(cls, d: dict) -> Field:
        d = _dict(d, "a field")
        rng = d.get("range")
        if rng is not None:
            rng = _list(rng, "range")
            rng = (_number(rng[0], "range"), _number(rng[1], "range"))
        last = d.get("last")
        return cls(
            _str(d["name"], "a field name"),
            _int(d["off"]),
            ValueType[_str(d["type"], "a field type").upper()],
            rng,
            None if last is None else _number(last, "last value"),
        )


@dataclass
class HookFilter:
    """Which objects the capture routine keeps: ``vtable`` (``[obj] == vtable``),
    ``field`` (dword at ``obj+off`` == ``value``, the Player-only guess) or ``none``."""

    kind: FilterKind = "vtable"
    off: int = 0
    value: int = 0

    def to_json(self) -> dict:
        if self.kind == "field":
            return {"kind": "field", "off": _hex(self.off), "value": _hex(self.value)}
        return {"kind": self.kind}

    @classmethod
    def from_json(cls, d: dict) -> HookFilter:
        d = _dict(d, "a hook filter")
        if d["kind"] == "field":
            return cls("field", _int(d["off"]), _int(d["value"]))
        if d["kind"] not in ("vtable", "none"):
            raise ValueError(f"unknown filter {d['kind']!r}")
        return cls(d["kind"])


@dataclass
class StaticAnchor:
    off: int  # module offset of the object
    kind: str = field(default="static", init=False)

    def to_json(self) -> dict:
        return {"kind": self.kind, "off": _hex(self.off)}


@dataclass
class ChainAnchor:
    base: int  # module offset of the first pointer
    offsets: tuple[int, ...]
    field: str  # the chain leads to this field, not to the object start
    runs_seen: int = 1
    kind: str = field(default="chain", init=False)

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "base": _hex(self.base),
            "offsets": [_hex(o) for o in self.offsets],
            "field": self.field,
            "runs_seen": self.runs_seen,
        }


@dataclass
class HookAnchor:
    hook: int  # module offset of the hooked instruction
    insn: str  # shown under Advanced only
    orig: bytes  # the whole instructions the 5-byte jump covers
    reg: str  # object register, e.g. "r12"
    disp: int  # the instruction touches [reg + disp]
    role: Role
    filter: HookFilter
    cave: int  # module offset of the capture routine
    slot: int  # module offset of the 16-byte slot: object address, then a counter
    signature: str = ""  # updater.Signature text, for a later game update
    kind: str = field(default="hook_slot", init=False)

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "hook": _hex(self.hook),
            "insn": self.insn,
            "orig": self.orig.hex(" ").upper(),
            "reg": self.reg,
            "disp": _hex(self.disp),
            "role": self.role,
            "filter": self.filter.to_json(),
            "cave": _hex(self.cave),
            "slot": _hex(self.slot),
            "signature": self.signature,
        }


@dataclass
class VtableAnchor:
    kind: str = field(default="vtable_scan", init=False)

    def to_json(self) -> dict:
        return {"kind": self.kind}


Anchor = StaticAnchor | ChainAnchor | HookAnchor | VtableAnchor


def anchor_from_json(d: dict) -> Anchor:
    d = _dict(d, "an anchor")
    kind = d["kind"]
    if kind == "static":
        return StaticAnchor(_int(d["off"]))
    if kind == "chain":
        return ChainAnchor(
            _int(d["base"]),
            tuple(_int(o) for o in _list(d["offsets"], "offsets")),
            _str(d["field"], "a chain field"),
            int(d.get("runs_seen", 1)),
        )
    if kind == "hook_slot":
        role = d["role"]
        if role not in ("reader", "writer"):
            raise ValueError(f"unknown hook role {role!r}")
        return HookAnchor(
            _int(d["hook"]),
            d.get("insn", ""),
            bytes.fromhex(_str(d["orig"], "orig")),
            _str(d["reg"], "a register"),
            _int(d["disp"]),
            role,
            HookFilter.from_json(d.get("filter", {"kind": "vtable"})),
            _int(d["cave"]),
            _int(d["slot"]),
            d.get("signature", ""),
        )
    if kind == "vtable_scan":
        return VtableAnchor()
    raise ValueError(f"unknown anchor kind {kind!r}")


@dataclass
class ObjectSpec:
    id: str
    label: str
    vtable: int | None  # module offset of the class's vtable, None if not a C++ object
    fields: list[Field] = field(default_factory=list)
    anchors: list[Anchor] = field(default_factory=list)

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def anchors_of(self, kind: type) -> list:
        return [a for a in self.anchors if isinstance(a, kind)]

    @property
    def hook(self) -> HookAnchor | None:
        hooks = self.anchors_of(HookAnchor)
        return hooks[0] if hooks else None

    @property
    def has_proven_chain(self) -> bool:
        """A chain that survived a restart: the hook isn't needed (simple games stay simple)."""
        return any(c.runs_seen >= 2 for c in self.anchors_of(ChainAnchor))

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "vtable": None if self.vtable is None else _hex(self.vtable),
            "fields": [f.to_json() for f in self.fields],
            "anchors": [a.to_json() for a in self.anchors],
        }

    @classmethod
    def from_json(cls, d: dict) -> ObjectSpec:
        d = _dict(d, "an object")
        vt = d.get("vtable")
        oid = _str(d["id"], "an object id")
        return cls(
            oid,
            _str(d.get("label", oid), "a label"),
            None if vt is None else _int(vt),
            [Field.from_json(f) for f in _list(d.get("fields", []), "fields")],
            [anchor_from_json(a) for a in _list(d.get("anchors", []), "anchors")],
        )


PART_KINDS = ("code_patch", "module_value")  # the fixed parts a combined cheat saves


def _bytes(v: object, what: str) -> bytes:
    return bytes.fromhex(_str(v, what))


@dataclass
class ProjectPart:
    """One part of a combined cheat. Learned: found again each run by ``object`` and
    ``field``, written with ``value``. Fixed: bytes in the game's module."""

    name: str
    object: str | None = None
    field: str | None = None
    value: float | int = 0
    kind: str = "code_patch"
    module: str = "eboot.bin"
    section: int = 0
    offset: int = 0
    on: bytes = b""
    off: bytes = b""
    vtype: ValueType | None = None

    @property
    def learned(self) -> bool:
        return self.object is not None

    def to_json(self) -> dict:
        if self.learned:
            return {
                "name": self.name,
                "object": self.object,
                "field": self.field,
                "value": self.value,
            }
        d: dict = {
            "name": self.name,
            "kind": self.kind,
            "module": self.module,
            "section": self.section,
            "offset": _hex(self.offset),
            "on": self.on.hex(),
            "off": self.off.hex(),
        }
        if self.vtype is not None:
            d["type"] = self.vtype.name.lower()
        return d

    @classmethod
    def from_json(cls, d: dict) -> ProjectPart:
        d = _dict(d, "a cheat part")
        name = _str(d["name"], "a part name")
        if "object" in d:
            return cls(
                name,
                _str(d["object"], "a part's object"),
                _str(d["field"], "a part's field"),
                _number(d["value"], "a part value"),
            )
        kind = _str(d["kind"], "a part kind")
        if kind not in PART_KINDS:
            raise ValueError(f"unknown part kind {kind!r}")
        on, off = _bytes(d["on"], "a part's bytes"), _bytes(d["off"], "a part's bytes")
        if not on or len(on) != len(off):
            raise ValueError("a part's on and off bytes must be the same length")
        vt = d.get("type")
        return cls(
            name,
            kind=kind,
            module=_str(d.get("module", "eboot.bin"), "a part's module"),
            section=int(d.get("section", 0)),
            offset=_int(d["offset"]),
            on=on,
            off=off,
            vtype=None if vt is None else ValueType[_str(vt, "a part type").upper()],
        )


@dataclass
class ProjectCheat:
    """A live cheat built on the notes: one field (``object``/``field``/``value``) or,
    for a combined cheat, its ``parts`` in order."""

    name: str
    object: str = ""
    field: str = ""
    action: Literal["freeze", "set"] = "freeze"
    value: float | int = 0
    parts: list[ProjectPart] = dataclasses.field(default_factory=list)

    @property
    def anchors(self) -> list[tuple[str, str]]:
        """Every (object id, field) this cheat is found again by."""
        if not self.parts:
            return [(self.object, self.field)]
        return [(p.object, p.field) for p in self.parts if p.object is not None and p.field]

    def to_json(self) -> dict:
        if self.parts:
            return {
                "name": self.name,
                "action": self.action,
                "parts": [p.to_json() for p in self.parts],
            }
        return {
            "name": self.name,
            "object": self.object,
            "field": self.field,
            "action": self.action,
            "value": self.value,
        }

    @classmethod
    def from_json(cls, d: dict) -> ProjectCheat:
        d = _dict(d, "a cheat")
        if d["action"] not in ("freeze", "set"):
            raise ValueError(f"unknown action {d['action']!r}")
        name = _str(d["name"], "a cheat name")
        if "parts" in d:
            parts = [ProjectPart.from_json(p) for p in _list(d["parts"], "parts")]
            if not parts:
                raise ValueError("a combined cheat has no parts")
            return cls(name, action=d["action"], parts=parts)
        return cls(
            name,
            _str(d["object"], "a cheat's object"),
            _str(d["field"], "a cheat's field"),
            d["action"],
            _number(d["value"], "a cheat value"),
        )


@dataclass
class HistoryEntry:
    at: str  # ISO time
    method: str
    seconds: float
    verified: bool

    def to_json(self) -> dict:
        return {
            "at": self.at,
            "method": self.method,
            "seconds": round(self.seconds, 2),
            "verified": self.verified,
        }

    @classmethod
    def from_json(cls, d: dict) -> HistoryEntry:
        d = _dict(d, "a history entry")
        return cls(d["at"], d["method"], float(d["seconds"]), bool(d["verified"]))


MAX_HISTORY = 50


@dataclass
class Project:
    title_id: str
    version: str
    process: str = "eboot.bin"
    objects: list[ObjectSpec] = field(default_factory=list)
    cheats: list[ProjectCheat] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    # Objects whose hook came from an older file and was dropped (not saved): the
    # player is asked to press Find it next time again.
    dropped_hooks: list[str] = field(default_factory=list, compare=False)

    def object(self, obj_id: str) -> ObjectSpec:
        for o in self.objects:
            if o.id == obj_id:
                return o
        raise KeyError(obj_id)

    def new_object_id(self) -> str:
        used = {o.id for o in self.objects}
        n = 1
        while f"obj{n}" in used:
            n += 1
        return f"obj{n}"

    def put_object(self, obj: ObjectSpec) -> None:
        """Add ``obj``, replacing an object with the same id."""
        self.objects = [o for o in self.objects if o.id != obj.id] + [obj]

    def remember(self, entry: HistoryEntry) -> None:
        self.history = [*self.history, entry][-MAX_HISTORY:]

    def to_json(self) -> dict:
        return {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "title_id": self.title_id,
            "version": self.version,
            "process": self.process,
            "objects": [o.to_json() for o in self.objects],
            "cheats": [c.to_json() for c in self.cheats],
            "history": [h.to_json() for h in self.history],
        }

    @classmethod
    def from_json(cls, d: dict) -> Project:
        d = _dict(d, "the file")
        if d.get("format") != FORMAT:
            raise ValueError("not a Cheat Maker project file")
        ver = _int(d.get("format_version", 0))
        if ver > FORMAT_VERSION:
            raise ValueError(f"made by a newer Cheat Maker (format {ver})")
        objects = [ObjectSpec.from_json(o) for o in _list(d.get("objects", []), "objects")]
        dropped = []
        if ver < 2:  # its hooks have no spare room for the keep-value routine
            for o in objects:
                if o.vtable is not None:  # format 1 capped at 10x / 10,000 (BUG-08)
                    for f in o.fields:
                        if f.range is not None:
                            f.range = (f.range[0], class_top(f.vtype))
                if o.hook is not None:
                    o.anchors = [a for a in o.anchors if not isinstance(a, HookAnchor)]
                    dropped.append(o.id)
        return cls(
            _str(d["title_id"], "title_id"),
            _str(d["version"], "version"),
            _str(d.get("process", "eboot.bin"), "process"),
            objects,
            [ProjectCheat.from_json(c) for c in _list(d.get("cheats", []), "cheats")],
            [HistoryEntry.from_json(h) for h in _list(d.get("history", []), "history")],
            dropped,
        )


def project_path(title_id: str, version: str) -> Path:
    from cheatmaker.paths import app_data_dir

    safe_version = "".join(c for c in version if c.isalnum() or c in "._-")
    return app_data_dir() / "projects" / f"{title_id.upper()}_{safe_version}{EXTENSION}"


def dumps(project: Project) -> str:
    return json.dumps(project.to_json(), indent=1) + "\n"


def save(path: Path, project: Project) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dumps(project), encoding="utf-8")
    tmp.replace(path)  # never leave a half-written file behind


def load(path: Path) -> Project | None:
    """The project at ``path``, or None if there is none. Raises ProjectFileError."""
    if not path.exists():
        return None
    try:
        return Project.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ProjectFileError(f"{path.name}: {exc}") from exc


def set_aside(path: Path) -> Path | None:
    """Rename a file that can't be read (broken, or from a newer Cheat Maker) to
    ``*.cmproj.bak`` (``.bak2``, ... if taken) before a new one is saved over it."""
    if not path.exists():
        return None
    n = 1
    while True:
        bak = path.with_name(path.name + (".bak" if n == 1 else f".bak{n}"))
        if not bak.exists():
            path.replace(bak)
            return bak
        n += 1


__all__ = [
    "Anchor",
    "ChainAnchor",
    "Field",
    "HistoryEntry",
    "HookAnchor",
    "HookFilter",
    "ObjectSpec",
    "Project",
    "ProjectCheat",
    "ProjectPart",
    "StaticAnchor",
    "VtableAnchor",
    "load",
    "project_path",
    "save",
    "set_aside",
]

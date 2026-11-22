"""Pro cheats as source (architecture-pro "Data model changes").

A Pro cheat is a name plus entries: Bytes (on/off bytes at a site), NOP (the site's
instructions replaced by NOPs) or Cave (assembly source, placed in the module's spare
code and reached by a jump at the hook). Compiled bytes are stored next to the source,
so a project loads, turns on and exports with no assembler on the PC. Saved in the
``.cmproj`` (format 5) ``pro_cheats`` list; ``compile.compile_cheat`` turns one into
an ordinary ``Cheat``.

All offsets are from the main module's base.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Literal

from cheatmaker.core.values import ValueType

EntryKind = Literal["bytes", "nop", "cave"]
ENTRY_KINDS: tuple[str, ...] = ("bytes", "nop", "cave")
ORIGINS: tuple[str, ...] = ("pro", "guided", "import")
"""Where a Pro cheat came from: made in Pro, adopted from a guided cheat, or imported."""
NOP = 0x90
JMP_REL32_SIZE = 5
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_NAMES = frozenset({"hook", "return", "original"})
"""Symbols the cave wrapper defines itself; a variable can't take them."""
_REGS = (
    ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rsp", "rbp", "rip"]
    + ["eax", "ebx", "ecx", "edx", "esi", "edi", "esp", "ebp", "eip"]
    + ["ax", "bx", "cx", "dx", "si", "di", "sp", "bp", "ip"]
    + ["al", "bl", "cl", "dl", "ah", "bh", "ch", "dh", "sil", "dil", "spl", "bpl"]
    + [f"r{n}{s}" for n in range(8, 16) for s in ("", "d", "w", "b", "l")]
    + [f"{k}mm{n}" for k in ("x", "y", "z") for n in range(32)]
    + [f"mm{n}" for n in range(8)]
    + [f"st{n}" for n in range(8)]
    + [f"k{n}" for n in range(8)]
    + [f"cr{n}" for n in range(16)]
    + [f"dr{n}" for n in range(16)]
    + ["cs", "ds", "es", "fs", "gs", "ss", "st", "flags", "rflags", "eflags"]
)
_NASM_WORDS = [
    "byte",
    "word",
    "dword",
    "qword",
    "tword",
    "oword",
    "yword",
    "zword",
    "ptr",
    "rel",
    "abs",
    "near",
    "far",
    "short",
    "strict",
    "nosplit",
    "default",
    "bits",
    "org",
    "section",
    "segment",
    "align",
    "alignb",
    "times",
    "equ",
    "db",
    "dw",
    "dd",
    "dq",
    "dt",
    "do",
    "dy",
    "dz",
    "resb",
    "resw",
    "resd",
    "resq",
    "rest",
    "reso",
    "resy",
    "resz",
    "incbin",
    "seg",
    "wrt",
    "global",
    "extern",
    "common",
    "static",
    "macro",
    "endmacro",
    "struc",
    "endstruc",
    "istruc",
    "iend",
    "at",
    "to",
    "float",
    "__float32__",
    "__float64__",
    "__utf16__",
    "__utf32__",
]
ASM_NAMES = frozenset(n.lower() for n in (*_REGS, *_NASM_WORDS))
"""x86 register names and NASM keywords: a variable named like one wouldn't assemble
(or would silently mean the register)."""
MAX_OFFSET = 1 << 48
"""Module offsets are user-space addresses on the PS5: anything at or past 2^48 is a typo."""


def _hex(v: int) -> str:
    return f"{v:#x}"


def _offset(v: object, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise ValueError(f"{what} is not a number: {v!r}")
    n = v if isinstance(v, int) else int(v, 16)
    if n < 0:
        raise ValueError(f"{what} can't be negative")
    return n


def _text(d: dict, key: str, default: str | None = None) -> str:
    v = d.get(key, default)
    if not isinstance(v, str):
        raise ValueError(f"{key} is not text")
    return v


def _blob(d: dict, key: str) -> bytes:
    """Hex bytes; spaces are allowed ("89 4b 08"). Missing means none."""
    return bytes.fromhex(_text(d, key, ""))


def _dict(v: object, what: str) -> dict:
    if not isinstance(v, dict):
        raise ValueError(f"{what} is not an object")
    return v


def _list(v: object, what: str) -> list:
    if not isinstance(v, list):
        raise ValueError(f"{what} is not a list")
    return v


@dataclass
class Variable:
    """A read-only constant placed at the end of the cave (R1), named in the source."""

    name: str
    vtype: ValueType
    value: float | int
    comment: str = ""

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"variable name {self.name!r}: use letters, digits and _")
        if self.name.lower() in RESERVED_NAMES:
            raise ValueError(f"{self.name!r} is a name the cave already uses")
        if self.name.lower() in ASM_NAMES:
            raise ValueError(f"{self.name!r} is a register or assembler word; pick another name")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError(f"variable {self.name}: value is not a number")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError(f"variable {self.name}: value must be a finite number")
        if (
            not self.vtype.is_float
            and isinstance(self.value, float)
            and not self.value.is_integer()
        ):
            raise ValueError(f"variable {self.name}: {self.vtype.label} needs a whole number")
        try:
            self.encoded()  # raises ValueError when the value doesn't fit the type
        except OverflowError:  # e.g. 1e39 as a 4-byte decimal, or a 400-digit whole number
            raise ValueError(
                f"variable {self.name}: {self.value!r} doesn't fit in {self.vtype.label.lower()}"
            ) from None

    def encoded(self) -> bytes:
        """The value's bytes as placed in the cave."""
        v = float(self.value) if self.vtype.is_float else int(self.value)
        return self.vtype.encode(v)

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "type": self.vtype.name.lower(),
            "value": self.value,
            "comment": self.comment,
        }

    @classmethod
    def from_json(cls, d: object) -> Variable:
        d = _dict(d, "a variable")
        try:
            vtype = ValueType[_text(d, "type").upper()]
        except KeyError:
            raise ValueError(f"unknown variable type {d.get('type')!r}") from None
        return cls(_text(d, "name"), vtype, d["value"], _text(d, "comment", ""))


@dataclass
class ProEntry:
    """One site a Pro cheat changes.

    ``bytes``: ``on`` replaces ``off`` at ``offset``. ``nop``: ``on`` defaults to NOPs
    over ``off``. ``cave``: ``offset`` is the hook, ``off`` the instructions the jump
    covers, ``on`` the jump plus NOPs, ``cave`` where ``cave_bytes`` (assembled from
    ``source``) go; ``cave`` is None until the cave is placed."""

    kind: EntryKind
    offset: int
    off: bytes
    on: bytes = b""
    source: str = ""
    cave: int | None = None
    cave_bytes: bytes = b""
    aob: str = ""
    comment: str = ""
    stale: bool = False
    """Cave only: its bytes no longer match its source (another cave's variables
    moved and it couldn't be re-assembled). It can't be turned on until rebuilt."""

    def __post_init__(self) -> None:
        if self.kind not in ENTRY_KINDS:
            raise ValueError(f"unknown entry kind {self.kind!r}")
        if self.offset < 0 or (self.cave is not None and self.cave < 0):
            raise ValueError("an offset can't be negative")
        if self.offset >= MAX_OFFSET or (self.cave is not None and self.cave >= MAX_OFFSET):
            raise ValueError(f"an offset must be below {MAX_OFFSET:#x}")
        if not self.off:
            raise ValueError(f"a {self.kind} entry needs its original bytes")
        if self.kind == "nop" and not self.on:
            self.on = bytes([NOP]) * len(self.off)
        if self.kind != "cave" and self.source:
            raise ValueError("only a cave entry has assembly source")
        if self.kind != "cave" and (self.cave is not None or self.cave_bytes):
            raise ValueError("only a cave entry has a cave")
        if self.on and len(self.on) != len(self.off):
            raise ValueError("on and off bytes must be the same length")
        if self.kind != "cave" and not self.on:
            raise ValueError(f"a {self.kind} entry needs its new bytes")
        if self.kind == "cave":
            if len(self.off) < JMP_REL32_SIZE:
                raise ValueError(f"a cave hook covers at least {JMP_REL32_SIZE} bytes")
            if self.cave_bytes and self.cave is None:
                raise ValueError("compiled cave bytes need the cave's place")
            if self.cave_bytes and len(self.on) < JMP_REL32_SIZE:
                raise ValueError("a compiled cave needs the jump at its hook")
        elif self.stale:
            raise ValueError("only a cave entry can be stale")

    @property
    def nop_default(self) -> bool:
        return self.kind == "nop" and self.on == bytes([NOP]) * len(self.off)

    def to_json(self) -> dict:
        if self.kind == "cave":
            d = {
                "kind": "cave",
                "hook": _hex(self.offset),
                "cave": None if self.cave is None else _hex(self.cave),
                "source": self.source,
                "off_hook": self.off.hex(),
                "on_hook": self.on.hex(),
                "cave_bytes": self.cave_bytes.hex(),
                "aob": self.aob,
                "comment": self.comment,
            }
            if self.stale:  # optional key, still format 5
                d["stale"] = True
            return d
        d: dict = {"kind": self.kind, "offset": _hex(self.offset), "off": self.off.hex()}
        if not self.nop_default:  # plain NOPs are implied by the kind
            d["on"] = self.on.hex()
        d.update(aob=self.aob, comment=self.comment)
        return d

    @classmethod
    def from_json(cls, d: object) -> ProEntry:
        d = _dict(d, "an entry")
        kind = _text(d, "kind")
        common = {"aob": _text(d, "aob", ""), "comment": _text(d, "comment", "")}
        if kind == "cave":
            cave = d.get("cave")
            return cls(
                "cave",
                _offset(d["hook"], "a hook offset"),
                _blob(d, "off_hook"),
                _blob(d, "on_hook"),
                source=_text(d, "source", ""),
                cave=None if cave is None else _offset(cave, "a cave offset"),
                cave_bytes=_blob(d, "cave_bytes"),
                stale=d.get("stale", False) is True,
                **common,
            )
        if kind not in ENTRY_KINDS:
            raise ValueError(f"unknown entry kind {kind!r}")
        return cls(
            kind,  # type: ignore[arg-type]
            _offset(d["offset"], "an entry offset"),
            _blob(d, "off"),
            _blob(d, "on"),
            **common,
        )


@dataclass
class ProCheat:
    id: str
    name: str
    entries: list[ProEntry] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    editable: str | None = None
    """The one variable written with the cheat file's single-value extension (R1)."""
    origin: str = "pro"
    comment: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("a Pro cheat needs an id")
        if self.origin not in ORIGINS:
            raise ValueError(f"unknown origin {self.origin!r}")
        names = [v.name for v in self.variables]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.name}: two variables share a name")
        if self.editable is not None and self.editable not in names:
            raise ValueError(f"{self.name}: editable variable {self.editable!r} doesn't exist")

    def variable(self, name: str) -> Variable:
        for v in self.variables:
            if v.name == name:
                return v
        raise KeyError(name)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "origin": self.origin,
            "comment": self.comment,
            "variables": [v.to_json() for v in self.variables],
            "editable": self.editable,
            "entries": [e.to_json() for e in self.entries],
        }

    @classmethod
    def from_json(cls, d: object) -> ProCheat:
        d = _dict(d, "a Pro cheat")
        editable = d.get("editable")
        if editable is not None and not isinstance(editable, str):
            raise ValueError("editable is not text")
        return cls(
            _text(d, "id"),
            _text(d, "name"),
            [ProEntry.from_json(e) for e in _list(d.get("entries", []), "entries")],
            [Variable.from_json(v) for v in _list(d.get("variables", []), "variables")],
            editable,
            _text(d, "origin", "pro"),
            _text(d, "comment", ""),
        )


def new_cheat_id(cheats: list[ProCheat]) -> str:
    """``p1``, ``p2``, ... : the first id no cheat in ``cheats`` uses."""
    used = {c.id for c in cheats}
    n = 1
    while f"p{n}" in used:
        n += 1
    return f"p{n}"

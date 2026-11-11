"""Cheat model (architecture §8): what a cheat is, where it lives, whether it can be saved."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from cheatmaker.core.values import ValueType
from cheatmaker.errors import CheatMakerError

MAIN_MODULE = "eboot.bin"
MAX_MOVING = 4  # parts that move (session or learned) in one combined cheat


class LocationKind(Enum):
    CODE_PATCH = "code_patch"  # "Lock it" result or a loaded file: survives restarts
    MODULE_VALUE = "module_value"  # value inside the game's own module: survives restarts
    SESSION_ADDRESS = "session_address"  # heap value: this session only, can't be saved


@dataclass
class Location:
    kind: LocationKind
    offset: int  # from the main module base; absolute address for SESSION_ADDRESS
    on: bytes
    off: bytes
    module: str = MAIN_MODULE
    section: int = 0
    anchor: tuple[str, str] | None = None
    """A learned part of a combined cheat: (object id, field) in the game's .cmproj.
    Its place is found again each run; ``offset`` is unused and never written to."""
    vtype: ValueType | None = field(default=None, compare=False)
    """The number type of a combined cheat's part (what Find it next time learns)."""
    part: str | None = field(default=None, compare=False)
    """The part's name in a combined cheat ("health", "health (2)"); None otherwise."""

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("offset can't be negative")
        if len(self.on) != len(self.off):
            raise ValueError("on and off bytes must be the same length")
        if not self.on:
            raise ValueError("a cheat line needs at least one byte")

    def address(self, module_base: int) -> int:
        """Where to write on the running console."""
        if self.kind is LocationKind.SESSION_ADDRESS:
            return self.offset
        return module_base + self.offset

    @property
    def moving(self) -> bool:
        """A value found by a search that isn't learned yet: this session only."""
        return self.kind is LocationKind.SESSION_ADDRESS and self.anchor is None


@dataclass
class Cheat:
    name: str
    locations: list[Location]
    template: str | None = None
    freeze: bool = False  # keep writing while on (live only; files can't freeze)
    value_type: ValueType | None = None  # set when the cheat holds a value the player can edit
    hint: str | None = None
    enabled: bool = field(default=False, compare=False)
    anchor: tuple[str, str] | None = None
    """(object id, field name) in the game's .cmproj: the address is found again each
    run (v3) instead of being the one in ``locations``."""
    value_at: list[tuple[int, int]] | None = None
    """Where the value's bytes sit, lowest byte first, as (line, byte) pairs, when the
    value is inside game code (Player only, works with the PC off). None: every line's
    ``on`` bytes are the value."""
    check_after_restart: bool = field(default=False, compare=False)
    """Made by Make it work with Only me by a field: the field may differ after a
    restart, so My cheats says "Check after restart". Not saved in cheat files."""
    carried: str = field(default="", compare=False)
    """Brought over from another game version (spec-game-update AC-U8): "check" until
    the player confirms its value, "test" until its brought-over code parts were
    ticked once, "" otherwise. Set from the notes; never saved in cheat files."""

    @property
    def session_only(self) -> bool:
        return any(loc.kind is LocationKind.SESSION_ADDRESS for loc in self.locations)

    @property
    def saveable(self) -> bool:
        return bool(self.locations) and not self.session_only

    @property
    def combined(self) -> bool:
        """Made by Combine: each location is a named part."""
        return any(loc.part is not None for loc in self.locations)

    @property
    def learned_parts(self) -> list[Location]:
        """A combined cheat's parts that are found again each run."""
        return [loc for loc in self.locations if loc.anchor is not None]

    @property
    def moving_parts(self) -> list[Location]:
        """A combined cheat's parts that are this session only (not learned yet)."""
        return [loc for loc in self.locations if loc.moving] if self.combined else []

    @property
    def objects(self) -> list[str]:
        """The .cmproj objects this cheat is found again by, in order, no repeats."""
        ids = [self.anchor[0]] if self.anchor is not None else []
        ids += [loc.anchor[0] for loc in self.learned_parts if loc.anchor is not None]
        return list(dict.fromkeys(ids))

    @property
    def found_again(self) -> bool:
        """Found again each run: a single found-again cheat, or a combined cheat with
        learned parts and none that are this session only."""
        return self.anchor is not None or (bool(self.learned_parts) and not self.moving_parts)

    @property
    def value(self) -> float | int | None:
        if self.value_type is None or not self.locations:
            return None
        if self.value_at is None:
            return self.value_type.decode(self.locations[0].on)
        try:
            raw = bytes(self.locations[i].on[b] for i, b in self.value_at)
        except IndexError:
            return None
        if len(raw) != self.value_type.size:
            return None
        return self.value_type.decode(raw)

    def set_value(self, value: float | int) -> None:
        """Change the number the cheat writes (AC-5.2). Raises ValueError if it can't."""
        if self.value is None:
            raise ValueError("this cheat doesn't hold a number")
        vt = self.value_type
        assert vt is not None  # self.value is None without one
        data = vt.encode(value)
        if self.value_at is None:
            for loc in self.locations:
                loc.on = data
            return
        on = {i: bytearray(loc.on) for i, loc in enumerate(self.locations)}
        for (i, b), byte in zip(self.value_at, data, strict=True):
            on[i][b] = byte
        for i, loc in enumerate(self.locations):
            new = bytes(on[i])
            if new == loc.on:
                continue
            if loc.off == loc.on:  # a routine left in place when off: keep it the same
                loc.off = new
            loc.on = new


def value_positions(
    build: Callable[[bytes], list[bytes]], size: int
) -> list[tuple[int, int]] | None:
    """Where a value of ``size`` bytes lands in the lines ``build(value)`` makes, as
    (line, byte) pairs lowest byte first; None if it can't be told (the lines change
    shape with the value). Builds with three test values and compares them."""
    if size < 1:
        return None
    low, high = bytes(size), b"\xff" * size
    mark = bytes(range(1, size + 1))
    try:
        a, b, m = build(low), build(high), build(mark)
    except (CheatMakerError, ValueError, RuntimeError):  # it may refuse a test value
        return None
    if not len(a) == len(b) == len(m) or any(
        not len(x) == len(y) == len(z) for x, y, z in zip(a, b, m, strict=True)
    ):
        return None
    diff = [
        (i, j)
        for i, (x, y) in enumerate(zip(a, b, strict=True))
        for j in range(len(x))
        if x[j] != y[j]
    ]
    if len(diff) != size:
        return None
    ordered: list[tuple[int, int] | None] = [None] * size
    for i, j in diff:
        k = m[i][j] - 1
        if not 0 <= k < size or ordered[k] is not None:
            return None
        ordered[k] = (i, j)
    return ordered  # type: ignore[return-value]


def combine(
    cheats: list[Cheat],
    name: str | None = None,
    addr_of: Callable[[Location], int | None] | None = None,
    module_base: int | None = None,
) -> Cheat:
    """One cheat that writes all of ``cheats``' lines (e.g. two health values that must
    both be kept). It keeps writing if any of them did. Its value can't be edited, since
    the parts usually hold different numbers. Each line becomes a named part that keeps
    its number type, and a found-again cheat becomes a learned part. ``addr_of`` gives
    where a learned part is right now and ``module_base`` where the game's module is (to
    spot overlaps across them). Raises ValueError if it can't combine them."""
    if len(cheats) < 2:
        raise ValueError("pick at least two cheats to combine")
    parts = [p for c in cheats for p in _parts_of(c)]
    _name_parts(parts)
    moving = sum(1 for p in parts if p.kind is LocationKind.SESSION_ADDRESS)
    if moving > MAX_MOVING:
        raise ValueError(f"Combine up to {MAX_MOVING} values that move; these have {moving}.")
    _refuse_overlap(parts, addr_of, module_base)
    return Cheat(
        name=name or " + ".join(c.name for c in cheats),
        locations=parts,
        freeze=any(c.freeze for c in cheats),
        hint="Combined: " + ", ".join(c.name for c in cheats),
    )


def _parts_of(c: Cheat) -> list[Location]:
    """Copies of ``c``'s lines as parts (named later), each with its number type."""
    if c.anchor is not None:  # found again each run: one learned part
        loc = c.locations[0]
        return [
            Location(
                LocationKind.SESSION_ADDRESS,
                0,
                loc.on,
                loc.off,
                anchor=c.anchor,
                vtype=c.value_type,
                part=c.name,
            )
        ]
    out = []
    for i, loc in enumerate(c.locations, 1):
        vtype = loc.vtype
        if (
            vtype is None
            and c.value_type is not None
            and loc.kind is not LocationKind.CODE_PATCH
            and c.value_at is None
            and len(loc.on) == c.value_type.size
        ):
            vtype = c.value_type
        part = loc.part or (f"{c.name} ({i})" if len(c.locations) > 1 else c.name)
        out.append(
            Location(
                loc.kind,
                loc.offset,
                loc.on,
                loc.off,
                loc.module,
                loc.section,
                anchor=loc.anchor,
                vtype=vtype,
                part=part,
            )
        )
    return out


def _name_parts(parts: list[Location]) -> None:
    """Two parts never share a name: a repeat gets " (2)", " (3)", ..."""
    used: set[str] = set()
    for p in parts:
        base = name = p.part or "part"
        n = 2
        while name in used:
            name = f"{base} ({n})"
            n += 1
        p.part = name
        used.add(name)


def _span(
    loc: Location,
    addr_of: Callable[[Location], int | None] | None,
    module_base: int | None,
) -> tuple[str, int, int] | None:
    """(space, first byte, end) of a part, or None if it can't be told. With the
    module base known every part is in one space: the running game's addresses."""
    if loc.anchor is not None:
        addr = addr_of(loc) if addr_of is not None else None
        return None if addr is None else ("", addr, addr + len(loc.on))
    if loc.kind is LocationKind.SESSION_ADDRESS:
        return ("", loc.offset, loc.offset + len(loc.on))
    if module_base is not None:
        at = loc.address(module_base)
        return ("", at, at + len(loc.on))
    return (loc.module, loc.offset, loc.offset + len(loc.on))


def _refuse_overlap(
    parts: list[Location],
    addr_of: Callable[[Location], int | None] | None,
    module_base: int | None,
) -> None:
    """Two parts that share a byte would fight over it: refuse them."""
    spans = [_span(p, addr_of, module_base) for p in parts]
    for i, a in enumerate(parts):
        for j in range(i + 1, len(parts)):
            b, sa, sb = parts[j], spans[i], spans[j]
            same_anchor = a.anchor is not None and a.anchor == b.anchor
            shared = (
                sa is not None
                and sb is not None
                and sa[0] == sb[0]
                and sa[1] < sb[2]
                and sb[1] < sa[2]
            )
            if same_anchor or shared:
                raise ValueError(f"{a.part} and {b.part} overlap in memory.")


@dataclass
class CheatFile:
    """One game + version's cheats: what a .json/.shn file holds."""

    title: str
    title_id: str
    version: str
    cheats: list[Cheat] = field(default_factory=list)
    process: str = MAIN_MODULE
    credits: list[str] = field(default_factory=lambda: ["Made with Cheat Maker"])

    @property
    def is_ps4(self) -> bool:
        return self.title_id.upper().startswith("CUSA")

    def file_name(self, fmt: str) -> str:
        """``{titleid}_{version}.{ext}`` (AC-6.1, AC-6.2)."""
        if fmt not in ("json", "shn"):
            raise ValueError(f"unknown format {fmt!r}")
        if not self.title_id or not self.version:
            raise ValueError("title ID and version are needed to name the file")
        safe_version = "".join(c for c in self.version if c.isalnum() or c in "._-")
        return f"{self.title_id.upper()}_{safe_version}.{fmt}"

    def saveable_cheats(self) -> list[Cheat]:
        return [c for c in self.cheats if c.saveable]


def normalize_version(version: str) -> str:
    """ "01.05" and "1.05" are the same version; compare them without leading zeros."""
    parts = version.strip().split(".")
    return ".".join(p.lstrip("0") or "0" for p in parts)


def versions_differ(file_version: str, game_version: str) -> bool:
    return normalize_version(file_version) != normalize_version(game_version)

"""Cheat model (architecture §8): what a cheat is, where it lives, whether it can be saved."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cheatmaker.core.values import ValueType

MAIN_MODULE = "eboot.bin"


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


@dataclass
class Cheat:
    name: str
    locations: list[Location]
    template: str | None = None
    freeze: bool = False  # keep writing while on (live only; files can't freeze)
    value_type: ValueType | None = None  # set when the cheat holds a value the player can edit
    hint: str | None = None
    enabled: bool = field(default=False, compare=False)

    @property
    def session_only(self) -> bool:
        return any(loc.kind is LocationKind.SESSION_ADDRESS for loc in self.locations)

    @property
    def saveable(self) -> bool:
        return bool(self.locations) and not self.session_only

    @property
    def value(self) -> float | int | None:
        if self.value_type is None or not self.locations:
            return None
        return self.value_type.decode(self.locations[0].on)

    def set_value(self, value: float | int) -> None:
        """Change the number the cheat writes (AC-5.2). Raises ValueError if it can't."""
        if self.value_type is None:
            raise ValueError("this cheat doesn't hold a number")
        data = self.value_type.encode(value)
        for loc in self.locations:
            loc.on = data


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

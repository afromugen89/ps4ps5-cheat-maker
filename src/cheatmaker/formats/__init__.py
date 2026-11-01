"""CheatRunner / GoldHEN cheat file formats (.json default, .shn option)."""

from __future__ import annotations

from pathlib import Path

from cheatmaker.core.cheats import CheatFile
from cheatmaker.errors import CheatFileError
from cheatmaker.formats import goldhen_json, shn

FORMATS = {"json": goldhen_json, "shn": shn}


def dumps(cf: CheatFile, fmt: str) -> bytes:
    try:
        return FORMATS[fmt].dumps(cf)
    except KeyError:
        raise ValueError(f"unknown format {fmt!r}") from None


def load_path(path: str | Path) -> CheatFile:
    """Read a .json or .shn file; anything wrong raises CheatFileError (AC-7.3)."""
    p = Path(path)
    fmt = p.suffix.lower().lstrip(".")
    if fmt not in FORMATS:
        raise CheatFileError(f"unsupported extension {p.suffix!r}")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise CheatFileError(f"can't read {p.name}: {exc}") from exc
    return FORMATS[fmt].loads(data)

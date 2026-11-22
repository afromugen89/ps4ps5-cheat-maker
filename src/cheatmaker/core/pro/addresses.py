"""Addresses as the Pro window shows and reads them (``eboot.bin+0x5e1536c``)."""

from __future__ import annotations

import re

from cheatmaker.core.cheats import MAIN_MODULE
from cheatmaker.core.session import GameInfo

_PLACE = re.compile(
    r"^\s*(?:(?P<module>[\w.\-]+)?\s*\+\s*)?(?P<num>(?:0x)?[0-9a-f]+)h?\s*$", re.IGNORECASE
)
BAD_PLACE = "Type an address like eboot.bin+0x1234, +0x1234 or 0x41234."


def parse_place(text: str, game: GameInfo) -> int:
    """The absolute address for what the player typed. ``eboot.bin+X`` / ``+X`` is X
    from the game's module start; a bare number is an address when it is at or above
    the module start, otherwise an offset into the module. Numbers are hex. ValueError
    with ``BAD_PLACE`` for anything else."""
    m = _PLACE.match(text)
    if m is None:
        raise ValueError(BAD_PLACE)
    module = m["module"]
    if module is not None and module.lower() not in (MAIN_MODULE, game.process.lower()):
        raise ValueError(f"Only the game's own module ({MAIN_MODULE}) can be used here.")
    num = int(m["num"], 16)
    explicit = "+" in text
    if explicit or num < game.module_base:
        return game.module_base + num
    return num


def place_text(addr: int, game: GameInfo | None) -> str:
    """``eboot.bin+0x1234`` inside the game's module, else ``0x...``."""
    if game is not None and game.module_base <= addr < game.module_end:
        return f"{MAIN_MODULE}+{addr - game.module_base:#x}"
    return f"{addr:#x}"


__all__ = ["BAD_PLACE", "parse_place", "place_text"]

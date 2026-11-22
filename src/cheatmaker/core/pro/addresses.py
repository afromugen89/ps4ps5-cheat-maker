"""Addresses as the Pro window shows and reads them (``eboot.bin+0x5e1536c``)."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from cheatmaker.core.cheats import MAIN_MODULE, Cheat, Location
from cheatmaker.core.freezer import Freezer
from cheatmaker.core.pro.badge import Badge, badge
from cheatmaker.core.session import GameInfo, Session
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ConsoleBusy, ReadFailed

log = logging.getLogger(__name__)

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


# -- the address list (spec PR-2) ---------------------------------------------------------
@dataclass
class AddressRow:
    """One address the player keeps: what it is, how to read it, and its freeze."""

    address: int
    vtype: ValueType
    description: str = ""
    group: str = ""
    comment: str = ""
    value: float | int | None = None  # as last read; None: couldn't be read
    frozen: float | int | None = None  # the value kept while frozen

    @property
    def key(self) -> str:
        return freeze_key(self.address)


def freeze_key(addr: int) -> str:
    """The Freezer key of a Pro address-list freeze (never a guided one's)."""
    return f"pro:{addr:x}"


def read_values(session: Session, rows: Sequence[AddressRow]) -> list[float | int | None]:
    """Each row's value now (None where it can't be read; the link stays up)."""
    game = session.require_game()
    client = session.require_client()
    out: list[float | int | None] = []
    for row in rows:
        try:
            out.append(row.vtype.decode(client.read(game.pid, row.address, row.vtype.size)))
        except ConsoleBusy:
            raise
        except ReadFailed:
            out.append(None)
    return out


def set_value(row: AddressRow, value: float | int, write) -> None:
    """Write ``value`` once at the row's address (``write(addr, data, source)``: the
    Pro writer, which logs and reads back)."""
    write(row.address, row.vtype.encode(value), "address_list")
    row.value = value


def freeze(session: Session, freezer: Freezer, row: AddressRow, value: float | int) -> None:
    """Keep writing ``value`` (the Freezer's rate, at least 10 a second; AC-2.3)."""
    game = session.require_game()
    freezer.freeze(row.key, [(game.pid, row.address, row.vtype.encode(value))])
    row.frozen = value
    log.info("action=pro_freeze addr=%#x type=%s", row.address, row.vtype.name)


def unfreeze(freezer: Freezer, row: AddressRow) -> None:
    freezer.unfreeze(row.key)
    row.frozen = None


def as_cheat(session: Session, row: AddressRow) -> Cheat:
    """The cheat this row would make: its value written at its place, frozen when the
    row is (a heap address is this session only)."""
    kind, offset = session.classify(row.address)
    value = row.frozen if row.frozen is not None else row.value
    data = row.vtype.encode(value if value is not None else 0)
    return Cheat(
        row.description or place_text(row.address, session.game),
        [Location(kind, offset, data, data)],
        freeze=row.frozen is not None,
        value_type=row.vtype,
    )


def row_badge(session: Session, row: AddressRow) -> Badge:
    """PC only on the heap; CheatRunner, "written once", for a frozen static (AC-2.3)."""
    return badge(as_cheat(session, row))


__all__ = [
    "BAD_PLACE",
    "AddressRow",
    "as_cheat",
    "freeze",
    "freeze_key",
    "parse_place",
    "place_text",
    "read_values",
    "row_badge",
    "set_value",
    "unfreeze",
]

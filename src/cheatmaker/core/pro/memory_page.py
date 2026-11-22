"""A 4 KiB page of game memory for the Pro Memory view (spec PR-7, read-only in R1).

Only the parts of the page inside readable memory maps are read; everything else is
unknown and shown as ``??`` (AC-7.1). NG answers reads in unmapped gaps with zeros, so
the map, not a read error, says what is unknown. A read that fails anyway leaves its
part unknown too, and never drops the connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cheatmaker.core.session import Session
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ConsoleBusy, ReadFailed

log = logging.getLogger(__name__)

PAGE = 0x1000
ROW = 16
UNKNOWN = "??"
INSPECT_TYPES = (
    ValueType.INT8,
    ValueType.UINT8,
    ValueType.INT16,
    ValueType.UINT16,
    ValueType.INT32,
    ValueType.UINT32,
    ValueType.INT64,
    ValueType.UINT64,
    ValueType.FLOAT,
    ValueType.DOUBLE,
)
"""Every PR-2 number type, in the order the value inspector lists them."""


@dataclass(frozen=True)
class MemPage:
    start: int
    data: tuple[int | None, ...]  # one entry per byte; None: unknown

    def known(self, addr: int, size: int) -> bytes | None:
        """``size`` bytes at ``addr`` when they are all on the page and known."""
        at = addr - self.start
        if at < 0 or at + size > len(self.data):
            return None
        part = self.data[at : at + size]
        if any(b is None for b in part):
            return None
        return bytes(part)  # type: ignore[arg-type]


def page_start(addr: int) -> int:
    return addr & ~(PAGE - 1)


def read_page(session: Session, addr: int) -> MemPage:
    """The 4 KiB page holding ``addr``."""
    game = session.require_game()
    client = session.require_client()
    start = page_start(addr)
    end = start + PAGE
    data: list[int | None] = [None] * PAGE
    for m in client.maps(game.pid):
        lo, hi = max(m.start, start), min(m.end, end)
        if lo >= hi or not m.readable:
            continue
        try:
            got = client.read(game.pid, lo, hi - lo)
        except ConsoleBusy:
            raise
        except ReadFailed as exc:
            log.info("action=pro_memory addr=%#x len=%d unreadable: %s", lo, hi - lo, exc)
            continue
        data[lo - start : lo - start + len(got)] = got
    return MemPage(start, tuple(data))


def hex_cell(b: int | None) -> str:
    return UNKNOWN if b is None else f"{b:02X}"


def text_cell(row: tuple[int | None, ...]) -> str:
    return "".join("?" if b is None else chr(b) if 0x20 <= b < 0x7F else "." for b in row)


def inspect(page: MemPage, addr: int) -> list[tuple[ValueType, str]]:
    """The value at ``addr`` as every PR-2 type; ``??`` where a byte is unknown or the
    value runs off the page."""
    out = []
    for vt in INSPECT_TYPES:
        raw = page.known(addr, vt.size)
        out.append((vt, UNKNOWN if raw is None else vt.text(vt.decode(raw))))
    return out


__all__ = [
    "INSPECT_TYPES",
    "PAGE",
    "ROW",
    "UNKNOWN",
    "MemPage",
    "hex_cell",
    "inspect",
    "page_start",
    "read_page",
    "text_cell",
]

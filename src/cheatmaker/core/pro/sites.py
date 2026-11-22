"""Where Pro cheats write (architecture-pro "Turning cheats on and off").

Every range here is a module offset range ``(start, end)``; turning it into console
addresses is the caller's job (``module_base + offset``). Guided cheats' lines are
compared the same way, so a Pro cheat can't be turned on over any enabled cheat
(AC-8.8; this generalises ``cheats._refuse_overlap`` from one cheat's parts to all
enabled cheats).
"""

from __future__ import annotations

from collections.abc import Iterable

from cheatmaker.core import cave as cv
from cheatmaker.core.cheats import MAIN_MODULE, Cheat, LocationKind
from cheatmaker.core.pro.model import ProCheat, ProEntry
from cheatmaker.core.session import Session
from cheatmaker.errors import CheatMakerError

Range = tuple[int, int]
CAVE_GAP = 32
"""``find_cave`` wants this many zero bytes more than the cave it places."""
SPACER = 16
"""Zero bytes a growing cave leaves before the next thing in the code."""


class Overlap(CheatMakerError):
    """Two enabled cheats would write the same bytes."""


def entry_ranges(entry: ProEntry) -> list[Range]:
    """The bytes an entry changes: the site, and for a placed cave its cave."""
    out = [(entry.offset, entry.offset + len(entry.off))]
    if entry.kind == "cave" and entry.cave is not None and entry.cave_bytes:
        out.append((entry.cave, entry.cave + len(entry.cave_bytes)))
    return out


def pro_ranges(pc: ProCheat) -> list[Range]:
    return [r for e in pc.entries for r in entry_ranges(e)]


def cheat_ranges(cheat: Cheat, module_base: int) -> list[Range]:
    """A cheat's lines as module offset ranges (a Pro cheat's own entries when it has
    them). Heap lines are converted from their absolute address; lines of other
    modules or found-again parts (no fixed place) are left out."""
    if cheat.pro is not None:
        return pro_ranges(cheat.pro)
    out = []
    for loc in cheat.locations:
        if loc.anchor is not None or loc.module != MAIN_MODULE:
            continue
        start = loc.offset - module_base if loc.kind is LocationKind.SESSION_ADDRESS else loc.offset
        out.append((start, start + len(loc.on)))
    return out


def _overlaps(a: Iterable[Range], b: Iterable[Range]) -> bool:
    bs = list(b)
    return any(s1 < e2 and s2 < e1 for s1, e1 in a for s2, e2 in bs)


def refuse_overlap(pc: ProCheat, enabled: Iterable[Cheat], module_base: int) -> None:
    """Overlap naming the first enabled cheat that writes any byte ``pc`` writes."""
    mine = pro_ranges(pc)
    for other in enabled:
        if other.pro is not None and other.pro.id == pc.id:
            continue
        if _overlaps(mine, cheat_ranges(other, module_base)):
            raise Overlap(f"It overlaps “{other.name}”, which is on. Turn that off first.")


def open_rooms(cheats: Iterable[Cheat], skip: str | None = None) -> list[Range]:
    """The code lines of the cheats in My cheats (as ``build_dialog._open_rooms``): a
    new cave never goes there. ``skip``: the Pro cheat being built (its own lines)."""
    return [
        (loc.offset, loc.offset + len(loc.on))
        for c in cheats
        if c.pro is None or c.pro.id != skip
        for loc in c.locations
        if loc.kind is LocationKind.CODE_PATCH
    ]


def cave_rooms(cheats: Iterable[ProCheat], skip: str | None = None) -> list[Range]:
    """The placed caves of ``cheats`` (all but the one with id ``skip``): rooms no new
    cave may take, and that guided caves keep out of (``session.reserved_rooms``)."""
    return [
        (e.cave, e.cave + max(len(e.cave_bytes), 1))
        for pc in cheats
        if pc.id != skip
        for e in pc.entries
        if e.kind == "cave" and e.cave is not None
    ]


def _tail(session: Session) -> tuple[int, bytearray]:
    """The last 64 KiB of the game's code (where caves go) as (address, bytes)."""
    game = session.require_game()
    client = session.require_client()
    code = [
        m
        for m in client.maps(game.pid)
        if m.executable and game.module_base <= m.start < game.module_end
    ]
    if not code:
        raise CheatMakerError("couldn't find the game's code")
    last = max(code, key=lambda m: m.end)
    start = max(last.start, last.end - cv.SEARCH_TAIL)
    return start, bytearray(client.read(game.pid, start, last.end - start))


def _masked_tail(session: Session, reserved: Iterable[Range]) -> tuple[int, bytearray]:
    """``_tail`` with every reserved room (module offsets) marked as not free."""
    start, data = _tail(session)
    base = session.require_game().module_base
    for lo, hi in [*session.reserved_rooms, *reserved]:
        a, b = max(base + lo, start), min(base + hi, start + len(data))
        if a < b:
            data[a - start : b - start] = b"\xff" * (b - a)
    return start, data


def free_room(session: Session, reserved: Iterable[Range] = ()) -> int:
    """The biggest new cave that fits right now (the cave budget shown in the editor):
    the longest zero run in the last 64 KiB of code, minus ``session.reserved_rooms``,
    minus ``reserved`` (e.g. Pro caves), minus ``find_cave``'s gap and alignment."""
    _start, data = _masked_tail(session, reserved)
    best = run = 0
    for byte in data:
        run = run + 1 if byte == 0 else 0
        best = max(best, run)
    return max(0, best - CAVE_GAP - 15)  # 15: the worst 16-byte alignment loss


def room_at(session: Session, cave: int, own: int, reserved: Iterable[Range] = ()) -> int:
    """How big the placed cave at module offset ``cave`` (now ``own`` bytes) can grow:
    its own bytes plus the zero bytes after them, keeping ``SPACER`` zero bytes before
    whatever follows. ``reserved`` must not hold the cave itself."""
    start, data = _masked_tail(session, reserved)
    at = session.require_game().module_base + cave - start
    if not 0 <= at < len(data):
        return own
    end = at + own
    while end < len(data) and data[end] == 0:
        end += 1
    if end < len(data):  # something follows: leave it a gap
        end = max(at + own, end - SPACER)
    return end - at


__all__ = [
    "CAVE_GAP",
    "SPACER",
    "Overlap",
    "Range",
    "cave_rooms",
    "cheat_ranges",
    "entry_ranges",
    "free_room",
    "open_rooms",
    "pro_ranges",
    "refuse_overlap",
    "room_at",
]

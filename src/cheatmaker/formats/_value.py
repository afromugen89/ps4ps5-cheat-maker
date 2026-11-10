"""Which bytes of a cheat are its editable value, kept in the file as Cheat Maker's own
extra field. CheatRunner and GoldHEN ignore it (they look fields up by name), so the
file still loads there; Cheat Maker reads it back so the value stays editable."""

from __future__ import annotations

from cheatmaker.core.cheats import Cheat
from cheatmaker.core.values import ValueType


def type_text(cheat: Cheat) -> str | None:
    """The value type to write, or None when the cheat has no editable value."""
    if cheat.value_type is None or cheat.value is None:
        return None
    return cheat.value_type.name.lower()


def at_pairs(cheat: Cheat) -> list[list[int]] | None:
    return None if cheat.value_at is None else [[i, b] for i, b in cheat.value_at]


def at_text(cheat: Cheat) -> str | None:
    return None if cheat.value_at is None else " ".join(f"{i}:{b}" for i, b in cheat.value_at)


def parse_at_text(text: str) -> list[tuple[int, int]]:
    """``"0:12 0:13"`` -> [(0, 12), (0, 13)]. Raises ValueError."""
    pairs = []
    for part in text.split():
        i, _, b = part.partition(":")
        pairs.append((int(i), int(b)))
    return pairs


def apply(cheat: Cheat, vtype: object, at: object) -> None:
    """Give a loaded cheat its value back. Anything that doesn't fit the cheat's bytes
    is ignored: the cheat still loads, its value just can't be edited."""
    if not isinstance(vtype, str):
        return
    try:
        vt = ValueType[vtype.upper()]
        pairs = None
        if at is not None:
            if isinstance(at, str):
                pairs = parse_at_text(at)
            elif isinstance(at, list):
                pairs = [(int(p[0]), int(p[1])) for p in at if isinstance(p, list) and len(p) == 2]
                if len(pairs) != len(at):
                    return
            else:
                return
    except (KeyError, ValueError, TypeError):
        return
    if pairs is None:
        if any(len(loc.on) != vt.size for loc in cheat.locations):
            return
    elif len(pairs) != vt.size or any(
        not 0 <= i < len(cheat.locations) or not 0 <= b < len(cheat.locations[i].on)
        for i, b in pairs
    ):
        return
    cheat.value_type, cheat.value_at = vt, pairs

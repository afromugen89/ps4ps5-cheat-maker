"""Import cheat files into Pro (spec PR-14; architecture-pro "Export pipeline").

A ``.json`` / ``.shn`` / ``.mc4`` file's cheats become Pro cheats (origin "import"),
one Bytes entry per line, exactly as in the file. Lines in a section other than 0 (or
another module) are refused with a message: Pro only patches the game's main code.
Caves (``updater.is_cave``: new code over zero bytes) are shown disassembled, and
"Convert to assembly" turns each cave and the jump to it into a Cave entry whose
source assembles back to the same bytes (AC-14.1), using ``asm_text``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import iced_x86 as ix

from cheatmaker import formats
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import MAIN_MODULE, Cheat, CheatFile, Location, LocationKind
from cheatmaker.core.pro.asm import Assembler, insn_text
from cheatmaker.core.pro.asm_text import CantConvert, cave_entries
from cheatmaker.core.pro.model import ProCheat, ProEntry, new_cheat_id

log = logging.getLogger(__name__)

OTHER_SECTION = (
    "a line is in section {section}; only section 0 (the game's main code) can be imported"
)
OTHER_MODULE = "a line is in {module}, not the game's main code"


@dataclass
class ImportResult:
    cheats: list[ProCheat] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)  # cheat name -> why


def _why_not(cheat: Cheat) -> str | None:
    for loc in cheat.locations:
        if loc.section != 0:
            return OTHER_SECTION.format(section=loc.section)
        if loc.module != MAIN_MODULE:
            return OTHER_MODULE.format(module=loc.module)
        if loc.kind is not LocationKind.CODE_PATCH:
            return "a line isn't a code patch"
    if not cheat.locations:
        return "it has no lines"
    return None


def import_cheats(cf: CheatFile, existing: Sequence[ProCheat] = ()) -> ImportResult:
    """Every cheat of ``cf`` as a Pro cheat with one Bytes entry per line, or refused
    with why. Ids follow on from ``existing``."""
    result = ImportResult()
    taken = list(existing)
    for cheat in cf.cheats:
        why = _why_not(cheat)
        if why is not None:
            result.refused[cheat.name] = why
            log.info("action=pro_import cheat=%s result=refused reason=%s", cheat.name, why)
            continue
        entries = [ProEntry("bytes", loc.offset, loc.off, loc.on) for loc in cheat.locations]
        pc = ProCheat(new_cheat_id(taken), cheat.name, entries, origin="import")
        taken.append(pc)
        result.cheats.append(pc)
    log.info(
        "action=pro_import title=%s version=%s cheats=%d refused=%d",
        cf.title_id,
        cf.version,
        len(result.cheats),
        len(result.refused),
    )
    return result


def import_path(path: str | Path, existing: Sequence[ProCheat] = ()) -> ImportResult:
    """``import_cheats`` for a file (format sniffed from its content as on load)."""
    return import_cheats(formats.load_path(path), existing)


def _location(entry: ProEntry) -> Location:
    return Location(LocationKind.CODE_PATCH, entry.offset, entry.on, entry.off)


def is_cave(entry: ProEntry) -> bool:
    return entry.kind == "bytes" and up.is_cave(_location(entry))


def disassembly(entry: ProEntry) -> list[str]:
    """A cave's bytes as NASM lines for display (not checked: see ``convert``); bytes
    that don't decode are shown as ``db``."""
    lines = []
    for insn in ix.Decoder(64, entry.on, ip=entry.offset):
        if insn.code == ix.Code.INVALID:
            at = insn.ip - entry.offset
            raw = entry.on[at : at + insn.len]
            lines.append("db " + ", ".join(f"{b:#04x}" for b in raw))
        else:
            lines.append(insn_text(insn))
    return lines


def convert(pc: ProCheat, assembler: Assembler) -> int:
    """ "Convert to assembly": each cave of ``pc`` and the jump line to it become one
    Cave entry with assembly source; other entries stay. Returns how many caves were
    converted (0: there is no cave reached by a jump in this cheat). CantConvert, with
    the offset of the bytes that don't decode, leaves ``pc`` unchanged."""
    if any(e.kind == "cave" for e in pc.entries):
        raise CantConvert("this cheat already has assembly entries")
    locations = [_location(e) for e in pc.entries]
    try:
        entries = cave_entries(locations, assembler)
    except CantConvert as exc:
        raise CantConvert(f"{pc.name}: {exc}") from None
    caves = sum(e.kind == "cave" for e in entries)
    if caves:
        # nop lines stay Bytes entries, as imported
        pc.entries = [
            ProEntry("bytes", e.offset, e.off, e.on) if e.kind == "nop" else e for e in entries
        ]
        log.info("action=pro_import_convert id=%s caves=%d", pc.id, caves)
    return caves


__all__ = [
    "OTHER_MODULE",
    "OTHER_SECTION",
    "ImportResult",
    "convert",
    "disassembly",
    "import_cheats",
    "import_path",
    "is_cave",
]

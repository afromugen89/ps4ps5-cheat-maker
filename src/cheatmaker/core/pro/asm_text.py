"""Machine code back to cave source (architecture-pro "Guided caves to text").

``to_source`` turns a cave's bytes into NASM text that assembles to exactly those
bytes: branch targets inside the cave become ``L1:`` labels, the hook-back target
becomes ``return`` (and the site ``hook``), and RIP-relative operands keep their
address. NASM may choose another encoding than the one in the cave (a shorter
immediate or displacement); such a line is written as ``db 0x.., ..`` with the
instruction as a comment, so the bytes stay exact. Only bytes that don't decode at all
are refused, naming their offset.

``adopt`` makes a guided cheat (Lock it, Make it work, Find it next time hooks) into a
``ProCheat`` whose cave entries hold that text (AC-S1).

Sources are assembled at **module offsets** (as if the module sat at 0): the caves are
RIP-relative, so the bytes are the same at any module base.
"""

from __future__ import annotations

import logging
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass

import iced_x86 as ix

from cheatmaker.core import updater as up
from cheatmaker.core.cheats import MAIN_MODULE, Cheat, Location
from cheatmaker.core.pro.asm import Assembler, CaveContext, insn_text
from cheatmaker.core.pro.model import JMP_REL32_SIZE, NOP, ProCheat, ProEntry
from cheatmaker.errors import CheatMakerError

log = logging.getLogger(__name__)

INDENT = "    "
_MARK = "__cm_i"
"""Marker labels put before each instruction while checking (never in the result)."""


class CantConvert(CheatMakerError):
    """The bytes can't be written as assembly (they don't decode)."""


@dataclass
class _Line:
    insn: ix.Instruction
    raw: bytes
    text: str
    as_db: bool = False

    def source(self) -> str:
        if not self.as_db:
            return self.text
        return "db " + ", ".join(f"{b:#04x}" for b in self.raw) + f" ; {self.text}"


def decode(code: bytes, ip: int) -> list[ix.Instruction]:
    """Every instruction in ``code``; CantConvert names the first that doesn't decode."""
    insns = list(ix.Decoder(64, code, ip=ip))
    for insn in insns:
        if insn.code == ix.Code.INVALID:
            at = insn.ip - ip
            raw = code[at : at + 8].hex(" ")
            raise CantConvert(f"the bytes at +{at:#x} ({raw}) aren't an instruction")
    return insns


def _names(insns: list[ix.Instruction], ctx: CaveContext) -> dict[int, str]:
    """Address -> name: the context's symbols, then ``L1``... for every instruction
    a branch or RIP-relative operand in the code points at."""
    names: dict[int, str] = {addr: name for name, addr in ctx.symbols.items()}
    if ctx.hook is not None:
        names[ctx.hook] = "hook"
        names[ctx.hook + len(ctx.original)] = "return"
    starts = {i.ip for i in insns}
    wanted = set()
    for insn in insns:
        if _branch_target(insn) in starts:
            wanted.add(_branch_target(insn))
        if insn.is_ip_rel_memory_operand and insn.ip_rel_memory_address in starts:
            wanted.add(insn.ip_rel_memory_address)
    taken = set(names.values())
    n = 0
    for addr in sorted(wanted - names.keys()):
        n += 1
        while f"L{n}" in taken:
            n += 1
        names[addr] = f"L{n}"
    return names


def _branch_target(insn: ix.Instruction) -> int | None:
    if insn.op_count and insn.op_kind(0) in (ix.OpKind.NEAR_BRANCH64, ix.OpKind.NEAR_BRANCH32):
        return insn.near_branch_target
    return None


def _text(insn: ix.Instruction, names: dict[int, str]) -> str:
    text = insn_text(insn)
    target = _branch_target(insn)
    if target is not None:
        mnemonic, _, rest = text.partition(" ")
        if insn.len >= JMP_REL32_SIZE and not re.match(r"(short|near) ", rest):
            rest = "near " + rest  # keep the rel32 form (NASM would pick short)
        if target in names:
            head, _, _ = rest.rpartition(f"{target:#x}")
            rest = head + names[target]
        text = f"{mnemonic} {rest}"
    if insn.is_ip_rel_memory_operand and insn.ip_rel_memory_address in names:
        addr = insn.ip_rel_memory_address
        text = text.replace(f"[rel {addr:#x}]", f"[rel {names[addr]}]")
    elif _long_displacement(insn):  # NASM would use the 1-byte form
        text = text.replace("[", "[dword ", 1)
    return text


def _long_displacement(insn: ix.Instruction) -> bool:
    """A 4-byte displacement that would fit in 1 byte (the game's compiler or our
    encoder chose the long form; ``[dword reg+d]`` keeps it)."""
    if not any(insn.op_kind(i) == ix.OpKind.MEMORY for i in range(insn.op_count)):
        return False
    if insn.memory_base in (ix.Register.NONE, ix.Register.RIP):
        return False
    disp = insn.memory_displacement
    disp = disp - (1 << 64) if disp >= 1 << 63 else disp
    # memory_displ_size is 8 for a sign-extended disp32 in 64-bit mode
    return insn.memory_displ_size in (4, 8) and -128 <= disp <= 127


def _uses_name(insn: ix.Instruction, names: dict[int, str]) -> bool:
    """True when the line's bytes depend on where a label lands."""
    if _branch_target(insn) in names:
        return True
    return insn.is_ip_rel_memory_operand and insn.ip_rel_memory_address in names


def _render(lines: list[_Line], names: dict[int, str], marks: bool = False) -> str:
    out = []
    for k, line in enumerate(lines):
        name = names.get(line.insn.ip)
        if name is not None and name.startswith("L"):
            out.append(f"{name}:")
        if marks:
            out.append(f"{_MARK}{k}:")
        out.append(INDENT + line.source())
    return "\n".join(out) + "\n"


def to_source(code: bytes, ctx: CaveContext, assembler: Assembler) -> str:
    """NASM text for ``code`` at ``ctx.org`` that ``assembler`` turns back into exactly
    ``code`` (with ``ctx``'s hook, return and symbols). CantConvert if a byte doesn't
    decode."""
    if not code:
        return ""
    insns = decode(code, ctx.org)
    names = _names(insns, ctx)
    lines = [
        _Line(i, code[i.ip - ctx.org : i.ip - ctx.org + i.len], _text(i, names)) for i in insns
    ]
    for _ in range(len(lines) + 1):
        bad = _differing(lines, names, code, ctx, assembler)
        if not bad:
            break
        for k in bad:
            lines[k].as_db = True
    source = _render(lines, names)
    result = assembler.assemble(source, ctx)
    if result.code != code:  # every line is checked above; this is a safety net
        raise CantConvert("the assembly doesn't give the same bytes back")
    n_db = sum(line.as_db for line in lines)
    if n_db:
        log.info("action=pro_asm_text lines=%d db=%d", len(lines), n_db)
    return source


def _differing(
    lines: list[_Line],
    names: dict[int, str],
    code: bytes,
    ctx: CaveContext,
    assembler: Assembler,
) -> list[int]:
    """The lines to write as ``db`` next: each line that assembles to other bytes, up
    to the first whose length differs (after it every address moves)."""
    result = assembler.assemble(_render(lines, names, marks=True), ctx)
    if result.error is not None:
        line_no, msg = result.error
        k = _line_index(lines, names, line_no)
        if k is None or lines[k].as_db:
            log.warning("action=pro_asm_text_error line=%d msg=%s", line_no, msg)
            return [k for k, line in enumerate(lines) if not line.as_db]
        return [k]
    bad = []
    for k, line in enumerate(lines):
        start = result.labels.get(f"{_MARK}{k}")
        end = result.labels.get(f"{_MARK}{k + 1}", ctx.org + len(result.code))
        if start is None:
            return [j for j, ln in enumerate(lines) if not ln.as_db]
        got = result.code[start - ctx.org : end - ctx.org]
        if got == line.raw:
            continue
        if len(got) != len(line.raw):
            # Every later address moves, so a line before it that refers to a label
            # may differ only because of this one: fix this first and look again.
            return [j for j in bad if not _uses_name(lines[j].insn, names)] + [k]
        bad.append(k)
    if not bad and result.code != code:
        return [k for k, line in enumerate(lines) if not line.as_db]
    return bad


def _line_index(lines: list[_Line], names: dict[int, str], line_no: int) -> int | None:
    """Which instruction a line of the marked source (``_render(marks=True)``) is."""
    n = 0
    for k, line in enumerate(lines):
        name = names.get(line.insn.ip)
        n += 1 if name is not None and name.startswith("L") else 0
        n += 2  # the marker, then the instruction
        if n == line_no:
            return k
        if n > line_no:
            return None
    return None


# -- adopting guided cheats ------------------------------------------------------------
def _jump_target(loc: Location) -> int | None:
    """The module offset a ``jmp rel32`` + NOPs line jumps to, else None."""
    on = loc.on
    if len(on) < JMP_REL32_SIZE or on[0] != 0xE9:
        return None
    if any(b != NOP for b in on[JMP_REL32_SIZE:]):
        return None
    return loc.offset + JMP_REL32_SIZE + struct.unpack_from("<i", on, 1)[0]


def _is_routine(loc: Location) -> bool:
    """A line that is a cave of its own: new code over spare zeros (``updater.is_cave``)
    or a routine left in place when off (on == off, as the guided builder writes).
    Any other line patches the game's code and must stay a Bytes entry, or turning it
    off would no longer put the game's bytes back."""
    return up.is_cave(loc) or loc.on == loc.off


def _check_adoptable(cheat: Cheat) -> None:
    if not cheat.locations:
        raise CantConvert(f"{cheat.name}: there is nothing to write")
    if cheat.session_only or cheat.anchor is not None or cheat.learned_parts:
        raise CantConvert(f"{cheat.name}: its address is found each run, so it is PC only")
    for loc in cheat.locations:
        if loc.module != MAIN_MODULE or loc.section != 0:
            raise CantConvert(f"{cheat.name}: a line is outside the game's main code")


def cave_entries(
    locations: Sequence[Location], assembler: Assembler, comment: str = ""
) -> list[ProEntry]:
    """Pro entries for cheat lines: a jump line and the cave it jumps to (a line over
    spare zeros, or one that is the same on and off) make one cave entry (its text from
    ``to_source``); every other line is a Bytes (or NOP) entry."""
    by_offset = {loc.offset: k for k, loc in enumerate(locations)}
    caves: dict[int, int] = {}  # jump line -> cave line
    for k, loc in enumerate(locations):
        target = _jump_target(loc)
        j = by_offset.get(target, k) if target is not None else k
        if j != k and j not in caves.values() and _is_routine(locations[j]):
            caves[k] = j
    used = set(caves.values())
    entries = []
    for k, loc in enumerate(locations):
        if k in used:
            continue
        if k in caves:
            cave = locations[caves[k]]
            ctx = CaveContext(cave.offset, loc.offset, loc.off)
            entries.append(
                ProEntry(
                    "cave",
                    loc.offset,
                    loc.off,
                    loc.on,
                    source=to_source(cave.on, ctx, assembler),
                    cave=cave.offset,
                    cave_bytes=cave.on,
                    comment=comment,
                )
            )
        elif loc.on == bytes([NOP]) * len(loc.on):
            entries.append(ProEntry("nop", loc.offset, loc.off, comment=comment))
        else:
            entries.append(ProEntry("bytes", loc.offset, loc.off, loc.on, comment=comment))
    return entries


def adopt(cheat: Cheat, cheat_id: str, assembler: Assembler) -> ProCheat:
    """The guided ``cheat`` as a Pro cheat (origin "guided"), byte for byte: a cave and
    its jump become one cave entry, other lines Bytes/NOP entries. CantConvert for a
    cheat that is PC only (found each run) or whose cave doesn't decode."""
    if cheat.pro is not None:
        return cheat.pro
    _check_adoptable(cheat)
    entries = cave_entries(cheat.locations, assembler)
    return ProCheat(cheat_id, cheat.name, entries, origin="guided", comment=cheat.hint or "")


__all__ = ["INDENT", "CantConvert", "adopt", "cave_entries", "decode", "to_source"]

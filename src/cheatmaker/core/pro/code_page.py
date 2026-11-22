"""A page of live disassembly for the Pro Code view (spec PR-6).

``read_page`` reads up to ``PAGE_BYTES`` around an address (inside its memory map,
never across a hole); ``lines`` decodes ``PAGE_INSNS`` instructions in NASM syntax,
starting a little before the address on a boundary that lands on it, so the
instruction asked for is on the page with some context above it. Cheat sites are
marked with the cheat's name, and "show original" puts the off-bytes of the cheats
that are on back before decoding (AC-6.2).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import iced_x86 as ix

from cheatmaker.core import updater as up
from cheatmaker.core.cheats import Cheat, LocationKind
from cheatmaker.core.pro.asm import insn_text
from cheatmaker.core.session import Session
from cheatmaker.errors import ReadFailed

PAGE_INSNS = 256
PAGE_BYTES = PAGE_INSNS * 15  # the longest x86 instruction is 15 bytes
CONTEXT = 64  # bytes before the address tried for a decode that lands on it
_BRANCHES = (ix.OpKind.NEAR_BRANCH16, ix.OpKind.NEAR_BRANCH32, ix.OpKind.NEAR_BRANCH64)


@dataclass(frozen=True)
class Page:
    start: int  # address of ``code[0]``
    code: bytes
    target: int  # the address asked for


@dataclass(frozen=True)
class Line:
    ip: int
    raw: bytes
    text: str
    target: int | None = None  # a near jump or call goes here
    mark: str = ""  # the cheat(s) writing here


@dataclass(frozen=True)
class Site:
    start: int
    end: int
    name: str
    on: bool
    off: bytes  # the game's own bytes at [start, end)


def read_page(session: Session, addr: int) -> Page:
    """The bytes around ``addr`` within its memory map. ReadFailed when ``addr`` isn't
    mapped."""
    game = session.require_game()
    client = session.require_client()
    m = next((m for m in client.maps(game.pid) if m.contains(addr)), None)
    if m is None or not m.readable:
        raise ReadFailed(f"{addr:#x} isn't in the game's memory")
    start = max(m.start, addr - CONTEXT)
    end = min(m.end, start + PAGE_BYTES)
    return Page(start, client.read(game.pid, start, end - start), addr)


def cheat_sites(cheats: Iterable[Cheat], module_base: int) -> list[Site]:
    """Every code line of ``cheats`` as an address range with its cheat's name."""
    out = []
    for c in cheats:
        for loc in c.locations:
            if loc.kind is LocationKind.CODE_PATCH and loc.anchor is None:
                a = loc.address(module_base)
                out.append(Site(a, a + len(loc.on), c.name, c.enabled, loc.off))
    return out


def with_originals(page: Page, sites: Iterable[Site]) -> bytes:
    """The page's bytes with the off-bytes of every cheat that is on put back."""
    code = bytearray(page.code)
    for s in sites:
        if not s.on:
            continue
        lo, hi = max(s.start, page.start), min(s.end, page.start + len(code))
        if lo < hi:
            code[lo - page.start : hi - page.start] = s.off[lo - s.start : hi - s.start]
    return bytes(code)


def _lands(code: bytes, start: int, offset: int, target: int) -> bool:
    for insn in ix.Decoder(64, code[offset:], ip=start + offset):
        if insn.ip >= target:
            return insn.ip == target and insn.code != ix.Code.INVALID
    return False


def first_offset(code: bytes, start: int, target: int) -> int:
    """The earliest offset to decode from whose stream lands on ``target``."""
    at = target - start
    for offset in range(0, max(at, 0)):
        if _lands(code, start, offset, target):
            return offset
    return max(at, 0)


def _mark(ip: int, size: int, sites: Iterable[Site]) -> str:
    names = []
    for s in sites:
        if s.start < ip + size and ip < s.end and s.name not in names:
            names.append(s.name + (" (on)" if s.on else ""))
    return ", ".join(names)


def lines(page: Page, sites: Iterable[Site] = (), original: bool = False) -> list[Line]:
    """Up to ``PAGE_INSNS`` instructions of the page; bytes that don't decode are ``db``."""
    sites = list(sites)
    code = with_originals(page, sites) if original else page.code
    offset = first_offset(code, page.start, page.target)
    out = []
    for insn in ix.Decoder(64, code[offset:], ip=page.start + offset):
        raw = code[insn.ip - page.start : insn.ip - page.start + insn.len]
        mark = _mark(insn.ip, insn.len, sites)
        if insn.code == ix.Code.INVALID:
            text, target = "db " + ", ".join(f"{b:#04x}" for b in raw), None
        else:
            text = insn_text(insn)
            target = insn.near_branch_target if insn.op0_kind in _BRANCHES else None
        out.append(Line(insn.ip, raw, text, target, mark))
        if len(out) >= PAGE_INSNS:
            break
    return out


def aob(page: Page, ip: int, original: bool = False, sites: Iterable[Site] = ()) -> str:
    """A signature from the instruction at ``ip``: whole instructions, jump distances
    and RIP-relative addresses as ``??`` (``updater.make_signature``)."""
    code = with_originals(page, sites) if original else page.code
    return up.make_signature(code[ip - page.start :], ip).text()


__all__ = [
    "CONTEXT",
    "PAGE_BYTES",
    "PAGE_INSNS",
    "Line",
    "Page",
    "Site",
    "aob",
    "cheat_sites",
    "first_offset",
    "lines",
    "read_page",
    "with_originals",
]

"""core/pro/code_page: the Code view's page (spec PR-6)."""

from __future__ import annotations

import pytest

from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.pro import code_page as cp
from cheatmaker.errors import ReadFailed
from tests import fakegame as fg


@pytest.fixture
def game():
    return fg.build(1)


def test_page_lands_on_the_address_with_context(game):
    page = cp.read_page(game.session, fg.WRITER)
    assert page.start == fg.WRITER - cp.CONTEXT and page.target == fg.WRITER
    rows = cp.lines(page)
    (hit,) = [r for r in rows if r.ip == fg.WRITER]
    assert hit.text.startswith("vmovss") and hit.raw == fg.VMOVSS_W
    assert rows[0].ip < fg.WRITER, "lines above it"
    assert len(rows) <= cp.PAGE_INSNS


def test_page_stays_inside_its_map(game):
    page = cp.read_page(game.session, fg.BASE + 0x10)
    assert page.start == fg.BASE
    end = cp.read_page(game.session, fg.DATA - 4)
    assert end.start + len(end.code) == fg.DATA


def test_unmapped_address(game):
    with pytest.raises(ReadFailed):
        cp.read_page(game.session, 0x10)


def test_first_offset_skips_a_misaligned_start():
    # 0x48 starts "mov rax, imm64" (10 bytes) that would swallow the target
    code = b"\x48\xb8" + b"\x90" * 8 + b"\xc3"
    assert cp.first_offset(code, 0x1000, 0x1002) == 2
    assert cp.first_offset(b"\x90\x90\xc3", 0x1000, 0x1002) == 0


def _jump_cheat(on: bool) -> Cheat:
    hook = fg.WRITER - fg.BASE
    jump = b"\xe9" + (0x3F00 - (hook + 5)).to_bytes(4, "little", signed=True) + b"\x90" * 5
    c = Cheat("Infinite health", [Location(LocationKind.CODE_PATCH, hook, jump, fg.VMOVSS_W)])
    c.enabled = on
    return c


def test_cheat_sites_are_marked_and_original_shown(game):
    """AC-6.2."""
    cheat = _jump_cheat(on=True)
    loc = cheat.locations[0]
    game.fake.poke(fg.PID, fg.WRITER, loc.on)
    sites = cp.cheat_sites([cheat], fg.BASE)
    page = cp.read_page(game.session, fg.WRITER)
    (hit,) = [r for r in cp.lines(page, sites) if r.ip == fg.WRITER]
    assert hit.text.startswith("jmp") and hit.target == fg.BASE + 0x3F00
    assert hit.mark == "Infinite health (on)"
    (orig,) = [r for r in cp.lines(page, sites, original=True) if r.ip == fg.WRITER]
    assert orig.text.startswith("vmovss") and orig.mark == "Infinite health (on)"


def test_original_ignores_cheats_that_are_off(game):
    cheat = _jump_cheat(on=False)
    page = cp.read_page(game.session, fg.WRITER)
    assert cp.with_originals(page, cp.cheat_sites([cheat], fg.BASE)) == page.code


def test_aob_wildcards_branch_distances(game):
    cheat = _jump_cheat(on=True)
    game.fake.poke(fg.PID, fg.WRITER, cheat.locations[0].on)
    page = cp.read_page(game.session, fg.WRITER)
    assert cp.aob(page, fg.WRITER).startswith("E9 ?? ?? ?? ?? 90")
    sites = cp.cheat_sites([cheat], fg.BASE)
    assert cp.aob(page, fg.WRITER, True, sites).startswith("C4 C1 7A 11 8C 24 AC 02 00 00")

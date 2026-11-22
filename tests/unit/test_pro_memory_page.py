"""core/pro/memory_page: the Memory view's page (spec PR-7, AC-7.1)."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.core.pro import memory_page as mp
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ReadFailed
from tests import fakegame as fg


@pytest.fixture
def game():
    return fg.build(1)


def test_page_is_aligned_and_read(game):
    page = mp.read_page(game.session, fg.WRITER + 3)
    assert page.start == fg.BASE and len(page.data) == mp.PAGE
    assert page.known(fg.WRITER, 10) == fg.VMOVSS_W


def test_bytes_outside_the_maps_are_unknown(game):
    """AC-7.1: the map says what is unknown; the connection stays."""
    end = fg.HEAP + fg.HEAP_SIZE
    page = mp.read_page(game.session, end)
    assert all(b is None for b in page.data)
    assert mp.hex_cell(page.data[0]) == "??"
    assert game.session.connected


def test_a_failed_read_leaves_its_part_unknown(game, monkeypatch):
    real = game.fake.read

    def flaky(pid, addr, n):
        if addr == fg.HEAP:
            raise ReadFailed("unreadable")
        return real(pid, addr, n)

    monkeypatch.setattr(game.fake, "read", flaky)
    page = mp.read_page(game.session, fg.HEAP)
    assert page.data[0] is None and game.session.connected


def test_inspector_shows_every_type(game):
    game.fake.poke(fg.PID, fg.HEAP + 0x20, struct.pack("<f", 87.5) + b"\xff" * 4)
    page = mp.read_page(game.session, fg.HEAP + 0x20)
    values = dict(mp.inspect(page, fg.HEAP + 0x20))
    assert values[ValueType.FLOAT] == "87.5"
    assert set(values) == set(mp.INSPECT_TYPES) and len(values) == 10
    values = dict(mp.inspect(page, fg.HEAP + 0x24))
    assert values[ValueType.INT8] == "-1" and values[ValueType.UINT8] == "255"
    assert values[ValueType.UINT32] == str(0xFFFFFFFF)


def test_inspector_off_the_page_or_unknown():
    page = mp.MemPage(0x1000, (1, 2, None, 4))
    values = dict(mp.inspect(page, 0x1000))
    assert values[ValueType.INT16] == str(0x0201)
    assert values[ValueType.INT32] == mp.UNKNOWN
    assert dict(mp.inspect(page, 0x1003))[ValueType.INT16] == mp.UNKNOWN


def test_text_cell():
    assert mp.text_cell((0x41, 0x00, None, 0x7A)) == "A.?z"

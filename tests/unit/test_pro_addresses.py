"""core/pro/addresses: places typed in the Pro window."""

from __future__ import annotations

import pytest

from cheatmaker.core.pro.addresses import BAD_PLACE, parse_place, place_text
from cheatmaker.core.session import GameInfo

GAME = GameInfo(7, "PPSA01285", "Game", "01.000", "eboot.bin", 0x400000, 0x500000)


@pytest.mark.parametrize(
    ("text", "addr"),
    [
        ("eboot.bin+0x1234", 0x401234),
        ("EBOOT.BIN + 1234", 0x401234),
        ("+0x10", 0x400010),
        ("0x1234", 0x401234),  # below the module start: an offset
        ("0x401234", 0x401234),  # an address
        ("20000000", 0x20000000),
        ("eboot.bin+5e1536ch", 0x400000 + 0x5E1536C),
    ],
)
def test_parse_place(text, addr):
    assert parse_place(text, GAME) == addr


@pytest.mark.parametrize("text", ["", "here", "eboot.bin+", "0xzz"])
def test_bad_place(text):
    with pytest.raises(ValueError, match="eboot.bin"):
        parse_place(text, GAME)
    assert "eboot.bin+0x1234" in BAD_PLACE


def test_other_module_is_refused():
    with pytest.raises(ValueError, match="own module"):
        parse_place("libc.prx+0x10", GAME)


def test_place_text():
    assert place_text(0x401234, GAME) == "eboot.bin+0x1234"
    assert place_text(0x20000000, GAME) == "0x20000000"
    assert place_text(0x401234, None) == "0x401234"

from cheatmaker.core import vtable_find as vf
from tests import fakegame as fg


def test_finds_every_object_of_the_class_aligned_only():
    game = fg.build(2)
    vt = fg.PLAYER_VT - fg.BASE
    assert vf.find_by_vtable(game.session, vt) == [game.player]
    import struct

    game.fake.poke(fg.PID, fg.HEAP + 0x9004, struct.pack("<Q", fg.PLAYER_VT))  # misaligned
    game.fake.poke(fg.PID, fg.HEAP + 0x9800, struct.pack("<Q", fg.PLAYER_VT))
    assert vf.find_by_vtable(game.session, vt) == [game.player, fg.HEAP + 0x9800]
    assert vf.find_by_vtable(game.session, fg.ENEMY_VT - fg.BASE) == sorted(game.enemies)


def test_ends_the_console_search_even_on_cancel():
    import threading

    import pytest

    from cheatmaker.errors import ScanCancelled

    game = fg.build(2)
    stop = threading.Event()
    stop.set()
    with pytest.raises(ScanCancelled):
        vf.find_by_vtable(game.session, fg.PLAYER_VT - fg.BASE, cancel=stop)
    assert game.fake._scan is None

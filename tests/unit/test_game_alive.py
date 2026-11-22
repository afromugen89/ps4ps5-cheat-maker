"""Session.game_alive (Pro window liveness poll, AC-1.2)."""

from __future__ import annotations

import pytest

from cheatmaker.errors import ConnectionLost, ConsoleBusy
from tests.conftest import PID


def test_foreground_game_is_alive(session):
    assert session.game_alive()


def test_background_game_with_a_memory_map_is_alive(session, fake):
    fake.foreground_pid = None
    assert session.game_alive()


def test_closed_game_is_not_alive(session, fake):
    fake.remove_process(PID)
    assert not session.game_alive()


def test_busy_connection_counts_as_alive(session, fake, monkeypatch):
    fake.foreground_pid = None

    def busy(pid):
        raise ConsoleBusy("busy")

    monkeypatch.setattr(fake, "maps", busy)
    assert session.game_alive()


def test_lost_connection_is_raised(session, fake):
    fake.disconnect()
    with pytest.raises(ConnectionLost):
        session.game_alive()

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


def test_busy_foreground_counts_as_alive(session, fake, monkeypatch):
    def busy():
        raise ConsoleBusy("busy")

    monkeypatch.setattr(fake, "foreground_app", busy)
    assert session.game_alive()


def test_the_poll_waits_only_briefly(session, fake, monkeypatch):
    from cheatmaker import busy
    from cheatmaker.core.session import ALIVE_WAIT

    seen = []
    real = fake.foreground_app

    def spy():
        seen.append(busy.thread_wait())
        return real()

    monkeypatch.setattr(fake, "foreground_app", spy)
    session.game_alive()
    assert seen == [ALIVE_WAIT] and busy.thread_wait() is None
    assert ALIVE_WAIT <= 1.0


def test_short_wait_nests_and_restores():
    from cheatmaker import busy

    with busy.short_wait(1.0):
        with busy.short_wait(0.2):
            assert busy.thread_wait() == 0.2
        assert busy.thread_wait() == 1.0
    assert busy.thread_wait() is None


def test_ng_socket_lock_honours_a_short_wait():
    """Off the UI thread, a short wait gives up with ConsoleBusy instead of queueing."""
    import threading

    from cheatmaker import busy
    from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient

    client = Ps5DebugNgClient.__new__(Ps5DebugNgClient)
    client._lock = threading.Lock()
    client._end_pending = False
    client._lock.acquire()  # a long search holds the socket
    seen = []

    def poll():
        with busy.short_wait(0.05):
            try:
                with client._locked():
                    seen.append("got it")
            except ConsoleBusy:
                seen.append("busy")

    t = threading.Thread(target=poll)
    t.start()
    t.join(2)
    client._lock.release()
    assert seen == ["busy"]

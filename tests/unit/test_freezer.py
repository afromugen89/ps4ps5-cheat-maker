import struct
import threading
import time

import pytest

from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.templates import BY_KEY, make_cheat
from cheatmaker.core.values import ValueType
from cheatmaker.errors import WriteFailed
from tests.conftest import GOLD_ADDR, HEALTH_ADDR, MODULE_BASE, PID


def _health_cheat():
    return make_cheat(
        BY_KEY["infinite_health"],
        LocationKind.SESSION_ADDRESS,
        HEALTH_ADDR,
        ValueType.INT32,
        struct.pack("<i", 100),
    )


def _i32(fake, addr):
    return struct.unpack("<i", fake.peek(PID, addr, 4))[0]


def test_set_once(session, fake):  # AC-4.1
    c = make_cheat(
        BY_KEY["set_number"],
        LocationKind.SESSION_ADDRESS,
        GOLD_ADDR,
        ValueType.INT32,
        struct.pack("<i", 2450),
        value=99999,
    )
    LiveCheats(session, Freezer(session)).set_once(c)
    assert _i32(fake, GOLD_ADDR) == 99999


def test_freeze_tick_restores_value(session, fake):  # AC-4.2
    fz = Freezer(session)
    live = LiveCheats(session, fz)
    c = _health_cheat()
    live.turn_on(c)
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 40))  # took damage
    fz.tick()
    assert _i32(fake, HEALTH_ADDR) == 100 and c.enabled
    live.turn_off(c)
    fz.stop()
    assert not c.enabled and str(id(c)) not in fz.keys


def test_freeze_thread_runs_at_least_10hz(session, fake):
    fz = Freezer(session)
    fz.freeze("k", [(PID, HEALTH_ADDR, struct.pack("<i", 100))])
    time.sleep(0.35)
    fz.stop()
    assert len(fake.writes) >= 3


def test_toggle_restores_off_bytes_for_code_patch(session, fake):  # AC-5.3
    loc = Location(LocationKind.CODE_PATCH, 0x100, b"\x90\x90", b"\x89\x08")
    c = Cheat("Lock", [loc])
    live = LiveCheats(session, Freezer(session))
    live.turn_on(c)
    assert fake.peek(PID, MODULE_BASE + 0x100, 2) == b"\x90\x90"
    live.turn_off(c)
    assert fake.peek(PID, MODULE_BASE + 0x100, 2) == b"\x89\x08"


def test_refresh_after_value_edit(session, fake):
    live = LiveCheats(session, Freezer(session))
    c = _health_cheat()
    live.turn_on(c)
    c.set_value(500)
    live.refresh(c)
    assert _i32(fake, HEALTH_ADDR) == 500
    live.freezer.stop()


def test_write_failure_raises(session, fake):  # AC-4.3
    fake.fail_writes = True
    with pytest.raises(WriteFailed):
        LiveCheats(session, Freezer(session)).turn_on(_health_cheat())


def test_write_failure_during_freeze_reports_and_unfreezes(session, fake):
    errors = []
    fz = Freezer(session, on_error=lambda k, e: errors.append(k))
    fz.freeze("k", [(PID, HEALTH_ADDR, b"\0\0\0\0")])
    fake.fail_writes = True
    fz.tick()
    fz.stop()
    assert errors == ["k"] and "k" not in fz.keys


def test_connection_drop_stops_and_marks_off(session, fake):  # AC-1.4
    lost = threading.Event()
    fz = Freezer(session, on_lost=lost.set, hz=50)
    live = LiveCheats(session, fz)
    c = _health_cheat()
    live.turn_on(c)
    fake.disconnect()
    assert lost.wait(5), "on_lost must fire within 5 s"
    off = live.mark_all_off()
    assert off == [c] and not c.enabled and not fz.keys
    fz.stop()

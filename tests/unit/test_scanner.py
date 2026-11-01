import struct
import threading

import pytest

from cheatmaker.console.fake import PROT_R, PROT_X
from cheatmaker.core.scanner import Scanner
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import ScanCancelled
from tests.conftest import BAR_ADDR, GOLD_ADDR, HEALTH_ADDR, PID


def test_exact_then_narrow(session, fake):  # AC-3.1, AC-3.2
    sc = Scanner(session)
    assert sc.first_scan(100) == 2 and sc.vtype is ValueType.INT32
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 90))
    assert sc.next_scan(Compare.EXACT, 90) == 1 and sc.few_left
    [m] = sc.matches()
    assert (m.address, m.value) == (HEALTH_ADDR, 90)


def test_live_values_update(session, fake):  # AC-3.4
    sc = Scanner(session)
    sc.first_scan(2450)
    fake.poke(PID, GOLD_ADDR, struct.pack("<i", 2000))
    assert sc.matches()[0].value == 2000


def test_auto_type_falls_back_to_float(session):  # AC-3.6
    sc = Scanner(session)
    assert sc.first_scan(87) >= 1 and sc.vtype is ValueType.FLOAT
    assert BAR_ADDR in [m.address for m in sc.matches()]


def test_explicit_type(session):
    sc = Scanner(session)
    assert sc.first_scan(100, ValueType.INT16) >= 2 and sc.vtype is ValueType.INT16


def test_unknown_value_then_decreased(session, fake):  # AC-3.3
    sc = Scanner(session)
    assert sc.first_scan(None) > 10 and sc.vtype is ValueType.FLOAT
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 50.0))
    assert sc.next_scan(Compare.DECREASED) == 1
    assert sc.next_scan(Compare.UNCHANGED) == 1
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 60.0))
    assert sc.next_scan(Compare.INCREASED) == 1
    assert sc.next_scan(Compare.CHANGED) == 0


def test_next_scan_errors(session):
    sc = Scanner(session)
    with pytest.raises(ValueError):
        sc.next_scan(Compare.DECREASED)
    sc.first_scan(100)
    with pytest.raises(ValueError):
        sc.next_scan(Compare.EXACT)
    with pytest.raises(ValueError):
        sc.next_scan(Compare.UNKNOWN)


def test_progress_and_cancel(session):  # AC-3.5 progress + cancel
    sc = Scanner(session)
    seen = []
    sc.first_scan(100, progress=seen.append)
    assert seen[-1] == 1.0 and seen == sorted(seen)
    ev = threading.Event()
    ev.set()
    with pytest.raises(ScanCancelled):
        sc.first_scan(100, cancel=ev)


def test_new_game_clears_results(session, fake):  # AC-2.3
    sc = Scanner(session)
    sc.first_scan(100)
    fake.remove_process(PID)
    fake.add_process(88, "eboot.bin", "PPSA09999", "Other")
    fake.add_region(88, "eboot.bin", 0x400000, b"\0" * 16, PROT_R | PROT_X)
    session.refresh_game()
    assert not sc.active and sc.matches() == []


def test_zero_matches(session):
    sc = Scanner(session)
    assert sc.first_scan(123456789) == 0 and not sc.few_left

"""core/pro/scan and the address-list rows (spec PR-2, AC-2.2, AC-2.3)."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.core.pro import addresses as ad
from cheatmaker.core.pro import scan as sc
from cheatmaker.core.pro.badge import CHEATRUNNER, HEAP_ADDRESS, PC_ONLY, WRITTEN_ONCE
from cheatmaker.core.scanner import Scanner
from cheatmaker.core.values import Compare, ValueType
from tests.conftest import (
    DATA_BASE,
    HEALTH_ADDR,
    HEAP_BASE,
    MODULE_BASE,
    PID,
    STATIC_ADDR,
)


def test_regions(session):
    names = lambda which: sorted(m.start for m in sc.regions(session, which))  # noqa: E731
    assert names(sc.GAME_DATA) == [DATA_BASE, HEAP_BASE]
    assert names(sc.ALL_WRITABLE) == [DATA_BASE, HEAP_BASE]
    assert names(sc.ALL_READABLE) == [MODULE_BASE, DATA_BASE, HEAP_BASE]
    assert names(sc.GAME_MODULE) == [MODULE_BASE, DATA_BASE]
    with pytest.raises(ValueError):
        sc.regions(session, "elsewhere")


def test_greater_than_keeps_only_bigger_values(session, fake):
    """AC-2.2."""
    scan = sc.ProScan(session)
    scan.claim()
    assert scan.first(sc.GAME_DATA, ValueType.INT32, Compare.EXACT, 100) == 2
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 150))
    assert scan.next(Compare.GREATER, 100) == 1
    (hit,) = scan.page(0)
    assert hit.address == HEALTH_ADDR and struct.unpack("<i", hit.value)[0] == 150


def test_unsigned_and_between(session, fake):
    fake.poke(PID, HEAP_BASE + 0x300, struct.pack("<I", 0xFFFFFFF0))
    scan = sc.ProScan(session)
    scan.claim()
    value = sc.scan_value(Compare.BETWEEN, ValueType.UINT32, 0xFFFFFF00, 0xFFFFFFFF)
    assert scan.first(sc.GAME_DATA, ValueType.UINT32, Compare.BETWEEN, value) == 1


def test_pages_of_a_thousand(session):
    scan = sc.ProScan(session)
    scan.claim()
    n = scan.first(sc.ALL_READABLE, ValueType.INT8, Compare.UNKNOWN)
    assert n > sc.PAGE_SIZE and scan.pages() == (n + 999) // 1000
    first, second = scan.page(0), scan.page(1)
    assert len(first) == sc.PAGE_SIZE and first[-1].address < second[0].address


def test_another_search_ends_the_pro_one(session):
    scan = sc.ProScan(session)
    scan.claim()
    scan.first(sc.GAME_DATA, ValueType.INT32, Compare.EXACT, 100)
    guided = Scanner(session)
    guided.first_scan(2450)
    assert not scan.active
    with pytest.raises(sc.SearchReplaced):
        scan.next(Compare.CHANGED)


def test_pro_scan_takes_the_guided_search(session):
    guided = Scanner(session)
    guided.first_scan(2450)
    lost = []
    guided.on_lost.append(lambda: lost.append(True))
    pro = sc.ProScan(session)
    pro.claim()
    pro.first(sc.GAME_DATA, ValueType.INT32, Compare.EXACT, 100)
    assert lost == [True] and not guided.active


def test_first_scan_refuses_a_change_compare(session):
    with pytest.raises(ValueError):
        sc.ProScan(session).first(sc.GAME_DATA, ValueType.INT32, Compare.CHANGED)


@pytest.mark.parametrize(
    ("cmp", "vt", "a", "b", "want"),
    [
        (Compare.EXACT, ValueType.INT32, 5, None, 5),
        (Compare.UNKNOWN, ValueType.INT32, None, None, None),
        (Compare.INCREASED_BY, ValueType.INT32, 5, None, (5, 1)),
        (Compare.DECREASED_BY, ValueType.FLOAT, 0.5, None, (0.5, 0.1)),
        (Compare.BETWEEN, ValueType.INT32, 1, 9, (1, 9)),
    ],
)
def test_scan_value(cmp, vt, a, b, want):
    assert sc.scan_value(cmp, vt, a, b) == want


@pytest.mark.parametrize(
    ("cmp", "a", "b"),
    [
        (Compare.BETWEEN, 9, 1),
        (Compare.BETWEEN, 1, None),
        (Compare.INCREASED_BY, 0, None),
        (Compare.EXACT, None, None),
    ],
)
def test_scan_value_refused(cmp, a, b):
    with pytest.raises(ValueError):
        sc.scan_value(cmp, ValueType.INT32, a, b)


def test_time_left():
    assert sc.time_left(10, 0.0) is None
    assert sc.time_left(10, 0.5) == pytest.approx(10)
    assert sc.time_left_text(None).startswith("working out")
    assert sc.time_left_text(42) == "about 42 s left"
    assert sc.time_left_text(95) == "about 1 min 35 s left"


def test_find_aob(session):
    assert sc.find_aob(session, "89 08") == [MODULE_BASE + 0x100]
    assert MODULE_BASE + 0x100 in sc.find_aob(session, "?? 08")
    with pytest.raises(ValueError, match="hex pairs"):
        sc.find_aob(session, "zz")


# -- address list rows ---------------------------------------------------------------------
def test_read_values_and_unreadable(session):
    rows = [ad.AddressRow(HEALTH_ADDR, ValueType.INT32), ad.AddressRow(0x10, ValueType.INT32)]
    assert ad.read_values(session, rows) == [100, None]


def test_set_and_freeze(session, fake):
    from cheatmaker.core.freezer import FREEZE_HZ, Freezer

    row = ad.AddressRow(HEALTH_ADDR, ValueType.INT32)
    writes = []
    ad.set_value(row, 7, lambda a, d, s: writes.append((a, d, s)))
    assert writes == [(HEALTH_ADDR, struct.pack("<i", 7), "address_list")] and row.value == 7
    freezer = Freezer(session)
    ad.freeze(session, freezer, row, 999)
    assert FREEZE_HZ >= 10 and freezer.keys == {f"pro:{row.row_id}:{HEALTH_ADDR:x}"}
    ad.unfreeze(freezer, row)
    assert freezer.keys == set() and row.frozen is None


def test_badges(session):
    """AC-2.3: heap is PC only; a frozen static is CheatRunner, written once."""
    heap = ad.AddressRow(HEALTH_ADDR, ValueType.INT32, value=100)
    assert ad.row_badge(session, heap).target == PC_ONLY
    assert HEAP_ADDRESS in ad.row_badge(session, heap).reasons
    static = ad.AddressRow(STATIC_ADDR, ValueType.INT32, value=5, frozen=5)
    b = ad.row_badge(session, static)
    assert b.target == CHEATRUNNER and b.notes == (WRITTEN_ONCE,)
    assert ad.as_cheat(session, static).name == f"eboot.bin+{STATIC_ADDR - MODULE_BASE:#x}"

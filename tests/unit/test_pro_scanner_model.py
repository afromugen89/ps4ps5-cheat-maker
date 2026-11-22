"""Pro step 2: unsigned types, greater/less than, previous/first values and paging.

The PC-side rules (values.py, LocalScanSession) and the PS5Debug-NG wire use. The
FakeConsole behaviour (AC-2.2) is in tests/contract/test_console_contract.py."""

from __future__ import annotations

import math
import struct
import sys

import pytest

from cheatmaker.console.local_scan import LocalScanSession, MultiScan
from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient
from cheatmaker.core.values import (
    F32_MAX,
    GUIDED_TYPES,
    Compare,
    ValueType,
    as_between,
    f32,
    matches,
    value_for,
)
from tests.conftest import GOLD_ADDR, HEALTH_ADDR, HEAP_BASE, PID
from tests.unit.test_ps5debug_ng import FakeNgServer


# -- value types ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("vt", "size", "hi"),
    [
        (ValueType.UINT8, 1, 0xFF),
        (ValueType.UINT16, 2, 0xFFFF),
        (ValueType.UINT32, 4, 0xFFFF_FFFF),
        (ValueType.UINT64, 8, 0xFFFF_FFFF_FFFF_FFFF),
    ],
)
def test_unsigned_types_hold_zero_to_max(vt, size, hi):
    assert vt.size == size and not vt.signed and not vt.is_float
    assert vt.int_range() == (0, hi)
    assert vt.decode(vt.encode(hi)) == hi and vt.encode(hi) == b"\xff" * size
    with pytest.raises(ValueError):
        vt.encode(-1)
    with pytest.raises(ValueError):
        vt.encode(hi + 1)
    assert vt.clamp(-5) == 0 and vt.clamp(hi + 9) == hi


def test_signed_types_unchanged():
    assert ValueType.INT32.signed and ValueType.INT32.int_range() == (-(2**31), 2**31 - 1)
    assert not ValueType.FLOAT.signed
    assert ValueType.INT8.decode(b"\xff") == -1 and ValueType.UINT8.decode(b"\xff") == 255


def test_guided_types_are_the_original_six_in_order():
    assert GUIDED_TYPES == (
        ValueType.INT8,
        ValueType.INT16,
        ValueType.INT32,
        ValueType.INT64,
        ValueType.FLOAT,
        ValueType.DOUBLE,
    )
    # New members come last, so indices into the old order still hold.
    assert list(ValueType)[: len(GUIDED_TYPES)] == list(GUIDED_TYPES)


def test_unsigned_type_names_round_trip_by_name():
    """Project files and cheat files store a type by its name."""
    assert ValueType["UINT16"] is ValueType.UINT16


# -- greater / less than ----------------------------------------------------------------
def test_greater_and_less_need_a_value():
    assert Compare.GREATER.needs_value and Compare.LESS.needs_value
    assert not Compare.GREATER.needs_previous
    with pytest.raises(ValueError, match="needs a number"):
        matches(ValueType.INT32, Compare.GREATER, struct.pack("<i", 1))


@pytest.mark.parametrize(
    ("vt", "now", "cmp", "target", "keep"),
    [
        (ValueType.INT32, 101, Compare.GREATER, 100, True),
        (ValueType.INT32, 100, Compare.GREATER, 100, False),
        (ValueType.INT32, 99, Compare.LESS, 100, True),
        (ValueType.INT32, 100, Compare.LESS, 100, False),
        (ValueType.INT32, -5, Compare.LESS, 0, True),
        (ValueType.UINT32, 0xFFFF_FFFB, Compare.GREATER, 100, True),  # -5 if signed
        (ValueType.FLOAT, 100.5, Compare.GREATER, 100, True),
        (ValueType.FLOAT, 99.9, Compare.GREATER, 100, False),
        (ValueType.DOUBLE, -0.25, Compare.LESS, 0, True),
    ],
)
def test_greater_less_compare(vt, now, cmp, target, keep):
    assert matches(vt, cmp, vt.encode(now), None, target) is keep


def test_greater_never_keeps_nan():
    nan = struct.pack("<f", math.nan)
    assert not matches(ValueType.FLOAT, Compare.GREATER, nan, None, -1e30)


def test_value_for_greater_less_past_the_type_ends():
    assert value_for(ValueType.UINT8, Compare.GREATER, 255) is None
    assert value_for(ValueType.UINT8, Compare.LESS, 0) is None
    assert value_for(ValueType.UINT8, Compare.GREATER, 254) == 254
    assert value_for(ValueType.INT8, Compare.LESS, -128) is None
    assert value_for(ValueType.FLOAT, Compare.GREATER, 1e40) is None
    assert value_for(ValueType.FLOAT, Compare.LESS, 1e40) == 1e40


@pytest.mark.parametrize(
    ("vt", "cmp", "value", "rng"),
    [
        (ValueType.INT32, Compare.GREATER, 100, (101, 2**31 - 1)),
        (ValueType.INT32, Compare.LESS, 100, (-(2**31), 99)),
        (ValueType.INT32, Compare.GREATER, 99.5, (100, 2**31 - 1)),
        (ValueType.INT32, Compare.LESS, 99.5, (-(2**31), 99)),
        (ValueType.UINT16, Compare.GREATER, -3, (0, 0xFFFF)),
        (ValueType.UINT16, Compare.LESS, 10, (0, 9)),
    ],
)
def test_as_between_whole_numbers(vt, cmp, value, rng):
    assert as_between(vt, cmp, value) == rng


def test_as_between_decimals_is_open_at_the_value():
    lo, hi = as_between(ValueType.FLOAT, Compare.GREATER, 100.0)
    assert lo > 100.0 and hi == F32_MAX
    lo, hi = as_between(ValueType.DOUBLE, Compare.LESS, 0.0)
    assert lo == -sys.float_info.max and hi < 0.0
    with pytest.raises(ValueError):
        as_between(ValueType.FLOAT, Compare.EXACT, 1.0)


@pytest.mark.parametrize(
    ("vt", "cmp", "value"),
    [
        (ValueType.INT32, Compare.GREATER, 2**31 - 1),
        (ValueType.INT32, Compare.GREATER, 1e20),
        (ValueType.INT32, Compare.LESS, -(2**31)),
        (ValueType.UINT8, Compare.LESS, 0),
        (ValueType.UINT8, Compare.GREATER, 255),
        (ValueType.UINT64, Compare.GREATER, 2**64 - 1),
        (ValueType.FLOAT, Compare.GREATER, 1e39),
        (ValueType.FLOAT, Compare.GREATER, F32_MAX),
        (ValueType.FLOAT, Compare.LESS, -1e39),
        (ValueType.DOUBLE, Compare.GREATER, sys.float_info.max),
    ],
)
def test_as_between_none_when_nothing_can_match(vt, cmp, value):
    assert as_between(vt, cmp, value) is None
    assert value_for(vt, cmp, value) is None


@pytest.mark.parametrize(
    ("vt", "cmp", "value", "rng"),
    [
        (ValueType.INT32, Compare.GREATER, -1e20, (-(2**31), 2**31 - 1)),
        (ValueType.UINT8, Compare.LESS, 1e9, (0, 255)),
        (ValueType.FLOAT, Compare.LESS, 1e39, (-F32_MAX, F32_MAX)),
        (ValueType.FLOAT, Compare.GREATER, -1e39, (-F32_MAX, F32_MAX)),
    ],
)
def test_as_between_clamps_to_the_type(vt, cmp, value, rng):
    assert as_between(vt, cmp, value) == rng
    vt.encode(rng[0]) and vt.encode(rng[1])  # both bounds pack


@pytest.mark.parametrize("value", [0.1, -0.1, 100.0, 1e-30, 0.0, 3.0e38])
def test_float_bounds_are_the_next_float32_past_the_value(value):
    """The console compares stored 4-byte decimals: each bound is one, strictly past
    ``value``, so it keeps exactly what ``matches`` keeps on the PC."""
    lo, _ = as_between(ValueType.FLOAT, Compare.GREATER, value)
    _, hi = as_between(ValueType.FLOAT, Compare.LESS, value)
    assert f32(lo) == lo and f32(hi) == hi and lo > value > hi
    for raw in (lo, hi, f32(value)):
        b = ValueType.FLOAT.encode(raw)
        keep = matches(ValueType.FLOAT, Compare.GREATER, b, None, value)
        assert keep == (lo <= raw <= F32_MAX)


# -- LocalScanSession: previous / first and paging --------------------------------------
def _session(mem: bytearray, base: int = 0x1000) -> LocalScanSession:
    return LocalScanSession(lambda a, n: bytes(mem[a - base : a - base + n]))


def test_hits_keep_previous_and_first_values():
    from cheatmaker.console.client import MemMap

    mem = bytearray(struct.pack("<4i", 5, 7, 5, 9))
    s = _session(mem)
    s.first([MemMap("heap", 0x1000, 0x1010, 3)], ValueType.INT32, Compare.EXACT, 5)
    [a, _b] = s.results(10)
    assert a.first == struct.pack("<i", 5) and a.previous is None
    mem[0:4] = struct.pack("<i", 6)
    mem[8:12] = struct.pack("<i", 4)
    s.next(Compare.INCREASED, None)
    [hit] = s.results(10)
    assert hit.address == 0x1000 and hit.value == struct.pack("<i", 6)
    assert hit.previous == struct.pack("<i", 5) and hit.first == struct.pack("<i", 5)
    mem[0:4] = struct.pack("<i", 8)
    s.next(Compare.CHANGED, None)
    [hit] = s.results(10)
    assert hit.previous == struct.pack("<i", 6) and hit.first == struct.pack("<i", 5)


def test_rebase_moves_value_to_previous():
    from cheatmaker.console.client import MemMap

    mem = bytearray(struct.pack("<i", 5))
    s = _session(mem)
    s.first([MemMap("heap", 0x1000, 0x1004, 3)], ValueType.INT32, Compare.EXACT, 5)
    mem[0:4] = struct.pack("<i", 9)
    [hit] = s.rebased()
    assert hit.value == struct.pack("<i", 9) and hit.previous == struct.pack("<i", 5)


def test_results_page_by_offset():
    from cheatmaker.console.client import MemMap

    mem = bytearray(struct.pack("<10i", *([3] * 10)))
    s = _session(mem)
    s.first([MemMap("heap", 0x1000, 0x1028, 3)], ValueType.INT32, Compare.EXACT, 3)
    assert [h.address for h in s.results(3, 4)] == [0x1010, 0x1014, 0x1018]
    assert [h.address for h in s.results(5, 8)] == [0x1020, 0x1024]
    assert s.results(5, 20) == []


def test_multiscan_pages_across_types_in_address_order():
    a, b = LocalScanSession(lambda *_: b""), LocalScanSession(lambda *_: b"")
    from cheatmaker.console.client import ScanHit

    a.set_results(ValueType.INT32, [ScanHit(x, b"\0" * 4) for x in (0x10, 0x30, 0x50)])
    b.set_results(ValueType.FLOAT, [ScanHit(x, b"\0" * 4) for x in (0x20, 0x40, 0x60)])
    multi = MultiScan([a, b])
    page = multi.results(3, 2)
    assert [h.address for h in page] == [0x30, 0x40, 0x50]
    assert [h.vtype for h in page] == [ValueType.INT32, ValueType.FLOAT, ValueType.INT32]


# -- PS5Debug-NG wire -------------------------------------------------------------------
@pytest.fixture
def server():
    return FakeNgServer()


def _connect(server):
    return Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)


def test_first_greater_than_runs_as_value_between(server):
    cl = _connect(server)
    regions = [m for m in cl.maps(PID) if m.writable]
    n = cl.scan_start(PID, regions, ValueType.INT32, Compare.GREATER, 2000)
    assert set(server.scan_compares) == {4}, "a proven ValueBetween, not BiggerThan"
    hits = cl.scan_results(50)
    assert GOLD_ADDR in [h.address for h in hits] and n == len(hits)
    assert all(struct.unpack("<i", h.value)[0] > 2000 for h in hits)
    cl.close()


def test_next_greater_than_on_the_pc_keeps_values_above(server):
    """AC-2.2 on the real client: candidates few enough to read are compared on the PC."""
    cl = _connect(server)
    regions = [m for m in cl.maps(PID) if m.writable]
    assert cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100) == 2
    server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 150))
    assert cl.scan_next(Compare.GREATER, 100) == 1
    [hit] = cl.scan_results(10)
    assert hit.address == HEALTH_ADDR
    assert hit.previous == struct.pack("<i", 100) and hit.first == struct.pack("<i", 100)
    cl.close()


def test_unsigned_first_scan_sends_the_unsigned_type_id(server):
    cl = _connect(server)
    server.fake.poke(PID, HEAP_BASE + 0x300, struct.pack("<I", 0xFFFF_FFF0))
    regions = [m for m in cl.maps(PID) if m.writable]
    assert cl.scan_start(PID, regions, ValueType.UINT32, Compare.EXACT, 0xFFFF_FFF0) == 1
    [hit] = cl.scan_results(5)
    assert hit.address == HEAP_BASE + 0x300
    cl.close()


def test_snapshot_narrow_greater_runs_as_value_between_and_pages():
    server = FakeNgServer(snapshot=True)
    server.three_value_rows = True
    cl = _connect(server)
    regions = [m for m in cl.maps(PID) if m.writable]
    cl.scan_start(PID, regions, ValueType.INT32, Compare.UNKNOWN, None)
    server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 300))
    n = cl.scan_next(Compare.GREATER, 200)
    assert server.scan_compares[-1] == 4, "ValueBetween, never BiggerThan (2)"
    hits = cl.scan_results(1000)
    assert len(hits) == n and HEALTH_ADDR in [h.address for h in hits]
    health = next(h for h in hits if h.address == HEALTH_ADDR)
    assert health.value == struct.pack("<i", 300)
    assert health.previous == struct.pack("<i", 100) and health.first == struct.pack("<i", 100)
    page = cl.scan_results(1, 1)
    assert server.get_requests[-1] == (1, 1) and page == hits[1:2]
    server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 50))
    cl.scan_next(Compare.LESS, 60)
    assert server.scan_compares[-1] == 4
    assert HEALTH_ADDR in [h.address for h in cl.scan_results(1000)]
    cl.close()


def test_two_value_rows_give_previous_but_no_first():
    server = FakeNgServer(snapshot=True)
    cl = _connect(server)
    regions = [m for m in cl.maps(PID) if m.writable]
    cl.scan_start(PID, regions, ValueType.INT32, Compare.UNKNOWN, None)
    server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 90))
    cl.scan_next(Compare.DECREASED, None)
    [hit] = cl.scan_results(10)
    assert hit.previous == struct.pack("<i", 100) and hit.first is None
    cl.close()


# -- greater / less never truncate, never send codes 2/3 (review) -----------------------
def _snapshot(types=ValueType.INT32):
    server = FakeNgServer(snapshot=True)
    cl = _connect(server)
    regions = [m for m in cl.maps(PID) if m.writable]
    cl.scan_start(PID, regions, types, Compare.UNKNOWN, None)
    return server, cl


def _addrs(cl):
    return [h.address for h in cl.scan_results(10_000)]


def test_snapshot_less_than_a_fraction_keeps_the_whole_number_below():
    server, cl = _snapshot()
    server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 100))
    cl.scan_next(Compare.LESS, 100.5)  # 100 < 100.5: a truncating SmallerThan 100 drops it
    assert HEALTH_ADDR in _addrs(cl)
    assert all(struct.unpack("<i", h.value)[0] <= 100 for h in cl.scan_results(10_000))
    assert 2 not in server.scan_compares and 3 not in server.scan_compares
    cl.close()


def test_snapshot_greater_than_minus_half_keeps_zero_and_up():
    server, cl = _snapshot()
    server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", -1))
    server.fake.poke(PID, GOLD_ADDR, struct.pack("<i", 7))
    cl.scan_next(Compare.GREATER, -0.5)
    got = _addrs(cl)
    assert GOLD_ADDR in got and HEALTH_ADDR not in got
    assert server.scan_compares[-1] == 4
    cl.close()


def test_snapshot_unsigned_greater_than_a_negative_keeps_all():
    server, cl = _snapshot(ValueType.UINT32)
    before = len(_addrs(cl))
    assert cl.scan_next(Compare.GREATER, -5) == before
    assert server.scan_compares[-1] == 4
    cl.close()


def test_snapshot_compare_nothing_can_pass_sends_nothing():
    server, cl = _snapshot()
    sent = len(server.scan_compares)
    assert cl.scan_next(Compare.GREATER, 2**31 - 1) == 0
    assert len(server.scan_compares) == sent, "no compare sent"
    assert not server.residents, "the snapshot was freed"
    assert cl.scan_results(10) == [] and cl.scan_counts() == {ValueType.INT32: 0}
    cl.close()


def test_multi_type_snapshot_greater_uses_a_range_per_type():
    server, cl = _snapshot((ValueType.FLOAT, ValueType.INT32))
    server.fake.poke(PID, GOLD_ADDR, struct.pack("<i", 2**31 - 1))
    n = cl.scan_next(Compare.GREATER, 2**31 - 1.5)  # 2**31-1 as int; floats above too
    assert 2 not in server.scan_compares and server.scan_compares[-1] == 4
    hits = cl.scan_results(10_000)
    assert len(hits) == n and GOLD_ADDR in [h.address for h in hits]
    assert cl.scan_next(Compare.GREATER, 1e39) == 0, "no float or int32 can pass"
    cl.close()


def test_first_scan_nothing_can_pass_asks_the_console_nothing(server):
    cl = _connect(server)
    regions = [m for m in cl.maps(PID) if m.writable]
    for vt, cmp, value in (
        (ValueType.INT32, Compare.GREATER, 1e20),
        (ValueType.UINT8, Compare.LESS, 0),
        (ValueType.FLOAT, Compare.GREATER, 1e39),
    ):
        assert cl.scan_start(PID, regions, vt, cmp, value) == 0
        assert cl.scan_results(10) == []
    assert server.scan_compares == []
    cl.close()


def test_pages_past_the_end_are_empty():
    from cheatmaker.console.client import ScanHit

    a, b = LocalScanSession(lambda *_: b""), LocalScanSession(lambda *_: b"")
    a.set_results(ValueType.INT32, [ScanHit(x, b"\0" * 4) for x in (0x10, 0x30)])
    b.set_results(ValueType.FLOAT, [ScanHit(0x20, b"\0" * 4)])
    assert a.results(5, 2) == [] and a.results(5, 99) == []
    assert [h.address for h in a.results(5, 1)] == [0x30]
    multi = MultiScan([a, b])
    assert multi.results(5, 3) == [] and multi.results(5, 10) == []
    assert [h.address for h in multi.results(5, 2)] == [0x30]

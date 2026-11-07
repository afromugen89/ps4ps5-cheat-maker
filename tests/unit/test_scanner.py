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
    assert sc.first_scan(None, ValueType.FLOAT) > 10 and sc.vtype is ValueType.FLOAT
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


def test_logs_value_and_few_match_addresses(session, fake, caplog):
    caplog.set_level("INFO", logger="cheatmaker.core.scanner")
    sc = Scanner(session)
    sc.first_scan(100)
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 90))
    sc.next_scan(Compare.EXACT, 90)
    text = caplog.text
    assert "value=100" in text and "value=90 count=1" in text
    assert f"action=matches addrs={HEALTH_ADDR:#x}" in text


def test_eight_byte_types_keep_4_byte_alignment():
    # Returnal keeps INT64 counters on 4-byte boundaries (console test C3).
    from cheatmaker.console.local_scan import alignment_for

    assert alignment_for(ValueType.INT64) == 4 and alignment_for(ValueType.DOUBLE) == 4


def test_unknown_with_range_keeps_only_values_in_range(session, fake):
    sc = Scanner(session)
    everything = Scanner(session).first_scan(None)
    sc = Scanner(session)
    count = sc.first_scan(None, value_range=(80, 90))
    assert 0 < count < everything
    assert BAR_ADDR in [m.address for m in sc.matches()]  # 87.4
    assert all(80 <= m.value <= 90 for m in sc.matches())


def test_between_rejects_backwards_range(session):
    sc = Scanner(session)
    sc.first_scan(None)
    with pytest.raises(ValueError, match="smaller"):
        sc.next_scan(Compare.BETWEEN, (10, 1))


def test_show_limit_is_player_set(session, fake):
    sc = Scanner(session)
    assert sc.first_scan(None, ValueType.INT32) > 10 and not sc.few_left
    sc.show_limit = 5000
    assert sc.few_left
    ms = sc.matches()
    assert len(ms) == sc.count and HEALTH_ADDR in [m.address for m in ms]
    reads = len(fake.reads) if hasattr(fake, "reads") else None
    assert reads is None or reads < sc.count, "nearby matches share one read"


# -- AC-H1 to AC-H4: hard values ---------------------------------------------------
def test_went_down_by_amount_whole_numbers(session, fake):  # AC-H1
    sc = Scanner(session)
    assert sc.first_scan(100, ValueType.INT32) == 2  # health and a decoy
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 85))
    assert sc.next_scan(Compare.DECREASED_BY, 15) == 1
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 95))
    assert sc.next_scan(Compare.INCREASED_BY, 5) == 0, "went up by 10, not 5"
    assert sc.history[-2:] == ["decreased by 15", "increased by 5"]


def test_went_down_by_on_a_float_uses_the_tolerance(session, fake):  # AC-H1 + AC-H2
    sc = Scanner(session)
    sc.first_scan(None)
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 87.4 - 14.6))  # shown as "-15"
    assert sc.next_scan(Compare.DECREASED_BY, 15) == 1
    assert sc.matches()[0].address == BAR_ADDR
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 72.8 - 1.5))
    assert sc.next_scan(Compare.DECREASED_BY, 1.0, unit=0.1) == 0, "1.5 is not 1.0 +- 0.1"


@pytest.mark.parametrize("amount", [0, -3, None])
def test_changed_by_needs_a_positive_amount(session, amount):
    sc = Scanner(session)
    sc.first_scan(100)
    with pytest.raises(ValueError, match="changed by"):
        sc.next_scan(Compare.INCREASED_BY, amount)


def test_typed_decimals_narrow_the_window(session, fake):  # AC-H2
    sc = Scanner(session)
    assert sc.first_scan(87.4, unit=0.1) >= 1 and sc.vtype is ValueType.FLOAT
    assert BAR_ADDR in [m.address for m in sc.matches(50)]
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 86.95))
    assert sc.next_scan(Compare.EXACT, 87.0, unit=0.1) == 1
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 86.7))
    assert sc.next_scan(Compare.EXACT, 87.0, unit=0.1) == 0, "86.7 is 0.3 away from 87.0"


def test_whole_number_float_search_keeps_one_unit_rule(session, fake):  # AC-H2
    sc = Scanner(session)
    sc.first_scan(87, ValueType.FLOAT)
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 79.1))
    assert sc.next_scan(Compare.EXACT, 80) == 1, "79.1 shown as 80 (or 79): kept"


def test_fraction_scale_finds_a_zero_to_one_bar(session, fake):  # AC-H3
    from cheatmaker.core.values import make_scale

    fake.poke(PID, BAR_ADDR, struct.pack("<f", 0.874))  # health 87 of 100, stored 0-1
    sc = Scanner(session)
    assert sc.first_scan(87, scale=make_scale("fraction", 100)) == 1
    assert sc.vtype is ValueType.FLOAT and sc.history == ["87 (a fraction of 100)"]
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 0.62))
    assert sc.next_scan(Compare.EXACT, 62) == 1
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 0.5))
    assert sc.next_scan(Compare.DECREASED_BY, 12) == 1
    assert sc.matches()[0].address == BAR_ADDR
    sc.reset()
    assert sc.scale.factor == 1, "Start over forgets the scale"


def test_x10_scale_on_whole_numbers(session, fake):  # AC-H3
    from cheatmaker.core.values import make_scale

    fake.poke(PID, GOLD_ADDR, struct.pack("<i", 24_503))  # shown 2,450, stored x10
    sc = Scanner(session)
    assert sc.first_scan(2450, scale=make_scale("x10")) >= 1 and sc.vtype is ValueType.INT32
    assert GOLD_ADDR in [m.address for m in sc.matches(50)]


def test_make_scale_rejects_bad_input():
    from cheatmaker.core.values import make_scale

    with pytest.raises(ValueError):
        make_scale("fraction", 0)
    with pytest.raises(ValueError):
        make_scale("x7")


def test_stand_still_drops_what_moves_and_stops_early(session, fake):  # AC-H4
    sc = Scanner(session)
    start = sc.first_scan(None)
    ticks = iter(range(1, 100))
    naps = []

    def sleep(s):
        naps.append(s)
        fake.poke(PID, GOLD_ADDR, struct.pack("<f", float(next(ticks))))  # a timer ticking

    seen = []
    left = sc.stand_still(progress=seen.append, sleep=sleep)
    assert left < start
    assert GOLD_ADDR not in [m.address for m in sc.matches(500)]
    assert len(naps) < 5, "stopped once two scans in a row dropped nothing"
    assert seen[-1] == 1.0 and set(naps) == {2.0}


def test_stand_still_honours_cancel(session):
    sc = Scanner(session)
    sc.first_scan(None)
    cancel = threading.Event()
    cancel.set()
    before = sc.count
    assert sc.stand_still(cancel=cancel, sleep=lambda _s: None) == before


def test_stand_still_needs_a_search(session):
    with pytest.raises(ValueError):
        Scanner(session).stand_still(sleep=lambda _s: None)


def test_changed_by_on_whole_numbers_needs_a_whole_amount(session):  # review finding 5
    sc = Scanner(session)
    sc.first_scan(100, ValueType.INT32)
    with pytest.raises(ValueError, match="whole numbers"):
        sc.next_scan(Compare.DECREASED_BY, 0.5, unit=0.1)


def test_scale_is_forgotten_when_the_search_is_lost(session):
    from cheatmaker.core.values import make_scale

    sc = Scanner(session)
    sc.first_scan(2450, scale=make_scale("x10"))
    sc._taken()
    assert sc.scale.factor == 1 and not sc.active


def test_stand_still_cancel_stops_the_wait(session):  # review finding 7
    import time

    sc = Scanner(session)
    sc.first_scan(None)
    cancel = threading.Event()

    def sleep(_s):
        raise AssertionError("must wait on the cancel event, not sleep")

    threading.Timer(0.1, cancel.set).start()
    t0 = time.monotonic()
    sc.stand_still(interval_s=30, cancel=cancel, sleep=sleep)
    assert time.monotonic() - t0 < 5


# -- "I don't know the number" over decimals and whole numbers at once ---------------


def test_unknown_searches_decimals_and_whole_numbers_at_once(session, fake):
    sc = Scanner(session)
    total = sc.first_scan(None)
    assert set(sc.types) == {ValueType.FLOAT, ValueType.INT32}
    assert sc.mixed and sc.vtype is None and total == sum(sc.types.values())
    fake.poke(PID, GOLD_ADDR, struct.pack("<i", 2000))  # whole-number gold spent
    sc.next_scan(Compare.DECREASED)
    assert ValueType.INT32 in sc.types
    assert sc.next_scan(Compare.EXACT, 2000) == 1
    assert sc.vtype is ValueType.INT32 and not sc.mixed, "only whole numbers left"
    [m] = sc.matches()
    assert (m.address, m.value) == (GOLD_ADDR, 2000)


def test_unknown_finds_a_decimal_bar_too(session, fake):
    sc = Scanner(session)
    sc.first_scan(None)
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 50.5))
    sc.next_scan(Compare.DECREASED)
    assert sc.next_scan(Compare.EXACT, 50.5) >= 1
    assert sc.vtype is ValueType.FLOAT
    assert BAR_ADDR in {m.address for m in sc.matches(50)}


def test_typed_decimal_drops_the_whole_number_search(session, fake):
    sc = Scanner(session)
    sc.first_scan(None)
    sc.next_scan(Compare.BETWEEN, (87.3, 87.5))
    assert sc.vtype is ValueType.FLOAT and BAR_ADDR in {m.address for m in sc.matches(50)}


def test_mixed_matches_carry_their_type(session, fake):
    sc = Scanner(session)
    sc.show_limit = 500
    sc.first_scan(None, value_range=(2000, 2500))
    rows = sc.matches(500)
    by_addr = {m.address: m for m in rows}
    assert by_addr[GOLD_ADDR].vtype is ValueType.INT32 and by_addr[GOLD_ADDR].value == 2450


def test_picked_type_still_searches_one_type(session):
    sc = Scanner(session)
    sc.first_scan(None, ValueType.INT32)
    assert set(sc.types) == {ValueType.INT32} and sc.vtype is ValueType.INT32

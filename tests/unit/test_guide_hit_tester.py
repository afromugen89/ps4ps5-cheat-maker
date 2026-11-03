"""AC-H5 Find-it guide and AC-H6 Test the hits."""

import struct

import pytest

from cheatmaker.core.guide import Action, Guide, Scan, Shows
from cheatmaker.core.hit_tester import TEST_LIMIT, HitTester, suggest_value
from cheatmaker.core.values import ValueType
from cheatmaker.errors import WriteFailed
from tests.conftest import GOLD_ADDR, HEALTH_ADDR, HEAP_BASE, PID


# -- guide --------------------------------------------------------------------
def test_guide_first_question():
    assert Guide().start(Shows.NUMBER).action is Action.SEARCH
    assert Guide().start(Shows.BAR).action is Action.UNKNOWN
    assert Guide().start(Shows.NOTHING).action is Action.UNKNOWN


def test_guide_falls_back_in_order_on_nothing_found():
    g = Guide()
    g.start(Shows.NUMBER)
    seen = []
    g.note_tried("shown")
    while (s := g.after_scan(Scan.FIRST_EXACT, 0)).action is Action.TRY_NEXT:
        seen.append(s.fallback)
        assert "Next: try it as" in s.text
        g.note_tried(s.fallback)
    assert seen == ["fraction", "x10", "x100", "unknown"]
    assert s.action is Action.START_OVER and s.fallback is None


def test_guide_zero_after_compare_says_start_over():
    g = Guide()
    g.start(Shows.NUMBER)
    g.note_tried("shown")
    s = g.after_scan(Scan.COMPARE, 0)
    assert s.action is Action.START_OVER and s.fallback == "fraction"
    assert "Start over and try a fraction of 100" in s.text


def test_guide_counts():
    g = Guide(show_limit=10)
    g.start(Shows.NUMBER)
    assert g.after_scan(Scan.NEXT_EXACT, 1).action is Action.MAKE_CHEAT
    assert g.after_scan(Scan.FIRST_EXACT, 400).action is Action.TYPE_NEW, "narrow first"
    s = g.after_scan(Scan.NEXT_EXACT, 400)
    assert s.action is Action.TEST_HITS and "about 9 questions" in s.text, "stuck at 400"
    assert g.after_scan(Scan.NEXT_EXACT, 8).action is Action.TEST_HITS, "short list"
    assert g.after_scan(Scan.FIRST_EXACT, TEST_LIMIT + 1).action is Action.TYPE_NEW


def test_guide_number_never_says_stand_still():
    g = Guide()
    g.start(Shows.NUMBER)
    g.after_scan(Scan.FIRST_EXACT, 10**5)
    g.after_scan(Scan.COMPARE, 10**4)
    assert g.after_scan(Scan.COMPARE, 10**3).action is Action.TYPE_NEW


def test_guide_bar_suggests_moving_then_stand_still():
    g = Guide()
    g.start(Shows.BAR)
    assert g.after_scan(Scan.FIRST_UNKNOWN, 10**6).action is Action.MOVE
    assert g.after_scan(Scan.COMPARE, 10**5).action is Action.MOVE
    assert g.after_scan(Scan.COMPARE, 10**4).action is Action.STAND_STILL
    g.new_value()
    assert g.compares_in_row == 0 and g.tried == []


# -- test the hits -------------------------------------------------------------
def _play(t, fake, real):
    """The player answers truthfully: did the real address get the test value?"""
    while not t.done:
        half = t.write_test(111)
        assert struct.unpack("<i", fake.peek(PID, half[0], 4))[0] == 111
        t.answer(real in half)
        for a in half:
            assert struct.unpack("<i", fake.peek(PID, a, 4))[0] == 100, "put back"


@pytest.mark.parametrize("n", [2, 3, 7, 64, 500])
def test_hit_tester_finds_the_real_one(fake, n):
    addrs = [HEAP_BASE + 0x300 + 4 * i for i in range(n)]
    for a in addrs:
        fake.poke(PID, a, struct.pack("<i", 100))
    real = addrs[n // 3]
    t = HitTester(fake, PID, ValueType.INT32, addrs)
    expected = t.rounds_left
    _play(t, fake, real)
    assert t.result == real and t.rounds == expected


def test_hit_tester_not_sure_stops_and_restores(fake):
    t = HitTester(fake, PID, ValueType.INT32, [HEALTH_ADDR, GOLD_ADDR])
    t.write_test(7)
    t.answer(None)
    assert t.done and t.result is None
    assert struct.unpack("<i", fake.peek(PID, HEALTH_ADDR, 4))[0] == 100


def test_hit_tester_limits_and_order(fake):
    with pytest.raises(ValueError):
        HitTester(fake, PID, ValueType.INT32, [HEALTH_ADDR])
    with pytest.raises(ValueError):
        HitTester(fake, PID, ValueType.INT32, list(range(0, 8 * (TEST_LIMIT + 1), 8)))
    t = HitTester(fake, PID, ValueType.INT32, [HEALTH_ADDR, GOLD_ADDR])
    with pytest.raises(ValueError, match="Write a test value"):
        t.answer(True)
    t.write_test(5)
    with pytest.raises(ValueError, match="Answer"):
        t.write_test(6)
    with pytest.raises(ValueError):
        HitTester(fake, PID, ValueType.INT8, [HEALTH_ADDR, GOLD_ADDR]).write_test(1000)


def test_hit_tester_failed_write_puts_back_what_it_wrote(fake, monkeypatch):
    t = HitTester(fake, PID, ValueType.INT32, [HEALTH_ADDR, HEALTH_ADDR + 8, GOLD_ADDR])
    real_write = fake.write
    calls = []

    def flaky(pid, addr, data):
        calls.append(addr)
        if len(calls) == 2:
            raise WriteFailed("boom")
        real_write(pid, addr, data)

    monkeypatch.setattr(fake, "write", flaky)
    with pytest.raises(WriteFailed):
        t.write_test(42)
    assert struct.unpack("<i", fake.peek(PID, HEALTH_ADDR, 4))[0] == 100
    assert not t.waiting


def test_suggest_value():
    assert suggest_value(ValueType.INT32, 100) == 111
    assert suggest_value(ValueType.INT8, 120) == 109, "no room above: go down"
    assert suggest_value(ValueType.FLOAT, 87.4) == pytest.approx(43.7)
    assert suggest_value(ValueType.FLOAT, 0.0) == 11.0
    assert suggest_value(ValueType.FLOAT, float("nan")) == 50.0


def test_hit_tester_stop_during_write_restores_everything(fake, monkeypatch):
    """Review finding 1: Stop pressed while a round is still writing."""
    addrs = [HEAP_BASE + 0x300 + 4 * i for i in range(8)]
    for a in addrs:
        fake.poke(PID, a, struct.pack("<i", 100))
    t = HitTester(fake, PID, ValueType.INT32, addrs)
    real_write = fake.write
    n = []

    def write(pid, addr, data):
        real_write(pid, addr, data)
        n.append(addr)
        if len(n) == 2:
            t.request_stop()  # as if the UI thread pressed Stop now

    monkeypatch.setattr(fake, "write", write)
    assert t.write_test(111) == []
    assert t.done and t.result is None and not t.waiting
    for a in addrs:
        assert struct.unpack("<i", fake.peek(PID, a, 4))[0] == 100


def test_hit_tester_failed_put_back_can_be_retried(fake, monkeypatch):
    t = HitTester(fake, PID, ValueType.INT32, [HEALTH_ADDR, GOLD_ADDR])
    t.write_test(7)
    real_write = fake.write
    monkeypatch.setattr(fake, "write", lambda *a: (_ for _ in ()).throw(WriteFailed("x")))
    with pytest.raises(WriteFailed):
        t.answer(True)
    assert t.waiting, "still noted for a retry"
    monkeypatch.setattr(fake, "write", real_write)
    t.restore()
    assert struct.unpack("<i", fake.peek(PID, HEALTH_ADDR, 4))[0] == 100

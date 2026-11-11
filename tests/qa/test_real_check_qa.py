"""QA round for Check which is real (docs/spec-real-check.md, AC-R1 to AC-R5).
Report: qa/reports/2026-11-11-real-check.md.

Bugs are strict xfail tests named after their BUG id; remove the marker once fixed.
FakeConsole with ``game_ticks`` plays the game (a tick runs on every read); pytest-qt
(offscreen) for the Find view.
"""

from __future__ import annotations

import math
import struct
import threading

import pytest

from cheatmaker.console.fake import FakeConsole
from cheatmaker.core import real_check
from cheatmaker.core.grouping import Group, Member, promote
from cheatmaker.core.real_check import (
    CHECK_LIMIT,
    READ_SPAN,
    ROUNDS_LIMIT,
    RealChecker,
    Target,
    Verdict,
    probe_value,
    targets_from_groups,
    targets_from_list,
)
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ConnectionLost, ProtocolError, ReadFailed, WriteFailed
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests.conftest import HEAP_BASE, PID, build_fake
from tests.ui.test_health_finding import COPIES, HP, HP_FRAC, full_and_three_hits, search

F, D = ValueType.FLOAT, ValueType.DOUBLE
I8, I16, I32, I64 = ValueType.INT8, ValueType.INT16, ValueType.INT32, ValueType.INT64
REAL = HEAP_BASE + 0x300  # the game's own value (only the test writes it)
FRAC = HEAP_BASE + 0x310  # worked out from REAL every frame
HUD = HEAP_BASE + 0x800  # HUD copy: REAL x 100
DEAD = HEAP_BASE + 0x900  # written once, never again
OTHER = HEAP_BASE + 0xA00  # a second group's value, nothing follows it


def pack(vt: ValueType, v: float | int) -> bytes:
    return vt.encode(v)


def get(fake: FakeConsole, addr: int, vt: ValueType = F) -> float | int:
    return vt.decode(fake.peek(PID, addr, vt.size))


def snapshot(fake: FakeConsole, targets: list[Target]) -> dict[Target, bytes]:
    return {t: fake.peek(PID, t.address, t.vtype.size) for t in targets}


@pytest.fixture
def game() -> FakeConsole:
    """REAL 3.0; FRAC = REAL / 3 and HUD = REAL x 100 every frame; DEAD 300 never
    refreshed; OTHER an int32 50 on its own."""
    fake = build_fake()
    fake.poke(PID, REAL, pack(F, 3.0))
    fake.poke(PID, DEAD, pack(F, 300.0))
    fake.poke(PID, OTHER, pack(I32, 50))

    def frame() -> None:
        r = get(fake, REAL)
        fake.poke(PID, FRAC, pack(F, r / 3.0))
        fake.poke(PID, HUD, pack(F, r * 100))

    frame()
    fake.game_ticks.append(frame)
    return fake


def group() -> list[Target]:
    return [Target(HUD, F), Target(FRAC, F), Target(REAL, F), Target(DEAD, F)]


def ticking_wait(fake: FakeConsole):
    return lambda _s: fake.read(PID, HEAP_BASE, 4)  # the game runs while we wait


def run(fake: FakeConsole, groups, **kw) -> RealChecker:
    kw.setdefault("wait", ticking_wait(fake))
    chk = RealChecker(fake, PID, groups, **kw)
    chk.run()
    return chk


def _member(addr: int, vals, vt: ValueType = F) -> Member:
    return Member(addr, vt, None, tuple(vals))


MOVING = (1.0, 0.5, 0.25)


# == AC-R2 test value ===============================================================
@pytest.mark.parametrize("vt", [I8, I16, I32, I64])
def test_tc01_probe_fits_and_differs_at_every_int_edge(vt):
    lo, hi = vt.int_range()
    for cur in (lo, lo + 1, -2, -1, 0, 1, 2, 3, 4, hi - 1, hi):
        p = probe_value(vt, cur)
        vt.encode(p)  # fits the type
        assert p != cur, (vt, cur)
        if cur >= 2:
            assert p < cur, "lower, so a capped value isn't put back by the game"
        if cur in (0, 1):
            assert p == cur + 7


@pytest.mark.parametrize("cur", [0.0, -0.0, 1.0, 3.0, -8.0, 3.4028234663852886e38, 1e-30])
def test_tc01b_float_probe_bytes_differ(cur):
    orig = F.encode(cur)
    test = F.encode(probe_value(F, F.decode(orig)))
    assert test != orig


def test_bug05_subnormal_float_is_not_called_kept(game):
    """The smallest decimal x 0.75 rounds back to itself (1.4e-45), so the 'test value'
    is the original bytes: nothing changes, yet it is judged 'kept the test value'."""
    tiny = HEAP_BASE + 0xD00
    game.poke(PID, tiny, struct.pack("<I", 1))
    chk = run(game, [[Target(tiny, F)]])
    assert chk.results[(tiny, F)].verdict is not Verdict.KEPT


# == AC-R1 what is checked ==========================================================
def test_tc02_check_limit_with_many_full_groups():
    """10 groups of 8: the first 8 groups are taken whole (64), the rest dropped."""
    groups = [
        Group([_member(HEAP_BASE + g * 0x100 + i * 8, MOVING) for i in range(ROUNDS_LIMIT)])
        for g in range(10)
    ]
    out = targets_from_groups(groups)
    assert sum(len(g) for g in out) == CHECK_LIMIT
    assert [len(g) for g in out] == [ROUNDS_LIMIT] * 8
    assert out[0][0].address == HEAP_BASE and out[7][0].address == HEAP_BASE + 0x700


def test_tc03_check_limit_cuts_the_last_group_short():
    singles = [Group([_member(HEAP_BASE + i * 8, MOVING)]) for i in range(CHECK_LIMIT - 1)]
    big = Group([_member(0x3000_0000 + i * 8, MOVING) for i in range(ROUNDS_LIMIT)])
    out = targets_from_groups([*singles, big, Group([_member(0x4000_0000, MOVING)])])
    assert sum(len(g) for g in out) == CHECK_LIMIT
    assert out[-1] == [Target(0x3000_0000, F)]


def test_tc04_still_groups_and_frozen_members_are_left_out():
    g = Group([_member(HEAP_BASE, MOVING), _member(HEAP_BASE + 8, MOVING)])
    still = Group([_member(HEAP_BASE + 0x40, (5.0, 5.0))])
    all_frozen = Group([_member(HEAP_BASE + 0x80, MOVING)])
    out = targets_from_groups([g, still, all_frozen], skip={HEAP_BASE, HEAP_BASE + 0x80})
    assert out == [[Target(HEAP_BASE + 8, F)]]


def test_tc05_flat_list_keeps_mixed_types_and_order():
    rows = [(HEAP_BASE, F), (HEAP_BASE + 8, I16), (HEAP_BASE, F), (HEAP_BASE + 16, D)]
    assert targets_from_list(rows) == [
        [Target(HEAP_BASE, F), Target(HEAP_BASE + 8, I16), Target(HEAP_BASE + 16, D)]
    ]


# == AC-R3 rounds and verdicts ========================================================
def test_tc06_mixed_types_follow_the_real_one():
    """A decimal real value with a 2-byte whole-number HUD (x10, rounded) and an 8-byte
    decimal copy (/ 100): both follow, both are copies."""
    fake = build_fake()
    real, hud16, dbl = HEAP_BASE + 0x300, HEAP_BASE + 0x320, HEAP_BASE + 0x340
    fake.poke(PID, real, pack(F, 25.0))

    def frame() -> None:
        r = get(fake, real)
        fake.poke(PID, hud16, pack(I16, round(r * 10)))
        fake.poke(PID, dbl, pack(D, r / 100))

    frame()
    fake.game_ticks.append(frame)
    g = [Target(hud16, I16), Target(dbl, D), Target(real, F)]
    chk = run(fake, [g])
    assert chk.results[(real, F)] == real_check.Result(Verdict.REAL, 2)
    assert chk.results[(hud16, I16)].verdict is Verdict.COPY
    assert chk.results[(dbl, D)].verdict is Verdict.COPY
    assert get(fake, real) == 25.0 and get(fake, hud16, I16) == 250


@pytest.mark.parametrize(("vt", "start"), [(I8, 127), (I8, -128), (I8, 1), (I16, 32767), (I8, 0)])
def test_tc07_small_int_edges_are_found_and_put_back(vt, start):
    fake = build_fake()
    real, copy = HEAP_BASE + 0x300, HEAP_BASE + 0x320
    fake.poke(PID, real, pack(vt, start))
    fake.game_ticks.append(lambda: fake.poke(PID, copy, pack(I32, get(fake, real, vt) * 10)))
    fake.read(PID, HEAP_BASE, 4)
    chk = run(fake, [[Target(copy, I32), Target(real, vt)]])
    assert chk.results[(real, vt)].verdict is Verdict.REAL
    assert chk.results[(copy, I32)].verdict is Verdict.COPY
    assert get(fake, real, vt) == start


@pytest.mark.parametrize(
    ("vt", "follower", "follows"),
    [(F, 76.4, True), (F, 76.6, False), (I32, 766, True), (I32, 767, False)],
)
def test_tc08_follow_tolerance_boundary(vt, follower, follows):
    """Real 100 -> 75; a copy at 100 (or 1000) should go to 75 (750): 2% (+1 for whole
    numbers) of slack, not more."""
    fake = build_fake()
    real, copy = HEAP_BASE + 0x300, HEAP_BASE + 0x320
    start = 100.0 if vt is F else 1000
    fake.poke(PID, real, pack(F, 100.0))
    fake.poke(PID, copy, pack(vt, start))

    def frame() -> None:
        fake.poke(PID, copy, pack(vt, follower if get(fake, real) == 75.0 else start))

    fake.game_ticks.append(frame)
    chk = run(fake, [[Target(real, F), Target(copy, vt)]])
    want = Verdict.REAL if follows else Verdict.KEPT
    assert chk.results[(real, F)].verdict is want


def test_tc09_tie_takes_the_first(game):
    """Two kept values with one follower each: best() picks the first listed."""
    real2 = HEAP_BASE + 0x500
    copy2 = HEAP_BASE + 0x520
    game.poke(PID, real2, pack(F, 3.0))
    game.game_ticks.append(lambda: game.poke(PID, copy2, pack(F, get(game, real2) * 2)))
    g = [Target(HUD, F), Target(REAL, F), Target(real2, F), Target(copy2, F)]
    chk = run(game, [g])
    assert chk.results[(REAL, F)].followers == chk.results[(real2, F)].followers == 1
    assert chk.best(g) == Target(REAL, F)


# == Overlapping addresses ============================================================


def test_bug01_same_address_two_sizes_in_a_flat_list(game):
    """A whole-number search lists 100 at A as 4 bytes and as 1 byte. Writing 75 to
    either makes the other read 75: each 'follows' the other and both come out real,
    though nothing in the game reacted."""
    a = HEAP_BASE + 0x600
    game.poke(PID, a, pack(I32, 100))
    rows = targets_from_list([(a, I32), (a, I8)])
    chk = run(game, rows)
    assert chk.results[(a, I32)].verdict is not Verdict.REAL
    assert chk.results[(a, I8)].verdict is not Verdict.REAL


def test_bug01_overlap_across_groups_makes_a_false_real(game):
    """Group 1 writes A (4 bytes). Group 2 lists A's low byte as a member; it moves
    100 -> 75, the same 3/4 as group 2's own test value, so group 2's head is called
    real though nothing follows it."""
    a, b = HEAP_BASE + 0x600, HEAP_BASE + 0x700
    game.poke(PID, a, pack(I32, 100))
    game.poke(PID, b, pack(F, 8.0))
    chk = run(game, [[Target(a, I32)], [Target(b, F), Target(a, I8)]])
    assert chk.results[(b, F)].verdict is Verdict.KEPT


def test_tc10_same_bytes_in_two_groups_same_round_is_not_checked(game):
    chk = run(game, [[Target(REAL, F)], [Target(REAL, I32)], [Target(REAL + 2, I16)]])
    assert chk.results[(REAL, I32)].verdict is Verdict.FAILED
    assert chk.results[(REAL + 2, I16)].verdict is Verdict.FAILED
    assert get(game, REAL) == 3.0


# == AC-R4 putting things back ========================================================
@pytest.mark.parametrize("stop_after", [1, 2, 3, 4])
def test_tc11_cancel_after_any_round_leaves_nothing_behind(game, stop_after):
    targets = [*group(), Target(OTHER, I32)]
    start = snapshot(game, targets)
    stop = threading.Event()
    rounds = []

    def progress(f):
        rounds.append(f)
        if len(rounds) == stop_after:
            stop.set()

    chk = RealChecker(game, PID, [group(), [Target(OTHER, I32)]], wait=ticking_wait(game))
    chk.run(progress, stop)
    assert len(rounds) == stop_after
    game.read(PID, HEAP_BASE, 4)  # one more frame: copies follow the put-back value
    assert snapshot(game, targets) == start


def test_tc12_cancel_set_before_start_writes_nothing(game):
    stop = threading.Event()
    stop.set()
    chk = RealChecker(game, PID, [group()], wait=ticking_wait(game))
    assert chk.run(None, stop) == {}
    assert game.writes == []


def test_tc13_lost_link_on_a_later_write_puts_back_the_earlier_ones(game, monkeypatch):
    """The link drops on group 2's write: group 1's test value is put back (here the
    console still takes that write, as when the first request after a hiccup works)."""
    real_write = game.write

    def write(pid, addr, data):
        if addr == OTHER and get(game, OTHER, I32) == 50 and data == pack(I32, 37):
            raise ConnectionLost("dropped")
        real_write(pid, addr, data)

    monkeypatch.setattr(game, "write", write)
    chk = RealChecker(game, PID, [[Target(REAL, F)], [Target(OTHER, I32)]], wait=lambda _s: None)
    with pytest.raises(ConnectionLost):
        chk.run()
    assert get(game, REAL) == 3.0 and get(game, OTHER, I32) == 50


def test_tc14_lost_link_on_the_read_back_puts_everything_back(game, monkeypatch):
    real_read = game.read

    def read(pid, addr, size):
        if game.writes and not hasattr(read, "done"):
            read.done = True
            raise ConnectionLost("dropped")
        return real_read(pid, addr, size)

    monkeypatch.setattr(game, "read", read)
    chk = RealChecker(game, PID, [[Target(REAL, F), Target(DEAD, F)], [Target(OTHER, I32)]])
    chk.wait = lambda _s: None
    with pytest.raises(ConnectionLost):
        chk.run()
    assert get(game, REAL) == 3.0 and get(game, OTHER, I32) == 50


def test_tc15_link_down_for_good_raises_and_does_not_hang(game):
    def wait(_s):
        game.disconnect()

    chk = RealChecker(game, PID, [group()], wait=wait)
    with pytest.raises(ConnectionLost):
        chk.run()


def test_tc16_refused_write_on_one_group_checks_the_others(game, monkeypatch):
    real_write = game.write

    def write(pid, addr, data):
        if addr == DEAD:
            raise WriteFailed("read-only page")
        real_write(pid, addr, data)

    monkeypatch.setattr(game, "write", write)
    chk = run(game, [[Target(DEAD, F)], [Target(REAL, F), Target(HUD, F)]])
    assert chk.results[(DEAD, F)].verdict is Verdict.FAILED
    assert chk.results[(REAL, F)].verdict is Verdict.REAL
    assert get(game, DEAD) == 300.0 and get(game, REAL) == 3.0


def test_tc17_unreadable_part_of_a_cluster(game):
    """Two addresses within READ_SPAN, the second past the end of the heap region:
    the cluster read fails, each is read on its own, the readable one is checked."""
    edge = HEAP_BASE + 0x1000 - 4  # last 4 bytes of the heap region
    gone = HEAP_BASE + 0x1000 + 0x10  # not mapped
    assert gone - edge < READ_SPAN
    game.poke(PID, edge, pack(F, 9.0))
    chk = run(game, [[Target(edge, F), Target(gone, F)]])
    assert chk.results[(edge, F)].verdict is Verdict.KEPT
    assert chk.results[(gone, F)].verdict is Verdict.FAILED
    assert get(game, edge) == 9.0


def test_bug02_area_unloaded_during_the_wait_is_not_checked(game):
    """A HUD area unloads during the 1 s wait. Spec AC-R3: that address is 'not
    checked'. Actual: putting it back raises WriteFailed, the check stops with an error
    and every result (and later round) is lost."""
    gone = 0x5000_0000
    game.add_region(PID, "[anon:menu]", gone, 0x10)
    game.poke(PID, gone, pack(F, 5.0))

    def wait(_s):
        regions = game._procs[PID].regions
        regions[:] = [r for r in regions if r.map.start != gone]
        game.read(PID, HEAP_BASE, 4)

    chk = RealChecker(game, PID, [[Target(gone, F)], group()], wait=wait)
    results = chk.run()
    assert results[(gone, F)].verdict is Verdict.FAILED
    assert results[(REAL, F)].verdict is Verdict.REAL
    assert get(game, REAL) == 3.0


def test_bug04_error_put_back_keeps_the_games_own_value(game, monkeypatch):
    """The read back fails with a protocol error (link still up). Meanwhile the game
    wrote its own value to the copy (HUD = 225). AC-R4: 'the same put-back runs':
    where the game already wrote its own value, that value stays. Actual: the HUD
    copy's stale original (300) is written over the game's value."""
    real_read = game.read
    hud_writes = []

    def read(pid, addr, size):
        if game.writes and not hasattr(read, "done"):
            read.done = True
            game.poke(PID, HUD, pack(F, 225.0))  # the game's own value
            raise ProtocolError("garbled reply")
        return real_read(pid, addr, size)

    monkeypatch.setattr(game, "read", read)
    real_write = game.write

    def write(pid, addr, data):
        if addr == HUD:
            hud_writes.append(data)
        real_write(pid, addr, data)

    monkeypatch.setattr(game, "write", write)
    chk = RealChecker(game, PID, [[Target(HUD, F)]], wait=lambda _s: None)
    with pytest.raises(ProtocolError):
        chk.run()
    assert hud_writes == [pack(F, 225.0)], "only the test value written, never the stale 300"


def test_tc18_read_failed_on_a_later_round_marks_only_that_one(game, monkeypatch):
    real_read = game.read

    def read(pid, addr, size):
        if addr == DEAD and size == 4:
            raise ReadFailed("gone")
        return real_read(pid, addr, size)

    monkeypatch.setattr(game, "read", read)
    far = [Target(REAL, F), Target(DEAD, F)]  # REAL and DEAD read in one cluster
    chk = run(game, [far])
    assert chk.results[(REAL, F)].verdict is Verdict.KEPT
    assert get(game, REAL) == 3.0


def test_bug08_ticking_value_is_not_a_follower_of_a_zero(game):
    """Ammo is 0 (test value 7). A timer in the same group ticks up on every frame.
    AC-R3: a follower moves from x to about x times (test / original); the timer
    doesn't, yet it is counted and the ammo is called real."""
    ammo, timer = HEAP_BASE + 0x600, HEAP_BASE + 0x610
    game.poke(PID, ammo, pack(I32, 0))
    game.poke(PID, timer, pack(I32, 1000))
    game.game_ticks.append(lambda: game.poke(PID, timer, pack(I32, get(game, timer, I32) + 1)))
    chk = run(game, [[Target(ammo, I32), Target(timer, I32)]])
    assert chk.results[(ammo, I32)].verdict is Verdict.KEPT


def test_bug09_write_refused_after_landing_is_put_back(game, monkeypatch):
    """PS5Debug-NG checks a write after the bytes were sent, so a refusal can come after
    the test value landed. The check drops it as 'nothing landed' and never puts the
    original back: the test value stays in the game."""
    real_write = game.write

    def write(pid, addr, data):
        real_write(pid, addr, data)
        if addr == DEAD and data != pack(F, 300.0):
            raise WriteFailed("refused after the bytes were sent")

    monkeypatch.setattr(game, "write", write)
    chk = run(game, [[Target(DEAD, F)]])
    assert chk.results[(DEAD, F)].verdict is Verdict.FAILED
    assert get(game, DEAD) == 300.0, "the test value 225 was left in the game"


# == promote ===========================================================================
def test_tc19_promote_with_nan_and_zero_factors():
    ms = [_member(HEAP_BASE + i * 8, MOVING) for i in range(4)]
    g = Group(ms, {0, 3}, [1.0, math.nan, 2.0, 0.0], "likely real")
    p = promote(g, 2)
    assert [m.address for m in p.members] == [ms[i].address for i in (2, 0, 1, 3)]
    assert p.factors[0] == 1.0 and p.factors[1] == 0.5
    assert math.isnan(p.factors[2]) and p.factors[3] == 0.0
    assert p.likely == {1, 3} and p.label == "likely real"
    nan_head = promote(g, 1)
    assert nan_head.factors[0] == 1.0 and all(math.isnan(x) for x in nan_head.factors[1:])
    zero_head = promote(g, 3)
    assert all(math.isnan(x) for x in zero_head.factors[1:])
    short = promote(Group(ms, set(), [1.0]), 2)
    assert len(short.factors) == 4 and short.factors[0] == 1.0
    assert promote(g, 0) is g


# == UI (AC-R1, AC-R5) ===================================================================
@pytest.fixture
def fake():
    fake = build_fake()
    for a in COPIES:
        fake.add_region(PID, "[anon:hud]", a, 0x10)
    return fake


@pytest.fixture
def win(qtbot, fake):
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()
    yield w
    ctl.shutdown()


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(real_check, "SETTLE_S", 0.0)


def hp_now(fake) -> float:
    return get(fake, HP)


def game_copies_health(fake) -> None:
    def frame() -> None:
        for a in [HP_FRAC, *COPIES]:
            fake.poke(PID, a, pack(F, hp_now(fake) / 250))

    fake.game_ticks.append(frame)


def during_check(fake, fn) -> list:
    """Run ``fn`` once, on the first read after the check wrote something."""
    seen: list = []

    def tick() -> None:
        if fake.writes and not seen:
            seen.append(fn())

    fake.game_ticks.append(tick)
    return seen


def test_tc20_button_enable_states(win, fake):
    f = win.find
    assert not f.check_btn.isEnabled(), "no search yet"
    f.unknown_btn.click()  # thousands of matches: nothing listed
    assert win.ctl.scanner.active and f._n_matches == 0
    assert not f.check_btn.isEnabled()
    f.do_reset()
    search(win, "100")  # 2 matches, listed
    assert f._n_matches == 2 and f.check_btn.isEnabled()
    states = during_check(
        fake, lambda: (f.check_btn.isEnabled(), f.cancel_btn.isEnabled(), f.new_btn.isEnabled())
    )
    f.check_btn.click()
    assert states == [(False, True, False)], "busy: only Cancel"
    assert f.check_btn.isEnabled() and not f.cancel_btn.isEnabled()


def test_tc21_cancel_from_the_ui_stops_and_puts_back(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    start = {a: fake.peek(PID, a, 4) for a in [HP, HP_FRAC, *COPIES]}
    during_check(fake, f.do_cancel)
    f.check_btn.click()
    assert f.hint.text().startswith(hints.CHECK_REAL_STOPPED)
    assert len(f.verdicts) == 1, "stopped after the first round"
    fake.read(PID, HEAP_BASE, 4)
    assert {a: fake.peek(PID, a, 4) for a in start} == start
    assert f._cancel is None and f.check_btn.isEnabled()


def test_tc22_marks_cleared_by_search_lost(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    f.check_btn.click()
    assert f.verdicts
    win.ctl.search_lost.emit()
    assert f.verdicts == {} and not f.check_btn.isEnabled()


def test_tc22b_marks_cleared_by_new_game(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    f.check_btn.click()
    assert f.verdicts
    win.ctl._on_game(win.ctl.session.game)
    assert f.verdicts == {} and not f.check_btn.isEnabled()


def test_tc23_marks_stay_after_search_again(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    f.check_btn.click()
    search(win, "10%")
    assert f.verdicts and f.groups[0].head.address == HP
    assert "checked: real" in f.tree.topLevelItem(0).text(2)


def test_bug03_new_game_during_the_check_drops_its_marks(win, fake):
    """Connect > Refresh finds a new game while the check runs (its signal is handled
    before the check's done callback). The finished check then stores its marks for the
    old game's addresses. AC-R5: marks stay until a new game."""
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    during_check(fake, lambda: win.ctl._on_game(win.ctl.session.game))
    f.check_btn.click()
    assert f.verdicts == {}


def test_bug03_search_lost_during_the_check_drops_its_marks(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    during_check(fake, win.ctl.search_lost.emit)
    f.check_btn.click()
    assert f.verdicts == {}
    assert f.hint.text() == hints.SEARCH_LOST


def test_bug06_lone_group_shows_its_mark(win, fake, monkeypatch):
    """Groups with one match have no expanded rows, and the group row shows only
    'checked: real'. A lone match that kept the test value (what the hint tells the
    player to look for) is marked nowhere."""
    f = win.find
    full_and_three_hits(win, fake)
    sc = win.ctl.scanner
    real_groups = sc.groups

    def split():
        g = real_groups()[0]
        lone = g.members[-1]
        rest = Group(g.members[:-1], g.likely - {len(g.members) - 1}, g.factors[:-1], g.label)
        return [rest, Group([lone], set(), [1.0])]

    monkeypatch.setattr(sc, "groups", split)
    f.refresh_live()
    lone = split()[1].head
    f.check_btn.click()  # nothing writes anything: all keep their test value
    assert f.verdicts[(lone.address, lone.vtype)].verdict is Verdict.KEPT
    top = f.tree.topLevelItem(f.tree.topLevelItemCount() - 1)
    assert top.childCount() == 0 and top.text(3) == f"{lone.address:#x}"
    assert "kept the test value" in top.text(0) + top.text(2)


def test_bug07_hint_when_every_write_is_refused(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    fake.fail_writes = True
    f.check_btn.click()
    assert {r.verdict for r in f.verdicts.values()} == {Verdict.FAILED}
    assert f.hint.text() != hints.CHECK_REAL_KEPT, "nothing was kept: nothing was written"


def test_bug07_hint_when_copies_and_one_not_checked(win, fake, monkeypatch):
    """The game puts every written value back, one address couldn't be written. No row
    says 'kept the test value', yet the hint points the player at such rows."""
    f = win.find
    full_and_three_hits(win, fake)
    fake.game_ticks.append(
        lambda: (
            [fake.poke(PID, a, pack(F, 25.0 if a == HP else 0.1)) for a in [HP, HP_FRAC]]
            + [fake.poke(PID, a, pack(F, 0.1)) for a in COPIES[:2]]
        )
    )
    real_write = fake.write

    def write(pid, addr, data):
        if addr == COPIES[2]:
            raise WriteFailed("read-only")
        real_write(pid, addr, data)

    monkeypatch.setattr(fake, "write", write)
    f.check_btn.click()
    kinds = {r.verdict for r in f.verdicts.values()}
    assert kinds == {Verdict.COPY, Verdict.FAILED}
    assert f.hint.text() != hints.CHECK_REAL_KEPT

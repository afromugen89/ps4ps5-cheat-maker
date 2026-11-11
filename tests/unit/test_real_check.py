"""Check which is real (spec-real-check AC-R1 to AC-R5)."""

import struct
import threading

import pytest

from cheatmaker.console.fake import FakeConsole
from cheatmaker.core.grouping import Group, Member
from cheatmaker.core.real_check import (
    CHECK_LIMIT,
    LIST_LIMIT,
    ROUNDS_LIMIT,
    RealChecker,
    Target,
    Verdict,
    probe_value,
    targets_from_groups,
    targets_from_list,
)
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ConnectionLost
from tests.conftest import HEAP_BASE, PID, build_fake

F = ValueType.FLOAT
I32 = ValueType.INT32
REAL = HEAP_BASE + 0x300  # health / 100, the game's own value
FRAC = HEAP_BASE + 0x310  # worked out from it every frame: health fraction
HUD = HEAP_BASE + 0x800  # HUD copy: health x 100
DEAD = HEAP_BASE + 0x900  # a copy written once, never again


def f32(v: float) -> bytes:
    return struct.pack("<f", v)


def get(fake: FakeConsole, addr: int, vt: ValueType = F) -> float:
    return vt.decode(fake.peek(PID, addr, vt.size))


@pytest.fixture
def game() -> FakeConsole:
    """REAL is only ever written by the test; FRAC and HUD follow it every frame."""
    fake = build_fake()
    fake.poke(PID, REAL, f32(3.0))
    fake.poke(PID, DEAD, f32(300.0))

    def frame() -> None:
        r = get(fake, REAL)
        fake.poke(PID, FRAC, f32(r / 3.0))
        fake.poke(PID, HUD, f32(r * 100))

    frame()
    fake.game_ticks.append(frame)
    return fake


def group() -> list[Target]:
    # best guess first, as grouping orders them: a copy here, to show the order is fixed
    return [Target(HUD, F), Target(FRAC, F), Target(REAL, F), Target(DEAD, F)]


def run(fake, groups, **kw) -> RealChecker:
    chk = RealChecker(fake, PID, groups, wait=lambda _s: fake.read(PID, HEAP_BASE, 4), **kw)
    chk.run()
    return chk


# -- AC-R2 test values ----------------------------------------------------------
@pytest.mark.parametrize(
    ("vt", "cur", "want"),
    [
        (F, 3.0, 2.25),
        (F, 0.0, 7.0),
        (F, float("nan"), 7.0),
        (F, -8.0, -6.0),
        (I32, 300, 225),
        (I32, 2, 1),
        (I32, 1, 8),
        (I32, 0, 7),
        (I32, -40, -30),
        (ValueType.INT8, 127, 95),
    ],
)
def test_probe_value(vt, cur, want):
    assert probe_value(vt, cur) == want
    assert probe_value(vt, cur) != cur


def test_probe_value_fits_the_type():
    assert probe_value(ValueType.INT8, 1) == 8
    _lo, hi = ValueType.INT8.int_range()
    ValueType.INT8.encode(probe_value(ValueType.INT8, hi))


# -- AC-R3 verdicts ---------------------------------------------------------------
def test_real_one_keeps_the_value_and_copies_follow(game):
    chk = run(game, [group()])
    r = chk.results
    assert r[(REAL, F)].verdict is Verdict.REAL and r[(REAL, F)].followers == 2
    assert r[(HUD, F)].verdict is Verdict.COPY
    assert r[(FRAC, F)].verdict is Verdict.COPY
    assert r[(DEAD, F)].verdict is Verdict.KEPT  # nothing writes it, nothing follows it
    assert chk.best(group()) == Target(REAL, F)


def test_everything_is_put_back(game):
    run(game, [group()])
    assert get(game, REAL) == 3.0
    assert get(game, DEAD) == 300.0
    assert get(game, HUD) == 300.0 and get(game, FRAC) == pytest.approx(1.0)


def test_value_the_game_wrote_is_left_alone(game):
    """A copy the game put back keeps the game's value, not the one read before."""
    run(game, [[Target(HUD, F)]])
    assert game.writes == [(PID, HUD, f32(225.0))]  # no put-back write: the game did it


def test_one_member_kept_is_not_called_real(game):
    chk = run(game, [[Target(REAL, F)]])
    assert chk.results[(REAL, F)].verdict is Verdict.KEPT
    assert chk.best([Target(REAL, F)]) is None


def test_all_copies_means_no_best(game):
    g = [Target(HUD, F), Target(FRAC, F)]
    chk = run(game, [g])
    assert {x.verdict for x in chk.results.values()} == {Verdict.COPY}
    assert chk.best(g) is None


def test_groups_are_checked_side_by_side(game):
    """Round 1 writes the first member of every group, so 2 groups of 4 take 4 rounds."""
    other = HEAP_BASE + 0xA00
    game.poke(PID, other, struct.pack("<i", 50))
    chk = RealChecker(game, PID, [group(), [Target(other, I32)]])
    assert chk.rounds == 4
    run(game, [group(), [Target(other, I32)]])
    assert get(game, other, I32) == 50


def test_follower_must_move_by_the_same_factor(game):
    """A value that changes during the check, but not in step, isn't a follower."""
    noise = HEAP_BASE + 0xB00
    game.poke(PID, noise, f32(10.0))
    game.game_ticks.append(lambda: game.poke(PID, noise, f32(get(game, noise) + 1)))
    chk = run(game, [[Target(REAL, F), Target(noise, F)]])
    assert chk.results[(REAL, F)].verdict is Verdict.KEPT


def test_whole_number_copy_rounds(game):
    """A whole-number copy of a decimal (300.0 -> 225) is within the slack."""
    hud_i = HEAP_BASE + 0xC00
    game.game_ticks.append(
        lambda: game.poke(PID, hud_i, struct.pack("<i", round(get(game, REAL) * 100)))
    )
    game.read(PID, HEAP_BASE, 4)
    chk = run(game, [[Target(REAL, F), Target(hud_i, I32)]])
    assert chk.results[(REAL, F)].verdict is Verdict.REAL
    assert chk.results[(hud_i, I32)].verdict is Verdict.COPY


# -- AC-R4 failures --------------------------------------------------------------
def test_refused_write_is_not_checked(game):
    game.fail_writes = True
    chk = run(game, [[Target(REAL, F)]])
    assert chk.results[(REAL, F)].verdict is Verdict.FAILED


def test_unreadable_address_is_not_checked(game):
    gone = 0x7000_0000
    chk = run(game, [[Target(REAL, F), Target(gone, F)]])
    assert chk.results[(gone, F)].verdict is Verdict.FAILED
    assert chk.results[(REAL, F)].verdict is Verdict.KEPT


def test_lost_link_during_wait_puts_back_what_it_can(game):
    def wait(_s):
        raise ConnectionLost("gone")

    chk = RealChecker(game, PID, [[Target(REAL, F)]], wait=wait)
    with pytest.raises(ConnectionLost):
        chk.run()
    assert get(game, REAL) == 3.0


def test_cancel_stops_between_rounds(game):
    stop = threading.Event()
    seen = []

    def progress(f):
        seen.append(f)
        stop.set()

    chk = RealChecker(game, PID, [group()], wait=lambda _s: None)
    chk.run(progress, stop)
    assert seen == [0.25]
    assert len(chk.results) == 1
    assert get(game, HUD) == 300.0


def test_nothing_to_check():
    with pytest.raises(ValueError):
        RealChecker(FakeConsole(), PID, [[]])


# -- AC-R1 what is checked ---------------------------------------------------------
def _member(addr, vals, vt=F):
    return Member(addr, vt, None, tuple(vals))


def test_targets_from_groups_limits_and_skips():
    moving = [(1.0, 0.5, 0.25)]
    g1 = Group([_member(HEAP_BASE + i * 8, *moving) for i in range(ROUNDS_LIMIT + 3)])
    still = Group([_member(HEAP_BASE + 0x900, (5.0, 5.0, 5.0))])
    out = targets_from_groups([g1, still], skip={HEAP_BASE})
    assert len(out) == 1 and len(out[0]) == ROUNDS_LIMIT
    assert out[0][0].address == HEAP_BASE + 8  # the frozen head is skipped
    many = [Group([_member(HEAP_BASE + i * 8, *moving)]) for i in range(CHECK_LIMIT + 5)]
    assert sum(len(g) for g in targets_from_groups(many)) == CHECK_LIMIT


def test_targets_from_list_is_one_group():
    rows = [(HEAP_BASE + i * 4, I32) for i in range(LIST_LIMIT + 4)] + [(0, I32)]
    out = targets_from_list(rows + rows[:2], skip={HEAP_BASE})
    assert len(out) == 1 and len(out[0]) == LIST_LIMIT
    assert Target(HEAP_BASE, I32) not in out[0]
    assert targets_from_list([(0, I32)]) == []


def test_list_mode_finds_the_real_row(game):
    rows = targets_from_list([(HUD, F), (REAL, F), (FRAC, F)])
    chk = run(game, rows)
    assert chk.best(rows[0]) == Target(REAL, F)


def test_same_bytes_in_two_groups_are_not_written_twice(game):
    as_int = Target(REAL, I32)  # the same 4 bytes read as a whole number
    chk = run(game, [[Target(REAL, F)], [as_int]])
    assert chk.results[(REAL, I32)].verdict is Verdict.FAILED
    assert chk.results[(REAL, F)].verdict is Verdict.KEPT
    assert get(game, REAL) == 3.0


# -- review fixes ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("vt", "cur", "want"), [(ValueType.INT8, -128, -96), (ValueType.INT8, -2, -1), (I32, -1, 6)]
)
def test_probe_value_edges(vt, cur, want):
    assert probe_value(vt, cur) == want


def test_tiniest_decimal_is_not_checked(game):
    tiny = HEAP_BASE + 0xD00
    game.poke(PID, tiny, b"\x01\x00\x00\x00")  # 3/4 of it rounds back to itself
    chk = run(game, [[Target(tiny, F)]])
    assert chk.results[(tiny, F)].verdict is Verdict.FAILED
    assert game.writes == []


def test_original_is_read_right_before_the_write(game):
    """The game changes it between the round's first read and its write: that value,
    not the older one, goes back."""
    ammo = HEAP_BASE + 0xD10
    game.poke(PID, ammo, struct.pack("<i", 30))
    reads = []

    def pick_up() -> None:
        reads.append(1)
        if len(reads) == 2:  # after the round's first read, before the write
            game.poke(PID, ammo, struct.pack("<i", 40))

    game.game_ticks.append(pick_up)
    run(game, [[Target(ammo, I32)]])
    assert get(game, ammo, I32) == 40


def test_game_change_after_the_read_back_is_kept(game):
    """A hit lands after the read back, before the put-back: the game's value stays."""
    hp = HEAP_BASE + 0xD20
    game.poke(PID, hp, struct.pack("<i", 100))
    state = {"n": 0}

    def hit() -> None:
        if get(game, hp, I32) == 75:
            state["n"] += 1
            if state["n"] == 2:  # the read back has seen 75; now the game writes 70
                game.poke(PID, hp, struct.pack("<i", 70))

    game.game_ticks.append(hit)
    run(game, [[Target(hp, I32)]])
    assert get(game, hp, I32) == 70


def test_lost_link_on_the_second_write_puts_the_first_back(game):
    other = HEAP_BASE + 0xD30
    game.poke(PID, other, f32(5.0))
    real_write = game.write

    def write(pid, addr, data):
        if addr == other:
            raise ConnectionLost("gone")
        real_write(pid, addr, data)

    game.write = write
    chk = RealChecker(game, PID, [[Target(REAL, F)], [Target(other, F)]], wait=lambda _s: None)
    with pytest.raises(ConnectionLost):
        chk.run()
    assert get(game, REAL) == 3.0


def test_refused_write_that_landed_is_put_back(game):
    from cheatmaker.errors import WriteFailed

    real_write = game.write

    def write(pid, addr, data):
        real_write(pid, addr, data)
        if data == f32(2.25):
            raise WriteFailed("read-back differs")

    game.write = write
    chk = run(game, [[Target(REAL, F)]])
    assert chk.results[(REAL, F)].verdict is Verdict.FAILED
    assert get(game, REAL) == 3.0


def test_failed_put_back_tries_the_rest_and_says_which(game):
    from cheatmaker.errors import WriteFailed

    a, b = HEAP_BASE + 0xD40, HEAP_BASE + 0xD50
    game.poke(PID, a, f32(8.0))
    game.poke(PID, b, f32(8.0))
    real_write = game.write

    def write(pid, addr, data):
        if addr == a and data == f32(8.0):
            raise WriteFailed("no")
        real_write(pid, addr, data)

    game.write = write
    chk = RealChecker(game, PID, [[Target(a, F)], [Target(b, F)]], wait=lambda _s: None)
    with pytest.raises(WriteFailed):
        chk.run()
    assert chk.not_restored == [a]
    assert get(game, b) == 8.0


def test_a_zero_tried_value_only_counts_zeros_that_changed():
    from cheatmaker.core.real_check import _follows

    i = Target(0, I32)
    enc = I32.encode
    assert not _follows(i, enc(1234), enc(1300), 0, 7), "a moving counter isn't a copy of 0"
    assert _follows(i, enc(0), enc(700), 0, 7)
    assert not _follows(i, enc(0), enc(1), 100, 75), "a copy of 100 isn't 0"

"""Builder flows on the fake game (spec-cheat-builder B1 to B9): the game's writers and
our routines run on minicpu; time is a fake clock whose sleeps let the "game" act.

The rollback invariant: every way a test can end without a pass leaves the game's
module bytes exactly as they were before the fix went in.
"""

from __future__ import annotations

import logging
import struct
import threading
from collections import deque
from collections.abc import Callable

import iced_x86 as ix
import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import build_notes as notes
from cheatmaker.core import builder as b
from cheatmaker.core import cave as cv
from cheatmaker.core import real_check as rc
from cheatmaker.core.cave import Effect
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.values import ValueType
from cheatmaker.errors import BuildFailed, ScanCancelled
from tests import fakegame as fg

TR = b.TryResult


class Clock:
    """Fake time. Each sleep runs the next queued game event (None = nothing happens)."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.events: deque[Callable[[], object] | None] = deque()

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.events:
            ev = self.events.popleft()
            if ev is not None:
                ev()


FORM_ADDR = {"float": fg.HP, "int32": fg.HP32, "int64": fg.HP64}


def setup(form="float", effect=Effect.NEVER_DOWN, *, hits=3, **kw):
    g = fg.build(capture_ticks=False)
    f = fg.FORMS[form]
    addr = g.player + FORM_ADDR[form]
    g.fake.write_hits[addr] = [WatchHit(f.end)] * hits
    clock = Clock()
    builder = b.Builder(
        g.session,
        addr,
        f.vtype,
        effect,
        name="Health",
        sleep=clock.sleep,
        clock=clock,
        **kw,
    )
    return g, clock, builder


def module(g) -> tuple[bytes, bytes, bytes]:
    return (
        g.fake.peek(fg.PID, fg.BASE, fg.CODE_SIZE),
        g.fake.peek(fg.PID, fg.DATA, fg.DATA_SIZE),
        g.fake.peek(fg.PID, fg.RODATA, 0x1000),
    )


def hits(*events, idle: int = 0):
    return [*events, *([None] * idle)]


# -- whole builds --------------------------------------------------------------------
def test_simple_game_one_test_then_done(caplog):
    caplog.set_level(logging.INFO)
    g, clock, bl = setup()
    [c] = bl.watch()
    assert c.method is b.Method.HOOK and bl.skipped == []
    before = module(g)
    clock.events.extend(hits(lambda: g.hit(g.player, 60.0), None, lambda: g.hit(g.player, 120.0)))
    progress = []
    out = bl.try_candidate(bl.next_candidate(), progress=lambda f, h: progress.append(h))
    assert out.result is TR.PASS, out
    assert progress[0] == "Testing fix 1 of 1: keep playing and take a hit."
    assert g.health() == 120.0, "a raise goes through"
    assert notes.current_note() is not None, "the fix is still in the game"
    res = bl.finalize()
    cheat = res.cheat
    assert cheat.name == "Health: Never goes down" and cheat.enabled and res.after_restart == "Yes"
    routine, jump = cheat.locations
    assert routine.on == routine.off and jump.off == fg.VMOVSS_W and jump.offset == 0x100
    for loc in cheat.locations:
        assert g.fake.peek(fg.PID, fg.BASE + loc.offset, len(loc.on)) == loc.on
    assert notes.current_note() is None
    slot = cv.find_slot(g.session)
    assert g.fake.peek(fg.PID, slot, 16) == bytes(16), "counter zeroed"
    test_room = routine.offset + fg.BASE - cv.CAVE_ROOM
    assert g.fake.peek(fg.PID, test_room, cv.CAVE_ROOM) == bytes(cv.CAVE_ROOM), "test room zeroed"
    g.hit(g.player, 10.0)
    assert g.health() == 120.0, "the saved routine holds it"
    assert g.fake.peek(fg.PID, slot, 16) == bytes(16), "no counter in the saved routine"
    g.hit(g.enemies[0], 10.0)
    assert g.value(g.enemies[0]) == 250.0, "without Only me, everyone (B3 risk 1)"
    _code, data, ro = module(g)
    assert data == before[1] and ro == before[2]
    text = caplog.text
    for action in ("build_start", "build_writer", "build_try", "build_judge", "build_result"):
        assert f"action={action}" in text, action


def test_sub_writer_with_flags_fix_survives_a_killing_blow():
    g, clock, bl = setup("int32")
    [c] = bl.watch()
    assert c.parts[0].flags_fix and c.method is b.Method.HOOK
    clock.events.extend(hits(lambda: g.hit(g.player, -5, "int32")))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    bl.finalize()
    cpu = g.hit(g.player, 0, "int32")
    assert g.value(g.player, "int32") == 100
    assert not cpu.condition(ix.ConditionCode.LE), "the jle doesn't kill"


def test_always_full_whole_number_keeps_full_editable():
    g, clock, bl = setup("int64", Effect.ALWAYS_FULL)
    g.set_value(g.player, 90_000, "int64")
    bl.watch()
    assert bl.full == 90_000, "Full read just before the watch"
    clock.events.extend(hits(lambda: g.hit(g.player, 5_000, "int64")))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    cheat = bl.finalize().cheat
    assert cheat.value == 90_000 and cheat.value_type is ValueType.INT64
    cheat.set_value(123_456)
    live = LiveCheats(g.session, Freezer(g.session))
    live.turn_on(cheat)
    g.hit(g.player, 1, "int64")
    assert g.value(g.player, "int64") == 123_456


def test_half_damage_decimal():
    g, clock, bl = setup("float", Effect.HALF)
    bl.watch()
    clock.events.extend(hits(lambda: g.hit(g.player, 40.0), None, lambda: g.hit(g.player, 50.0)))
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.PASS, out
    assert g.health() == 60.0  # 100 -> 70 (half of 60), then 70 -> 60 (half of 20)
    assert bl.finalize().cheat.name == "Health: Half damage"


def test_release_and_full_given():
    released = []
    _g, _clock, bl = setup(
        effect=Effect.ALWAYS_FULL, full=250.0, release=lambda: released.append(1)
    )
    bl.watch()
    assert released == [1] and bl.full == 250.0


# -- watching ---------------------------------------------------------------------------
def test_until_waits_five_seconds_after_the_third_write_and_stops_on_cancel():
    _g, _clock, bl = setup()
    stop = bl.until()
    assert not stop([1, 2], 3.0)
    assert not stop([1, 2, 3], 4.0)
    assert not stop([1, 2, 3], 8.9)
    assert stop([1, 2, 3, 4], 9.0)
    cancel = threading.Event()
    stop = bl.until(cancel)
    assert not stop([], 1.0)
    cancel.set()
    assert stop([], 1.5)


def test_watch_cancelled():
    g, _clock, bl = setup()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        bl.watch(cancel)
    assert g.fake.until_answers == [True]


def test_nothing_changed_it():
    g, _clock, bl = setup(hits=0)
    g.fake.write_hits[bl.address] = []
    with pytest.raises(BuildFailed, match="Nothing changed it"):
        bl.watch()


def test_only_skipped_writers():
    g, _clock, bl = setup()
    g.fake.poke(fg.PID, fg.BASE + 0x600, bytes.fromhex("c4c17a1106"))  # vmovss [r14],xmm0
    g.fake.write_hits[bl.address] = [WatchHit(fg.BASE + 0x605)] * 3
    with pytest.raises(BuildFailed, match="None of the safe fixes"):
        bl.watch()
    assert [s.reason for s in bl.skipped] == [b.SHARED]


def test_a_method_that_crashed_is_never_tried_again():
    notes.mark_crashed(fg.TITLE, fg.VERSION, fg.WRITER - fg.BASE, "hook")
    _g, _clock, bl = setup()
    with pytest.raises(BuildFailed, match="None of the safe fixes"):
        bl.watch()
    assert bl.skipped[0].reason == b.CRASHED


def test_unsupported_type():
    g = fg.build(capture_ticks=False)
    with pytest.raises(BuildFailed, match="isn't supported yet"):
        b.Builder(g.session, g.health_addr, ValueType.DOUBLE, Effect.NEVER_DOWN)


def test_real_check_one_group(monkeypatch):
    g, _clock, bl = setup()
    group = [
        rc.Target(g.health_addr, ValueType.FLOAT),
        rc.Target(g.health_addr + 0x40, ValueType.FLOAT),
    ]
    bl.group = group
    assert bl.real_check() == b.NOT_SURE_REAL and bl.address == g.health_addr
    assert g.health() == 100.0, "test values put back"
    monkeypatch.setattr(rc.RealChecker, "best", lambda self, grp: grp[1])
    assert bl.real_check() is None and bl.address == g.health_addr + 0x40
    bl.group = group[:1]
    assert bl.real_check() is None, "nothing to check"


# -- the rollback invariant ----------------------------------------------------------
def _fail_paths():
    return [
        "no hit",
        "effect broken",
        "cancel",
        "not landed",
        "patch gone",
        "slow answer",
        "other thread",
        "error",
        "not in front",
    ]


@pytest.mark.parametrize("path", _fail_paths())
def test_every_fail_path_leaves_the_module_as_it_was(path, monkeypatch):
    g, clock, bl = setup()
    bl.watch()
    before = module(g)
    cancel = threading.Event()
    want = {
        "no hit": TR.NO_HIT,
        "effect broken": TR.FAIL,
        "cancel": TR.CANCELLED,
        "not landed": TR.FAIL,
        "patch gone": TR.LOST,
        "slow answer": TR.LOST,
        "other thread": TR.CANCELLED,
        "not in front": TR.LOST,
    }.get(path)
    if path == "effect broken":  # another code path lowers it: the fix didn't hold
        clock.events.extend(hits(lambda: g.hit(g.player, 60.0), lambda: g.set_health(50.0)))
    elif path == "cancel":
        clock.events.extend(hits(None, None, cancel.set))
    elif path == "not landed":
        g.fake.drop_code_writes = True
    elif path == "patch gone":
        clock.events.extend(hits(lambda: g.fake.poke(fg.PID, fg.WRITER, fg.VMOVSS_W)))
    elif path == "slow answer":
        real_read = g.fake.read

        def slow(pid, addr, size):
            if addr == fg.WRITER:
                clock.now += 3.0
            return real_read(pid, addr, size)

        monkeypatch.setattr(g.fake, "read", slow)
    elif path == "other thread":
        clock.events.extend(hits(None, lambda: bl.close()))
    elif path == "error":

        def boom():
            raise RuntimeError("worker died")

        clock.events.extend(hits(None, boom))
    elif path == "not in front":
        clock.events.extend(hits(lambda: setattr(g.fake, "foreground_pid", 999)))
    if path == "error":
        with pytest.raises(RuntimeError):
            bl.try_candidate(bl.next_candidate(), cancel)
    else:
        out = bl.try_candidate(bl.next_candidate(), cancel)
        assert out.result is want, out
        if path in ("patch gone", "slow answer"):
            assert out.hint.startswith("The game stopped responding with fix 1. Put back.")
    g.fake.drop_code_writes = False
    assert module(g) == before, path
    assert notes.current_note() is None and bl.close() is b.Rollback.NOTHING
    assert not notes.is_crashed(fg.TITLE, fg.VERSION, 0x100, "hook"), "still listed: no crash"


def test_no_app_in_front_but_the_game_still_listed_is_no_crash():
    g, clock, bl = setup()
    bl.watch()
    clock.events.extend(hits(lambda: setattr(g.fake, "foreground_pid", None)))
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.LOST and out.rollback is b.Rollback.RESTORED
    assert not notes.is_crashed(fg.TITLE, fg.VERSION, 0x100, "hook")


def test_no_hit_repeats_the_same_fix_once():
    _g, _clock, bl = setup()
    bl.watch()
    c = bl.next_candidate()
    out = bl.try_candidate(c)
    assert out.result is TR.NO_HIT and out.hint == "You didn't get hit; trying again."
    assert out.rollback is b.Rollback.RESTORED
    again = bl.next_candidate()
    assert again is c
    assert bl.try_candidate(again).result is TR.NO_HIT
    assert bl.next_candidate() is None, "only once"


def test_close_after_a_pass_puts_everything_back():
    g, clock, bl = setup()
    bl.watch()
    before = module(g)
    clock.events.extend(hits(lambda: g.hit(g.player, 50.0)))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    assert module(g) != before
    assert bl.close() is b.Rollback.RESTORED
    assert module(g) == before and bl.close() is b.Rollback.NOTHING
    with pytest.raises(ScanCancelled):  # closed: no step starts any more
        bl.finalize()


def test_game_gone_marks_the_fix_as_a_crash():
    g, clock, bl = setup()
    bl.watch()
    clock.events.extend(hits(lambda: g.fake.remove_process(fg.PID)))
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.LOST and out.hint.startswith("The game stopped with fix 1.")
    assert notes.is_crashed(fg.TITLE, fg.VERSION, fg.WRITER - fg.BASE, "hook")
    assert notes.current_note() is None


def test_lost_link_says_restart_and_keeps_the_note():
    g, clock, bl = setup()
    bl.watch()
    clock.events.extend(hits(g.fake.disconnect))
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.LOST and out.hint == b.RESTART
    assert out.rollback is b.Rollback.NOT_RESTORED
    note = notes.current_note()
    assert note is not None and note.site_off == fg.WRITER - fg.BASE and note.method == "hook"


def test_finalize_with_no_answer_says_restart():
    g, clock, bl = setup()
    bl.watch()
    clock.events.extend(hits(lambda: g.hit(g.player, 50.0)))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    g.fake.fail_writes = True
    with pytest.raises(BuildFailed, match="Restart the game"):
        bl.finalize()


def test_finalize_failure_puts_back_when_it_can(monkeypatch):
    g, clock, bl = setup()
    bl.watch()
    before = module(g)
    clock.events.extend(hits(lambda: g.hit(g.player, 50.0)))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    real_write = g.fake.write
    calls = []

    def flaky(pid, addr, data):
        calls.append(addr)
        if len(calls) == 1:
            from cheatmaker.errors import WriteFailed

            raise WriteFailed("busy")
        return real_write(pid, addr, data)

    monkeypatch.setattr(g.fake, "write", flaky)
    with pytest.raises(BuildFailed, match="unchanged"):
        bl.finalize()
    assert module(g) == before


def test_site_changed_before_the_test_writes_nothing():  # S6
    g, _clock, bl = setup()
    bl.watch()
    g.fake.poke(fg.PID, fg.WRITER, b"\xe9\0\0\0\0" + b"\x90" * 5)  # another cheat's jump
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.FAIL and "isn't what we expected" in out.hint
    assert g.fake.writes == [] and notes.current_note() is None


# -- a plain NOP: the player says whether they got hit ----------------------------------
NOP_SITE = fg.BASE + 0x500
INDEXED = bytes.fromhex("898C88B0020000")  # mov [rax+rcx*4+2B0h],ecx: no jump fits there


def _nop_setup():
    g, clock, bl = setup("int32")
    g.fake.poke(fg.PID, NOP_SITE, INDEXED)
    g.fake.write_hits[bl.address] = [WatchHit(NOP_SITE + len(INDEXED))] * 3
    [c] = bl.watch()
    assert c.method is b.Method.NOP
    return g, clock, bl, c


def test_nop_asks_and_passes_on_yes():
    g, _clock, bl, c = _nop_setup()
    out = bl.try_candidate(c)
    assert out.result is TR.NEEDS_ANSWER and out.question is b.Question.GOT_HIT
    assert g.fake.peek(fg.PID, NOP_SITE, 7) == b"\x90" * 7, "still in while asking"
    assert bl.answer(b.Question.GOT_HIT, b.Answer.YES).result is TR.PASS
    [line] = bl.finalize().cheat.locations
    assert line.on == b"\x90" * 7 and line.off == INDEXED and line.offset == 0x500


def test_nop_no_answer_rolls_back_and_repeats_once():
    g, _clock, bl, c = _nop_setup()
    before = module(g)
    assert bl.try_candidate(c).result is TR.NEEDS_ANSWER
    out = bl.answer(b.Question.GOT_HIT, b.Answer.NO)
    assert out.result is TR.NO_HIT and module(g) == before
    assert bl.next_candidate() is c
    with pytest.raises(BuildFailed):
        bl.answer(b.Question.GOT_HIT, b.Answer.YES)  # nothing waiting any more


# -- Only me ---------------------------------------------------------------------------------
def _fight(g, rounds: int, enemies: bool = True):
    """Collect: the game writes everyone's health with its own value, through the sampler."""
    out = []
    for _ in range(rounds):
        out.append(lambda: g.hit(g.player, g.health()))
        if enemies:
            for e in g.enemies:
                out.append(lambda e=e: g.hit(e, g.value(e)))
    return out


def test_only_me_by_class():
    g, clock, bl = setup(only_me=True)
    bl.watch()
    clock.events.extend(_fight(g, 3))
    assert bl.collect() is None
    assert bl.filter.kind == "vtable" and bl.filter.vtable == fg.PLAYER_VT and bl.by_class
    # The sampler's 64-slot table keeps one of the two enemies (same slot): enough.
    assert bl.others and set(bl.others) <= {e + fg.HP for e in g.enemies}
    clock.events.clear()
    clock.events.extend(
        hits(
            lambda: g.hit(g.player, 50.0),
            lambda: g.hit(g.enemies[0], 200.0),
            lambda: g.hit(g.enemies[1], 200.0),
        )
    )
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.PASS, out
    assert g.health() == 100.0 and g.value(g.enemies[0]) == g.value(g.enemies[1]) == 200.0
    res = bl.finalize()
    assert res.cheat.name == "Health: Never goes down (only me)" and res.after_restart == "Yes"


def test_only_me_by_field_asks_about_enemies_when_none_went_down():
    g, clock, bl = setup(only_me=True)
    for e in g.enemies:  # same class as the player: tell apart by a field
        g.fake.poke(fg.PID, e, struct.pack("<Q", fg.PLAYER_VT))
    bl.watch()
    clock.events.extend(_fight(g, 3))
    assert bl.collect() is None
    assert bl.filter.kind == "field" and bl.filter.field == 8 and not bl.by_class
    clock.events.clear()
    clock.events.extend(hits(lambda: g.hit(g.player, 50.0)))
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.NEEDS_ANSWER and out.question is b.Question.ENEMIES
    assert bl.answer(b.Question.ENEMIES, b.Answer.YES).result is TR.PASS
    assert bl.finalize().after_restart == "Check after restart"


def test_only_me_cant_tell_apart_after_two_collects():
    g, clock, bl = setup(only_me=True)
    bl.watch()
    clock.events.extend(_fight(g, 3, enemies=False))
    assert bl.collect() == b.ONLY_YOU_SEEN
    clock.events.extend(_fight(g, 3, enemies=False))
    with pytest.raises(BuildFailed, match="Couldn't tell you apart"):
        bl.collect()
    with pytest.raises(BuildFailed):
        bl.try_candidate(bl.next_candidate())  # no filter: never patched for everyone


def test_alive_falls_back_to_the_process_list(monkeypatch):
    g, clock, bl = setup()
    bl.watch()

    def unsupported():
        raise NotImplementedError

    monkeypatch.setattr(g.fake, "foreground_app", unsupported)
    clock.events.extend(hits(lambda: g.hit(g.player, 50.0)))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    bl.rollback()
    real, gone = g.fake.processes, []
    clock.events.extend(hits(*[None] * 6, lambda: gone.append(1)))
    monkeypatch.setattr(g.fake, "processes", lambda: [] if gone else real())
    out = bl.try_candidate(bl.next_candidate() or b.Candidate(bl.tried[0][0].parts))
    assert out.result is TR.LOST and notes.is_crashed(fg.TITLE, fg.VERSION, 0x100, "hook")


# -- close() on another thread (Cancel, Disconnect) -------------------------------------
class _Blocked:
    """Hold the first matching write of the fake console until ``go`` is set."""

    def __init__(self, g, monkeypatch, when: Callable[[int, int], bool]) -> None:
        self.inside, self.go = threading.Event(), threading.Event()
        self.n = 0
        real = g.fake.write

        def write(pid, addr, data):
            real(pid, addr, data)
            self.n += 1
            if not self.inside.is_set() and when(self.n, addr):
                self.inside.set()
                self.go.wait(5)

        monkeypatch.setattr(g.fake, "write", write)


def _in_thread(fn) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # handed to the test thread
            out["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def test_close_on_another_thread_waits_for_the_install_then_puts_back(monkeypatch):
    g, _clock, bl = setup()
    bl.watch()
    before = module(g)
    held = _Blocked(g, monkeypatch, lambda n, _a: n == 1)  # the routine is in, the jump not
    worker, wout = _in_thread(lambda: bl.try_candidate(bl.next_candidate()))
    assert held.inside.wait(5)
    closer, cout = _in_thread(bl.close)
    closer.join(0.3)
    assert closer.is_alive(), "close waits for the writes in progress"
    held.go.set()
    closer.join(5)
    worker.join(5)
    # Either thread may do the put back once the writes end; both report it.
    assert cout["value"] is b.Rollback.RESTORED
    out = wout["value"]
    assert out.result is TR.CANCELLED and out.rollback is b.Rollback.RESTORED, out
    assert module(g) == before and notes.current_note() is None


def test_close_on_another_thread_during_finalize_waits_and_the_cheat_matches(monkeypatch):
    g, clock, bl = setup()
    bl.watch()
    clock.events.extend(hits(lambda: g.hit(g.player, 50.0)))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    held = _Blocked(g, monkeypatch, lambda n, _a: n == 1)  # the saved routine is in
    worker, wout = _in_thread(bl.finalize)
    assert held.inside.wait(5)
    closer, cout = _in_thread(bl.close)
    closer.join(0.3)
    assert closer.is_alive(), "close waits for finalize's writes"
    held.go.set()
    worker.join(5)
    closer.join(5)
    assert "error" not in wout, wout
    cheat = wout["value"].cheat
    assert cout["value"] is b.Rollback.NOTHING, "nothing left to put back: the fix is kept"
    for loc in cheat.locations:  # the cheat said to be on is what the game has
        assert g.fake.peek(fg.PID, fg.BASE + loc.offset, len(loc.on)) == loc.on


def test_finalize_after_close_keeps_nothing():
    g, clock, bl = setup()
    bl.watch()
    before = module(g)
    clock.events.extend(hits(lambda: g.hit(g.player, 50.0)))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    cancel = threading.Event()
    cancel.set()  # Cancel pressed while the pass was on its way to the dialog
    with pytest.raises(ScanCancelled):
        bl.finalize(cancel)
    assert module(g) == before and notes.current_note() is None


def test_close_that_cant_wait_says_not_restored_and_the_worker_puts_back(monkeypatch):
    g, _clock, bl = setup()
    bl.watch()
    before = module(g)
    held = _Blocked(g, monkeypatch, lambda n, _a: n == 1)
    worker, wout = _in_thread(lambda: bl.try_candidate(bl.next_candidate()))
    assert held.inside.wait(5)
    assert bl.close(wait=0.05) is b.Rollback.NOT_RESTORED, "never claims a put back"
    assert bl.not_restored
    held.go.set()
    worker.join(5)
    out = wout["value"]
    assert out.result is TR.CANCELLED and out.rollback is b.Rollback.RESTORED, out
    assert module(g) == before


def test_a_put_back_not_taken_stops_the_build():
    g, clock, bl = setup()
    bl.watch()
    clock.events.extend([lambda: setattr(g.fake, "fail_writes", True)])
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.NO_HIT and out.rollback is b.Rollback.NOT_RESTORED
    assert bl.not_restored and notes.current_note() is not None, "the note stays open"
    g.fake.fail_writes = False
    with pytest.raises(BuildFailed, match="Restart the game"):
        bl.try_candidate(bl.next_candidate())
    assert bl.close() is b.Rollback.NOT_RESTORED, "close repeats it"


def test_put_back_on_this_thread_while_installing_is_redone(monkeypatch):
    g, _clock, bl = setup()
    bl.watch()
    before = module(g)
    real = g.fake.write
    n = []

    def write(pid, addr, data):
        real(pid, addr, data)
        n.append(addr)
        if len(n) == 1:
            bl.rollback("other")  # as a put back between our two writes

    monkeypatch.setattr(g.fake, "write", write)
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.CANCELLED and out.rollback is b.Rollback.RESTORED
    assert module(g) == before


def test_collect_hook_is_noted_and_close_on_another_thread_takes_it_out():
    g, clock, bl = setup(only_me=True)
    bl.watch()
    before = module(g)
    seen, go = threading.Event(), threading.Event()

    def collecting():
        note = notes.current_note()
        assert note is not None and note.method == "collect" and note.site_off == 0x100
        seen.set()
        go.wait(5)

    clock.events.extend([None, collecting])
    worker, wout = _in_thread(bl.collect)
    assert seen.wait(5)
    closer, cout = _in_thread(bl.close)
    closer.join(0.2)  # close puts back at once: the hook isn't a run of writes
    go.set()
    closer.join(5)
    worker.join(5)
    assert cout["value"] is b.Rollback.RESTORED
    assert isinstance(wout.get("error"), ScanCancelled), wout
    assert module(g) == before and notes.current_note() is None


def test_collect_puts_the_sampler_back_when_done():
    g, clock, bl = setup(only_me=True)
    bl.watch()
    before = module(g)
    clock.events.extend(_fight(g, 3))
    assert bl.collect() is None
    assert module(g) == before and notes.current_note() is None


def test_typed_full_is_what_the_game_stores_so_the_nop_is_offered():
    g, _clock, bl = setup("float", Effect.ALWAYS_FULL, full=99.9)
    assert bl.full == struct.unpack("<f", struct.pack("<f", 99.9))[0]
    g.set_health(bl.full)
    assert any(c.method is b.Method.NOP for c in bl.watch()), "it is Full right now"


def test_hit_during_the_value_read_is_read_again(monkeypatch):
    g, _clock, bl = setup("float", Effect.HALF)
    bl.watch()
    real_read = g.fake.read
    seen, done = [], []

    def read(pid, addr, size):
        hooked = g.fake.peek(fg.PID, fg.WRITER, 1) == b"\xe9"
        if addr == bl.address and hooked:
            seen.append(1)
            if len(seen) == 2:  # the first poll (the first read is the start value)
                done.append(1)
                out = real_read(pid, addr, size)  # the value before the hit ...
                g.hit(g.player, 40.0)  # ... then the counter moves
                return out
        return real_read(pid, addr, size)

    monkeypatch.setattr(g.fake, "read", read)
    out = bl.try_candidate(bl.next_candidate())
    assert done and g.health() == 70.0
    assert out.result is TR.PASS, out


def test_a_reading_that_never_settles_is_dropped(monkeypatch):
    g, _clock, bl = setup()
    bl.watch()
    inst = bl._prepare(bl.next_candidate(), 1)
    real_read = g.fake.read
    count = iter(range(1, 100))

    def read(pid, addr, size):
        if addr == inst.slot:
            return struct.pack("<Q", next(count)) + bytes(8)
        return real_read(pid, addr, size)

    monkeypatch.setattr(g.fake, "read", read)
    assert bl._sample(inst) is None


def test_finalize_notes_the_re_aimed_jump_before_writing_it(monkeypatch):
    g, clock, bl = setup()
    bl.watch()
    clock.events.extend(hits(lambda: g.hit(g.player, 50.0)))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    real_write, noted = g.fake.write, []

    def write(pid, addr, data):
        if addr == fg.WRITER:  # the jump is re-aimed: the note already says so
            [p] = notes.current_note().patches
            noted.append(bytes.fromhex(p.new_hex) == data)
        real_write(pid, addr, data)

    monkeypatch.setattr(g.fake, "write", write)
    bl.finalize()
    assert noted == [True]


def test_only_me_skips_writers_that_reach_the_value_another_way():
    g, clock, bl = setup(only_me=True)
    other = fg.BASE + 0x700
    g.fake.poke(fg.PID, other, bytes.fromhex("C4C17A118C24A0010000"))  # [r12+1A0h]
    g.fake.write_hits[bl.address] += [WatchHit(other + 10)] * 2
    cands = bl.watch()
    assert len(cands) == 3, "both writers, then all of them"
    clock.events.extend(_fight(g, 3))
    assert bl.collect() is None
    assert [c.parts[0].writer.address for c in bl.queue] == [fg.WRITER]
    assert [len(c.parts) for c in bl.queue] == [1]
    [s] = [s for s in bl.skipped if s.writer.address == other]
    assert s.reason == b.OTHER_PLACE_ONLY_ME


# -- the test counter's place (review 12) -----------------------------------------------
def test_a_counter_place_the_game_writes_to_is_left_alone(caplog):
    caplog.set_level(logging.INFO)
    g, clock, bl = setup()
    bl.watch()
    slot = cv.find_slot(g.session)
    game_data = struct.pack("<Q", 0) + b"GAME!\0\0\0"
    clock.events.extend(
        hits(lambda: g.hit(g.player, 50.0), lambda: g.fake.poke(fg.PID, slot, game_data))
    )
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is not TR.PASS and out.rollback is b.Rollback.RESTORED, out
    assert g.fake.peek(fg.PID, fg.WRITER, 10) == fg.VMOVSS_W
    assert g.fake.peek(fg.PID, slot, 16) == game_data, "not zeroed: the game's"
    assert "action=build_slot_changed" in caplog.text


def test_a_counter_place_in_use_at_install_writes_nothing(monkeypatch):
    g, _clock, bl = setup()
    bl.watch()
    before = module(g)
    real_prepare = bl._prepare

    def prepare(c, n):
        inst = real_prepare(c, n)
        g.fake.poke(fg.PID, inst.slot, b"\1" * 16)  # the game took it meanwhile
        return inst

    monkeypatch.setattr(bl, "_prepare", prepare)
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.FAIL and out.hint == b.SLOT_IN_USE
    assert g.fake.peek(fg.PID, fg.WRITER, 10) == fg.VMOVSS_W and notes.current_note() is None
    assert module(g)[0] == before[0]


def test_the_kept_fix_is_marked_on_without_writing_again(monkeypatch):
    g, clock, bl = setup()
    live = bl.live = LiveCheats(g.session, Freezer(g.session))
    bl.watch()
    clock.events.extend(hits(lambda: g.hit(g.player, 50.0)))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    writes = []
    real = g.fake.write
    monkeypatch.setattr(g.fake, "write", lambda p, a, d: (writes.append(a), real(p, a, d)))
    cheat = bl.finalize().cheat
    assert cheat.enabled and live.active[id(cheat)] is cheat
    room = cheat.locations[0].offset + fg.BASE
    assert writes.count(fg.WRITER) == 1 and writes.count(room) == 1, "each written once"

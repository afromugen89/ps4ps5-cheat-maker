"""QA for the smart cheat builder, "Make it work..." (docs/spec-cheat-builder.md B1-B9).

Report: qa/reports/2026-11-11-cheat-builder.md. Test case IDs (TC-xx) match the report.
Bugs found are strict xfail tests marked "BUG-nn": they fail today and must pass
once fixed (then remove the marker).

The game is tests/fakegame.py (writers and routines run on tests/minicpu.py); time is
the fake clock from the builder flow tests, whose sleeps let the game act.
"""

from __future__ import annotations

import json
import logging

import iced_x86 as ix
import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import build_notes as notes
from cheatmaker.core import builder as b
from cheatmaker.core import cave as cv
from cheatmaker.core.cave import Effect
from cheatmaker.core.locker import FLAGS_UNSAFE, FlagsUse, Writer
from cheatmaker.core.values import ValueType
from cheatmaker.errors import BuildFailed
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests import fakegame as fg
from tests.ui.test_build_ui import Harness, add_and_select, health_cheat
from tests.unit.test_builder_flow import Clock, hits, module, setup

TR = b.TryResult
INT32, INT64, FLOAT = ValueType.INT32, ValueType.INT64, ValueType.FLOAT
NONE = FlagsUse()


def _w(code: str, hits_: int = 1, at: int = 0x400400, **kw) -> Writer:
    return Writer(at, bytes.fromhex(code), code, hits_, **kw)


def _module_code(g: fg.Game) -> bytes:
    return g.fake.peek(fg.PID, fg.BASE, fg.CODE_SIZE) + g.fake.peek(fg.PID, fg.DATA, fg.DATA_SIZE)


# -- fixtures (own names: tests/qa/conftest.py already has a "win") -----------------------
@pytest.fixture
def bgame():
    g = fg.build(capture_ticks=False)
    g.fake.write_hits[g.health_addr] = [WatchHit(fg.WRITER + 10)] * 3
    return g


@pytest.fixture
def bwin(qtbot, bgame):
    ctl = Controller(lambda ip, port: bgame.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("1.2.3.4")
    w.connect_view.connect_btn.click()
    assert ctl.session.game is not None
    yield w
    ctl.shutdown()


@pytest.fixture
def bclock():
    return Clock()


@pytest.fixture
def bh(bwin, bclock):
    h = Harness(bwin, bclock)
    h.hide_notice = True
    return h


# == B3: classification S1-S7 (pure) ====================================================
@pytest.mark.parametrize(
    ("code", "vtype", "why"),
    [
        ("8908", INT32, b.SHARED),  # TC-10 S1: mov [rax],ecx
        ("F00188B0020000", INT32, "atomic"),  # TC-11 S2: lock add [rax+2B0h],ecx
        ("8788B0020000", INT32, "atomic"),  # TC-11 S2: xchg [rax+2B0h],ecx (implicit lock)
        ("0FC188B0020000", INT32, "atomic"),  # TC-11 S2: xadd
        ("48898BB8020000", INT32, b.WRONG_SIZE),  # TC-14 S4: 8-byte store, 4-byte value
        ("0F1188B0020000", FLOAT, b.WRONG_SIZE),  # TC-14 S4: movups (16 bytes) over a float
    ],
)
def test_tc10_14_rules_skip_every_method(code, vtype, why):
    w = _w(code, flags=NONE)
    from cheatmaker.core.locker import decode_one, nop_unsafe

    w = Writer(w.address, w.code, w.text, 1, nop_unsafe(decode_one(w.code, w.address)), False, NONE)
    if why == b.SHARED:
        w = Writer(w.address, w.code, w.text, 1, None, True, NONE)
    for effect in Effect:
        for only_me in (False, True):
            eligible, skipped = b.classify([w], vtype, effect, only_me)
            assert eligible == [] and len(skipped) == 1, (effect, only_me)
            assert why in skipped[0].reason


def test_tc12_flags_rules():
    """S3: ZF/SF/OF read -> no NOP, hook with flags_fix; CF/PF/AF read or unknown -> skip."""
    sub = bytes.fromhex("2988B0020000")
    zs = Writer(fg.SUB_SITE, sub, "sub", 1, FLAGS_UNSAFE, False, FlagsUse(frozenset({"ZF", "SF"})))
    [e], _ = b.classify([zs], INT32, Effect.NEVER_DOWN, False)
    assert e.methods == (b.Method.HOOK,) and e.flags_fix
    eligible, _ = b.classify([zs], INT32, Effect.ALWAYS_FULL, False, now_full=True)
    assert eligible[0].methods == (b.Method.HOOK,), "no NOP when flags are read"
    cf = Writer(fg.SUB_SITE, sub, "sub", 1, FLAGS_UNSAFE, False, FlagsUse(frozenset({"CF"})))
    assert b.classify([cf], INT32, Effect.HALF, False)[1][0].reason == b.FLAGS_CARRY
    unk = Writer(fg.SUB_SITE, sub, "sub", 1, FLAGS_UNSAFE, False, FlagsUse(unknown=True))
    assert b.classify([unk], INT32, Effect.HALF, False)[1][0].reason == b.FLAGS_UNKNOWN
    unchecked = Writer(fg.SUB_SITE, sub, "sub", 1)  # flags never looked at
    assert b.classify([unchecked], INT32, Effect.HALF, False)[1][0].reason == b.FLAGS_UNKNOWN


def test_tc13_string_store_is_skipped():
    """movsd / rep stosd: they move rdi/rsi(/rcx), so S2 skips them for every method."""
    from cheatmaker.core.locker import decode_one, nop_unsafe

    for code in ("A5", "F3AB"):
        raw = bytes.fromhex(code)
        why = nop_unsafe(decode_one(raw, 0x400400))
        w = Writer(0x400400, raw, code, 1, why, False, NONE)
        assert why is not None
        _, [s] = b.classify([w], INT32, Effect.NEVER_DOWN, False)
        assert s.reason == why


def test_tc15_crashed_method_only_that_method():
    """S7: a crashed hook leaves the NOP (when allowed) and vice versa."""
    w = Writer(fg.WRITER, fg.VMOVSS_W, "vmovss", 1, None, False, NONE)
    crashed = lambda _w, m: m is b.Method.HOOK  # noqa: E731
    eligible, _ = b.classify([w], FLOAT, Effect.ALWAYS_FULL, False, crashed, now_full=True)
    assert eligible[0].methods == (b.Method.NOP,)
    _, [s] = b.classify([w], FLOAT, Effect.HALF, False, crashed)
    assert s.reason == b.CRASHED


def test_tc16_only_me_never_nops_and_short_writer_reason():
    short = Writer(0x400200, bytes.fromhex("894808"), "mov [rax+8],ecx", 1, None, False, NONE)
    _, [s] = b.classify([short], INT32, Effect.NEVER_DOWN, True)
    assert s.reason == b.TOO_SHORT_ONLY_ME
    [e], _ = b.classify([short], INT32, Effect.NEVER_DOWN, False)
    assert e.methods == (b.Method.NOP,), "under 5 bytes: plain NOP for Never goes down"
    _, [s] = b.classify([short], INT32, Effect.HALF, False)
    assert s.reason == b.NO_METHOD, "half damage has no NOP"


# == B4: candidates order and cap ========================================================
def _hookable(k: int, hits_: int) -> Writer:
    # mov [rax+disp32],ecx: 6 bytes, hookable; distinct addresses
    return Writer(
        0x400400 + 0x10 * k, bytes.fromhex("8988B0020000"), f"w{k}", hits_, None, False, NONE
    )


def test_tc20_most_hits_first_then_all():
    ws = [_hookable(0, 2), _hookable(1, 9), _hookable(2, 5)]
    eligible, _ = b.classify(ws, INT32, Effect.HALF, False)
    cands = b.candidates(eligible)
    assert [c.parts[0].writer.hits for c in cands[:3]] == [9, 5, 2]
    assert len(cands) == 4 and len(cands[3].parts) == 3, "then the 1st method on all"


def test_tc21_cap_of_six():
    ws = [_hookable(k, 10 - k) for k in range(8)]
    eligible, _ = b.classify(ws, INT32, Effect.ALWAYS_FULL, False, now_full=True)
    cands = b.candidates(eligible)
    assert len(cands) == b.MAX_CANDIDATES == 6
    single = [_hookable(0, 1)]
    e1, _ = b.classify(single, INT32, Effect.HALF, False)
    assert len(b.candidates(e1)) == 1, "one writer: no 'all' candidate"


# == B5: judge tolerances ===============================================================
def _d(effect, vtype, start, values, counts=None, game_new=None, **kw):
    counts = counts or [1] * len(values)
    game_new = game_new or [None] * len(values)
    later = [b.Sample(v, c, g) for v, c, g in zip(values, counts, game_new, strict=True)]
    samples = (b.Sample(start), *later)
    return b.TestData(effect, vtype, start, samples, **kw)


@pytest.mark.parametrize(
    ("vtype", "low", "outcome"),
    [
        (FLOAT, 99.995, b.Outcome.PASS),  # within 0.01
        (FLOAT, 99.98, b.Outcome.FAIL),
        (INT32, 99, b.Outcome.FAIL),  # whole numbers exact
        (INT64, 100, b.Outcome.PASS),
    ],
)
def test_tc30_never_down_tolerance(vtype, low, outcome):
    assert b.judge(_d(Effect.NEVER_DOWN, vtype, 100, [low])).outcome is outcome


@pytest.mark.parametrize(
    ("vtype", "value", "outcome"),
    [
        (FLOAT, 1004.9, b.Outcome.PASS),  # within 0.5 % of 1000
        (FLOAT, 1005.2, b.Outcome.FAIL),
        (INT32, 999, b.Outcome.FAIL),
        (INT32, 1000, b.Outcome.PASS),
    ],
)
def test_tc31_always_full_tolerance(vtype, value, outcome):
    d = _d(Effect.ALWAYS_FULL, vtype, 500, [value], full=1000)
    assert b.judge(d).outcome is outcome


def test_tc31b_always_full_ignores_readings_before_the_first_tick():
    d = _d(Effect.ALWAYS_FULL, INT32, 500, [400, 1000], counts=[0, 1], full=1000)
    assert b.judge(d).outcome is b.Outcome.PASS


@pytest.mark.parametrize(
    ("vtype", "prev", "game_new", "cur", "outcome"),
    [
        (INT32, 10, 3, 6, b.Outcome.PASS),  # 7 -> 4
        (INT32, 10, 9, 9, b.Outcome.PASS),  # 1 -> 1
        (INT32, 10, 3, 7, b.Outcome.FAIL),  # 7 -> 3: rounded the wrong way
        (INT64, 2**40, 2**40 - 7, 2**40 - 4, b.Outcome.PASS),
        (FLOAT, 100.0, 40.0, 70.5, b.Outcome.PASS),  # within 2 % of the game's drop
        (FLOAT, 100.0, 40.0, 72.0, b.Outcome.FAIL),
    ],
)
def test_tc32_half_damage(vtype, prev, game_new, cur, outcome):
    d = _d(Effect.HALF, vtype, prev, [cur], game_new=[game_new])
    assert b.judge(d).outcome is outcome


def test_tc33_no_counter_tick_is_no_hit_and_half_needs_a_drop():
    assert b.judge(_d(Effect.NEVER_DOWN, INT32, 100, [100], counts=[0])).outcome is b.Outcome.NO_HIT
    raise_only = _d(Effect.HALF, INT32, 100, [120], game_new=[120])
    assert b.judge(raise_only).outcome is b.Outcome.NO_HIT


def test_tc34_dead_game_fails_first_and_only_me_needs_yes():
    assert b.judge(_d(Effect.NEVER_DOWN, INT32, 100, [100], alive=False)).outcome is b.Outcome.FAIL
    d = _d(Effect.NEVER_DOWN, INT32, 100, [100], only_me=True, others_down=False)
    assert b.judge(d).question is b.Question.ENEMIES
    no = b.judge(d, {b.Question.ENEMIES: b.Answer.NOT_SURE})
    assert no.outcome is b.Outcome.FAIL
    assert b.judge(d, {b.Question.ENEMIES: b.Answer.YES}).outcome is b.Outcome.PASS


# == effect semantics on minicpu (beyond the existing matrix) ============================
@pytest.mark.parametrize(
    ("form", "effect", "old", "new", "want"),
    [
        ("int32", Effect.HALF, 100, 93, 96),  # 7 -> 4
        ("int32", Effect.HALF, 100, 99, 99),  # 1 -> 1
        ("int64", Effect.HALF, 100, 99, 99),
        ("int64", Effect.HALF, -(2**63), 2**63 - 1, 2**63 - 1),  # raise, overflowing sub
        ("int64", Effect.NEVER_DOWN, -5, -6, -5),  # signed compare
        ("int64", Effect.NEVER_DOWN, -5, 2**40, 2**40),
        ("int32", Effect.NEVER_DOWN, 100, 100, 100),  # equal
        ("int32", Effect.ALWAYS_FULL, 100, 500, 250),  # raise is replaced by Full too
    ],
)
def test_tc40_effects_on_the_cpu(form, effect, old, new, want):
    from tests.unit.test_effect_code import install

    g = fg.build()
    g.set_value(g.player, old, form)
    install(g, form, effect)
    g.hit(g.player, new, form)
    assert g.value(g.player, form) == want


# == B5/B6 flows ==========================================================================
def test_tc50_no_hit_then_pass_on_the_repeat():
    g, clock, bl = setup()
    bl.watch()
    c = bl.next_candidate()
    assert bl.try_candidate(c).result is TR.NO_HIT
    again = bl.next_candidate()
    assert again is c
    clock.events.extend(hits(lambda: g.hit(g.player, 40.0)))
    assert bl.try_candidate(again).result is TR.PASS
    assert bl.tried[-1][1].result is TR.PASS


def test_tc51_note_survives_lost_link_and_reconnect_puts_back():
    """B6 both ways: a lost link keeps the note; the same game process on the next
    connect gets its bytes back and no crash mark."""
    g, clock, bl = setup()
    bl.watch()
    before = module(g)
    clock.events.extend(hits(g.fake.disconnect))
    out = bl.try_candidate(bl.next_candidate())
    assert out.rollback is b.Rollback.NOT_RESTORED and notes.current_note() is not None
    assert module(g) != before, "the fix is still in the game"
    g.session.disconnect()
    g.fake.connected = True  # the link is back, same game process
    g.session.connect("1.2.3.4")
    g.session.refresh_game()
    hint = notes.check_on_connect(g.session, sleep=lambda _s: None)
    assert "put back" in hint
    assert module(g) == before and notes.current_note() is None
    assert not notes.is_crashed(fg.TITLE, fg.VERSION, fg.WRITER - fg.BASE, "hook")


def test_tc52_saved_cheat_has_no_counter_and_still_works():
    g, clock, bl = setup("int32", Effect.HALF)
    bl.watch()
    clock.events.extend(hits(lambda: g.hit(g.player, 90, "int32")))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    cheat = bl.finalize().cheat
    slot = cv.find_slot(g.session)
    assert g.fake.peek(fg.PID, slot, 16) == bytes(16)
    routine = cheat.locations[0]
    insns = list(ix.Decoder(64, routine.on, ip=fg.BASE + routine.offset))
    assert not any(i.is_ip_rel_memory_operand for i in insns), "no counter lines kept"
    g.set_value(g.player, 100, "int32")
    g.hit(g.player, 93, "int32")
    assert g.value(g.player, "int32") == 96, "saved routine halves (7 -> 4)"
    assert g.fake.peek(fg.PID, slot, 16) == bytes(16), "saved routine never counts"


# -- bugs -------------------------------------------------------------------------------
def test_bug01_rollback_during_install_leaves_the_jump(monkeypatch):
    g, _clock, bl = setup()
    bl.watch()
    before = module(g)
    real_write = g.fake.write
    state = {"n": 0, "inside": False}

    def write(pid, addr, data):
        real_write(pid, addr, data)
        state["n"] += 1
        if state["n"] == 1 and not state["inside"]:  # the routine is in, the jump not yet
            state["inside"] = True
            bl.close()  # what Cancel / before_disconnect does on the UI thread

    monkeypatch.setattr(g.fake, "write", write)
    bl.try_candidate(bl.next_candidate())
    assert g.fake.peek(fg.PID, fg.WRITER, 10) == fg.VMOVSS_W, "the jump stayed in"
    assert module(g) == before


@pytest.mark.xfail(
    reason="BUG-04: value read before the counter: a hit between the two reads fails a "
    "correct Half damage fix",
    strict=True,
)
def test_bug04_hit_between_value_and_counter_reads(monkeypatch):
    g, _clock, bl = setup("float", Effect.HALF)
    bl.watch()
    slot = cv.find_slot(g.session)
    real_read = g.fake.read
    state = {"done": False}

    def read(pid, addr, size):
        hooked = g.fake.peek(fg.PID, fg.WRITER, 1) == b"\xe9"  # the test is running
        if addr == slot and size == cv.SLOT_SIZE and hooked and not state["done"]:
            state["done"] = True
            g.hit(g.player, 40.0)  # the game writes between our value and counter reads
        return real_read(pid, addr, size)

    monkeypatch.setattr(g.fake, "read", read)
    out = bl.try_candidate(bl.next_candidate())
    assert g.health() == 70.0, "the routine did halve the hit"
    assert out.result is TR.PASS, out


@pytest.mark.xfail(
    reason="BUG-05: a damaged crash-notes file ('crashed': null) stops the builder with a "
    "TypeError",
    strict=True,
)
@pytest.mark.parametrize("crashed", [None, 5, {"a": 1}, [{"writer_off": "x"}]])
def test_bug05_damaged_crash_list(crashed):
    _g, _clock, bl = setup()
    notes.notes_path().parent.mkdir(parents=True, exist_ok=True)
    notes.notes_path().write_text(json.dumps({"open": None, "crashed": crashed}), "utf-8")
    assert bl.watch(), "the build goes on as with no crash marks"
    notes.mark_crashed(fg.TITLE, fg.VERSION, 0x100, "hook")
    assert notes.is_crashed(fg.TITLE, fg.VERSION, 0x100, "hook")


def test_bug06_close_while_finalizing():
    g, clock, bl = setup()
    bl.watch()
    before = module(g)
    clock.events.extend(hits(lambda: g.hit(g.player, 50.0)))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    clock.events.clear()
    clock.events.append(bl.close)  # finalize's 2 s wait: the dialog is closed
    try:
        res = bl.finalize()
    except BuildFailed:
        assert module(g) == before
        return
    for loc in res.cheat.locations:  # a cheat said to be on must be in the game
        assert g.fake.peek(fg.PID, fg.BASE + loc.offset, len(loc.on)) == loc.on, loc


@pytest.mark.xfail(
    reason="BUG-07: Only me's Collect hook isn't taken out by close()/Disconnect",
    strict=True,
)
def test_bug07_disconnect_during_collect_leaves_the_sampler_hook():
    g, clock, bl = setup(only_me=True)
    bl.watch()
    before = g.fake.peek(fg.PID, fg.WRITER, 10)

    def disconnect():
        bl.close()  # Controller.run_before_disconnect -> dialog._close_builder
        g.fake.disconnect()

    clock.events.extend([None, disconnect])
    with pytest.raises(Exception):  # noqa: B017 - the link is gone
        bl.collect()
    assert g.fake.peek(fg.PID, fg.WRITER, 10) == before, "sampler jump still in the game"


# == UI (B1, B2, B6, B8, B9) ==============================================================
def test_tc60_my_cheats_button_disabled_when_disconnected(bwin, bgame):
    add_and_select(bwin, health_cheat(bgame))
    assert bwin.cheats.build_btn.isEnabled()
    bwin.ctl.handle_lost()
    bwin.cheats.table.selectRow(0)
    assert not bwin.cheats.build_btn.isEnabled()


def test_tc61_find_button_needs_a_row(bwin):
    assert not bwin.find.build_btn.isEnabled()
    assert bwin.find.build_btn.text() == "Make it work..."


def test_tc62_effects_and_defaults(bwin, bgame, bh):
    d = bh.make(address=bgame.health_addr, vtype=FLOAT, name="Health")
    assert [btn.text() for btn in d.effect_btns.values()] == [e.value for e in Effect]
    assert d.effect() is Effect.NEVER_DOWN and not d.only_me.isChecked()
    assert not d.full.isVisibleTo(d) and d.start_btn.text() == "Start"


def test_bug02_not_restored_is_never_called_unchanged(bwin, bgame, bh, bclock):
    add_and_select(bwin, health_cheat(bgame))
    bclock.events.extend([lambda: setattr(bgame.fake, "fail_writes", True)])
    bwin.cheats.build_btn.click()
    d = bh.last
    bgame.fake.fail_writes = False
    assert bgame.fake.peek(fg.PID, fg.WRITER, 1) == b"\xe9", "the fix is still in the game"
    assert "unchanged" not in d.hint.text(), d.hint.text()
    assert b.RESTART in d.hint.text()


@pytest.mark.xfail(
    reason="BUG-03: Always full uses the value from when the dialog opened, not when Start "
    "was pressed",
    strict=True,
)
def test_bug03_full_is_read_at_start(bwin, bgame, bh, bclock):
    add_and_select(bwin, health_cheat(bgame))
    bgame.set_health(80.0)  # hurt when opening the dialog

    def fill_up_then_start(d):
        d.effect_btns[Effect.ALWAYS_FULL].setChecked(True)
        assert d.hint.text() == hints.BUILD_FILL_UP
        bgame.set_health(100.0)  # does what the hint says, doesn't type anything

    bh.setup = fill_up_then_start
    bclock.events.extend([lambda: bgame.hit(bgame.player, 10.0)])
    bwin.cheats.build_btn.click()
    made = bh.last.result_cheat
    assert made is not None
    assert made.value == 100.0, f"Full is {made.value}"


@pytest.mark.xfail(
    reason="BUG-08: Full is prefilled with 6 significant digits ('1.23457e+06')", strict=True
)
def test_bug08_full_prefill_keeps_the_decimal_exactly(bwin, bgame, bh):
    bgame.set_health(1234567.0)
    d = bh.make(address=bgame.health_addr, vtype=FLOAT, name="Gold")
    assert float(d.full.text()) == 1234567.0, d.full.text()


@pytest.mark.xfail(
    reason="BUG-09: no action=build_result log line when a build ends without a cheat",
    strict=True,
)
def test_bug09_build_result_logged_when_nothing_worked(bwin, bgame, bh, caplog):
    caplog.set_level(logging.INFO)
    add_and_select(bwin, health_cheat(bgame))
    bwin.cheats.build_btn.click()
    assert bh.last.hint.text() == hints.BUILD_NONE_WORKED
    assert "action=build_result" in caplog.text


def test_tc63_fail_path_in_dialog_restores_and_continues(bwin, bgame, bh, bclock):
    """Effect broken: Fix 1 fails, the game is put back, the build ends 'none worked'."""
    add_and_select(bwin, health_cheat(bgame))
    before = _module_code(bgame)
    bclock.events.extend([lambda: bgame.hit(bgame.player, 60.0), lambda: bgame.set_health(50.0)])
    bwin.cheats.build_btn.click()
    d = bh.last
    assert _module_code(bgame) == before and notes.current_note() is None
    texts = [d.advanced.item(i).text() for i in range(d.advanced.count())]
    assert any("fail" in t and "went down" in t for t in texts), texts
    assert d.result_cheat is None


def test_tc64_close_after_pass_before_finalize_is_not_possible_in_sync(bwin, bgame, bh, bclock):
    """Pass -> Done: source row replaced, nothing of ours registered for Disconnect."""
    src = health_cheat(bgame)
    add_and_select(bwin, src)
    bclock.events.extend([lambda: bgame.hit(bgame.player, 50.0)])
    bwin.cheats.build_btn.click()
    d = bh.last
    assert d.result_cheat is not None and src not in bwin.ctl.cheat_file.cheats
    assert d._close_builder not in bwin.ctl.before_disconnect
    d.reject()  # Close after Done: the cheat stays in
    assert bgame.fake.peek(fg.PID, fg.WRITER, 1) == b"\xe9"

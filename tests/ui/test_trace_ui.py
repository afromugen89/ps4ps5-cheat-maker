"""Find the real one behind it in the UI (spec-trace AC-T1, AC-T7..T10), with a sync
TaskRunner. The game is tests/tracegame.py; the "player" changes the number when the
dialog asks (its progress signal), and time is a fake clock."""

from __future__ import annotations

import pytest

from cheatmaker.core import tracer as tr
from cheatmaker.core.guide import Action
from cheatmaker.core.real_check import Verdict
from cheatmaker.core.trace_way import Kind, Step, Way
from cheatmaker.core.values import ValueType
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from cheatmaker.ui.trace_dialog import TraceDialog, details, result_text
from tests.asm import ins, mem, rip
from tests.tracegame import TraceGame
from tests.unit.test_tracer import Clock

F, I32 = ValueType.FLOAT, ValueType.INT32


class Game:
    """HUD (whole number) = cut(real x 100), real a decimal in a heap object."""

    def __init__(self) -> None:
        self.g = TraceGame(connect=False)
        g = self.g
        self.obj, self.hud = g.alloc(0x40), g.alloc(0x40)
        k100 = g.const(F, 100.0)
        g.set(self.obj + 0x10, F, 0.87)
        g.routine(
            ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
            ins("MULSS_XMM_XMMM32", "xmm0", rip(k100)),
            ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
            ins("MOV_RM32_R32", mem("rdi"), "eax"),
            regs={"rbx": self.obj, "rdi": self.hud},
        )
        self.changes = [0.5, 0.25, 0.75]

    def change(self) -> None:
        if self.changes:
            self.g.set(self.obj + 0x10, F, self.changes.pop(0))


@pytest.fixture
def game():
    return Game()


@pytest.fixture
def win(qtbot, game):
    ctl = Controller(lambda ip, port: game.g.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("1.2.3.4")
    w.connect_view.connect_btn.click()
    assert ctl.session.game is not None
    yield w
    ctl.shutdown()


class Harness:
    """Opens the dialog the way MainWindow does, but presses Start instead of exec()."""

    def __init__(self, w: MainWindow, game: Game) -> None:
        self.w, self.game, self.clock = w, game, Clock()
        self.dialogs: list[TraceDialog] = []
        self.notices = 0
        self.hide_notice = False
        self.after_start = lambda d: None
        w.make_trace_dialog = self.make

    def make(self, **kw) -> TraceDialog:
        d = TraceDialog(self.w.ctl, parent=self.w, sleep=self.clock.sleep, clock=self.clock, **kw)
        d.notice = self._notice
        d._signals.progress.connect(
            lambda _f, stage, _n: stage is tr.Stage.CHANGE and self.game.change()
        )
        d.exec = lambda: (d.do_start(), self.after_start(d), 0)[-1]
        self.dialogs.append(d)
        return d

    def _notice(self) -> bool:
        self.notices += 1
        return self.hide_notice


def add_row(w: MainWindow, addr: int, vt: ValueType) -> None:
    f = w.find
    f.add_addr.setText(f"{addr:#x}")
    f.add_type.setCurrentIndex(f.add_type.findData(vt))
    f.add_btn.click()


def test_button_sits_under_advanced_and_needs_one_row(win, game):
    f = win.find
    assert not hints.TRACE_TESTED
    assert f.trace_btn.text() == "Find the real one behind it (being tested)"
    assert f.advanced.isAncestorOf(f.trace_btn)
    assert not f.trace_btn.isEnabled() and f.trace_btn.toolTip() == "Pick one row in the list."
    add_row(win, game.hud, I32)
    assert f.trace_btn.isEnabled() and f.trace_btn.toolTip() == hints.TRACE_BUTTON_TIP
    win.ctl.tasks._running = 1  # Lock it / Make it work / Find it next time collecting
    win.ctl.tasks.busy_changed.emit(True)
    assert not f.trace_btn.isEnabled()
    win.ctl.tasks._running = 0
    win.ctl.tasks.busy_changed.emit(False)
    assert f.trace_btn.isEnabled()
    assert f._action_widget(Action.TRACE) is f.trace_btn


def test_trace_adds_the_real_value_selected(win, game):
    h = Harness(win, game)
    f = win.find
    add_row(win, game.hud, I32)
    f.trace_btn.click()
    [d] = h.dialogs
    r = d.result
    assert r is not None and r.outcome is tr.Outcome.FOUND, r.reason
    assert r.address == game.obj + 0x10 and r.vtype is F
    assert f.manual[game.obj + 0x10] is F
    assert f.selected().address == game.obj + 0x10
    assert f.hint.text() == (
        "Found the value the game uses: 0.5, a decimal; the screen shows it x100."
    )
    assert d.cancel_btn.text() == "Close" and not d.start_btn.isEnabled()
    adv = [d.advanced.item(i).text() for i in range(d.advanced.count())]
    assert adv[0].startswith(f"Hop 1: watched {game.hud:#x} (4 bytes)")
    assert any("worked out from the registers" in line for line in adv)
    assert any("Write test: real" in line for line in adv)


def test_notice_once_per_run_and_dont_show_again(win, game):
    h = Harness(win, game)
    add_row(win, game.hud, I32)
    win.find.trace_btn.click()
    win.find._select_row(0)
    win.find.trace_btn.click()
    assert h.notices == 1 and win.ctl.trace_notice_shown
    win.ctl.trace_notice_shown = False
    h.hide_notice = True
    win.find.trace_btn.click()
    assert h.notices == 2 and Settings.load().trace_notice_hidden
    win.ctl.trace_notice_shown = False
    win.find.trace_btn.click()
    assert h.notices == 2


def test_freeze_on_the_copy_is_paused_and_resumed(win, game):
    h = Harness(win, game)
    f = win.find
    add_row(win, game.hud, I32)
    f.freeze_btn.click()
    key = f"find:{game.hud}"
    assert key in win.ctl.freezer.keys
    seen = []
    h.after_start = lambda d: seen.append(key in win.ctl.freezer.keys)
    released = []
    real_release = TraceDialog.release

    def spy(self, addr, size):
        resume = real_release(self, addr, size)
        released.append(key in win.ctl.freezer.keys)
        return resume

    TraceDialog.release = spy
    try:
        f._select_row(0)
        f.trace_btn.click()
    finally:
        TraceDialog.release = real_release
    assert released == [False], "paused while watched"
    assert seen == [True] and game.hud in win.ctl.frozen_addresses, "resumed after"


def test_cancel_before_the_watch_puts_everything_back(win, game):
    h = Harness(win, game)
    add_row(win, game.hud, I32)
    orig = h.make

    def make(**kw):
        d = orig(**kw)
        d._signals.progress.connect(
            lambda _f, stage, _n: stage is tr.Stage.WATCH and d._cancel and d._cancel.set()
        )
        return d

    win.make_trace_dialog = make
    win.find.trace_btn.click()
    d = h.dialogs[-1]
    assert d.result.outcome is tr.Outcome.CANCELLED
    assert d.hint.text() == hints.TRACE_CANCELLED
    assert game.g.fake.writes == []
    assert d._stop_for_disconnect not in win.ctl.before_disconnect


def wrong_regs(game: Game) -> int:
    """A float copy whose trap registers point at a decoy: no address passes."""
    g = game.g
    obj, hud, decoy = g.alloc(0x40), g.alloc(0x80), g.alloc(0x40)
    g.set(decoy + 0x10, F, 12.0)
    g.set(obj + 0x10, F, 87.0)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"),
        regs={"rbx": obj, "rdi": hud},
        trap_regs={"rbx": decoy},
    )
    return hud + 0x40


def test_search_for_it_that_way(win, game):
    h = Harness(win, game)
    hud = wrong_regs(game)
    add_row(win, hud, F)
    win.find.trace_btn.click()
    [d] = h.dialogs
    assert d.result.outcome is tr.Outcome.SEARCH_THAT_WAY
    assert d.hint.text() == (
        "The game keeps it as decimal (4 bytes), the screen shows it as is. Search for it that way?"
    )
    assert not d.search_btn.isHidden()
    d.search_btn.click()
    f = win.find
    assert f.advanced.isChecked() and f.vtype.currentData() is F
    assert f.number.text() == "87"


R = tr.TraceResult


@pytest.mark.parametrize(
    ("result", "text"),
    [
        (R(tr.Outcome.ALREADY_REAL, 1), "This is already the value the game uses."),
        (
            R(tr.Outcome.NO_WRITER, 1),
            "Nothing wrote to it. Make the number change while it watches, then try again.",
        ),
        (R(tr.Outcome.CANT_WATCH, 1), "This address can't be watched."),
        (R(tr.Outcome.NOT_FOLLOWED, 1), hints.TRACE_NOT_FOLLOWED),
        (R(tr.Outcome.SEARCH_THAT_WAY, 1), hints.TRACE_NOT_FOLLOWED),
        (R(tr.Outcome.SCRAMBLED, 1), "The game scrambles this value, which isn't supported yet."),
        (R(tr.Outcome.UNCONFIRMED, 1), "Change the number in the game so the app can check."),
        (R(tr.Outcome.LOST, 1), "Lost connection to the PS5."),
        (
            R(tr.Outcome.LOST, 1, not_restored=(0x20,)),
            "Lost connection to the PS5. " + hints.TRACE_NOT_RESTORED.format(addrs="0x20"),
        ),
        (R(tr.Outcome.TOO_FAR, 1), "Followed it 0 steps without reaching the real value."),
        (
            R(tr.Outcome.TOO_FAR, 1, address=5),
            "Followed it 0 steps without reaching the real value. " + hints.TRACE_NOT_SURE,
        ),
        (
            R(
                tr.Outcome.FOUND,
                1,
                5,
                ValueType.INT64,
                Way(ValueType.INT64, ValueType.INT64),
                value=51,
            ),
            "Found the value the game uses: 51, an 8-byte whole number; the screen shows it as is.",
        ),
        (R(tr.Outcome.FOUND, 1, 5), "Found the value the game uses."),
    ],
)
def test_result_text(result, text):
    assert result_text(result) == text


def test_details_name_every_part_of_a_hop():
    from cheatmaker.core import trace_flow as tf
    from cheatmaker.core.locker import Writer

    way = Way(F, I32, (Step(Kind.TO_WHOLE, I32, rounding="cut"),))
    hop = tr.Hop(1, tr.rc.Target(0x100, I32), span=(0x100, 4), hits=3)
    path = tf.Path(tf.End.SOURCE, way, followed=("mov [rdi],eax", "cvttss2si eax,xmm0"))
    hop.writers.append(tr.WriterPath(Writer(0x400000, b"\x89\x07", "mov [rdi],eax", 3), path))
    bad = tf.Candidate(0x200, "replay", "[rbx+30h]+10h")
    hop.tried.append((bad, "held 12, expected 87"))
    hop.source = tf.Candidate(0x300, "regs", "rbx+10h")
    hop.way, hop.verdict, hop.stop = way, Verdict.COPY, "a copy: following it"
    lines = details(R(tr.Outcome.TOO_FAR, 0x100, hops=(hop,), not_restored=(0x300,)))
    assert lines[0] == "Hop 1: watched 0x100 (4 bytes), 3 writes"
    assert "Writer 0x400000: mov [rdi],eax (3 writes)" in lines[1]
    assert "Followed: mov [rdi],eax <- cvttss2si eax,xmm0 -> source" in lines[2]
    assert "0x200 (replaying the loads: [rbx+30h]+10h): held 12, expected 87" in lines[3]
    assert "Source 0x300 worked out from the registers: rbx+10h" in lines[4]
    assert "Write test: copy" in lines[5] and "Stopped: a copy" in lines[6]
    assert "0x300" in lines[7]

"""Make it work in the UI (spec-cheat-builder B1, B8, notice, questions, closing), with a
sync TaskRunner. The game is tests/fakegame.py (writers and routines run on minicpu);
time is the fake clock from the builder flow tests, whose sleeps let the game act."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import build_notes as notes
from cheatmaker.core import builder as bd
from cheatmaker.core import real_check
from cheatmaker.core import real_check as rc
from cheatmaker.core.cave import UNSUPPORTED_TYPE, Effect
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.values import ValueType
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui.build_dialog import MakeItWorkDialog
from cheatmaker.ui.cheats_view import CHECK_AFTER_RESTART, keeps_text
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests import fakegame as fg
from tests.unit.test_builder_flow import Clock


@pytest.fixture
def game():
    g = fg.build(capture_ticks=False)
    g.fake.write_hits[g.health_addr] = [WatchHit(fg.WRITER + 10)] * 3
    return g


@pytest.fixture
def win(qtbot, game):
    ctl = Controller(lambda ip, port: game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("1.2.3.4")
    w.connect_view.connect_btn.click()
    assert ctl.session.game is not None
    yield w
    ctl.shutdown()


@pytest.fixture
def clock():
    return Clock()


class Harness:
    """Opens the dialog the way MainWindow does, but runs Start instead of exec()."""

    def __init__(self, w: MainWindow, clock: Clock) -> None:
        self.w, self.clock = w, clock
        self.dialogs: list[MakeItWorkDialog] = []
        self.notices = 0
        self.hide_notice = False
        self.answers: dict[bd.Question, bd.Answer] = {}
        self.asked: list[bd.Question] = []
        self.setup = lambda d: None  # pick an effect, Only me... before Start
        w.make_build_dialog = self.make

    def make(self, **kw) -> MakeItWorkDialog:
        d = MakeItWorkDialog(
            self.w.ctl, parent=self.w, sleep=self.clock.sleep, clock=self.clock, **kw
        )
        d.notice = self._notice
        d.ask = self._ask
        d.exec = lambda: (self.setup(d), d.do_start(), 0)[-1]
        self.dialogs.append(d)
        return d

    def _notice(self) -> bool:
        self.notices += 1
        return self.hide_notice

    def _ask(self, q: bd.Question) -> bd.Answer:
        self.asked.append(q)
        return self.answers.get(q, bd.Answer.NO)

    @property
    def last(self) -> MakeItWorkDialog:
        return self.dialogs[-1]


@pytest.fixture
def h(win, clock):
    return Harness(win, clock)


def health_cheat(g: fg.Game, vtype: ValueType = ValueType.FLOAT, n: int = 1) -> Cheat:
    locs = [
        Location(
            LocationKind.SESSION_ADDRESS,
            g.health_addr + 0x10 * k,
            vtype.encode(100),
            vtype.encode(100),
        )
        for k in range(n)
    ]
    return Cheat("Health", locs, value_type=vtype)


def add_and_select(w: MainWindow, cheat: Cheat) -> None:
    w.ctl.ensure_file().cheats.append(cheat)
    w.ctl.cheats_changed.emit()
    w.cheats.table.selectRow(w.ctl.cheat_file.cheats.index(cheat))


def module(g: fg.Game) -> bytes:
    return g.fake.peek(fg.PID, fg.BASE, fg.CODE_SIZE) + g.fake.peek(fg.PID, fg.DATA, fg.DATA_SIZE)


# -- My cheats ---------------------------------------------------------------------------
def test_my_cheats_make_it_work_replaces_the_row(win, game, h, clock):
    first = Cheat(
        "Gold", [Location(LocationKind.SESSION_ADDRESS, fg.HEAP + 8, b"\1\0\0\0", b"\0\0\0\0")]
    )
    win.ctl.ensure_file().cheats.append(first)
    source = health_cheat(game)
    add_and_select(win, source)
    assert win.cheats.build_btn.isEnabled() and win.cheats.build_btn.text() == "Make it work..."
    h.hide_notice = True
    clock.events.extend([lambda: game.hit(game.player, 40.0)])
    win.cheats.build_btn.click()
    d = h.last
    cheats = win.ctl.cheat_file.cheats
    assert d.result_cheat is not None and cheats[1] is d.result_cheat and source not in cheats
    assert cheats[0] is first
    made = d.result_cheat
    assert made.name == "Health: Never goes down" and made.enabled
    assert keeps_text(made) == "Yes"
    assert d.hint.text() == (
        hints.BUILD_DONE.format(name=made.name, after="Yes") + " " + hints.BUILD_TRY_ONLY_ME
    )
    assert win.status.text() == d.hint.text()
    assert game.health() == 100.0
    texts = [d.advanced.item(i).text() for i in range(d.advanced.count())]
    assert texts[0].startswith("Writer 0x400100: vmovss") and texts[-1].startswith("Fix 1:")
    assert not d.advanced.isVisible() or d.show_advanced.isChecked()
    assert d.cancel_btn.text() == "Close" and not d.start_btn.isEnabled()
    assert h.last._close_builder not in win.ctl.before_disconnect, (
        "nothing of ours left to put back"
    )
    assert h.notices == 1 and win.ctl.settings.build_notice_hidden
    assert Settings.load().build_notice_hidden, "saved"


def test_notice_once_per_run_unless_hidden(win, game, h, clock):
    add_and_select(win, health_cheat(game))
    win.cheats.build_btn.click()  # no hit: nothing works, but the notice was shown
    assert h.notices == 1 and not win.ctl.settings.build_notice_hidden
    win.cheats.build_btn.click()
    assert h.notices == 1, "once per run"


@pytest.mark.parametrize("kind", ["combined", "anchored", "code"])
def test_button_needs_a_single_value(win, game, kind):
    c = health_cheat(game, n=2 if kind == "combined" else 1)
    if kind == "anchored":
        c.anchor = ("player", "health")
    if kind == "code":
        c.locations = [Location(LocationKind.CODE_PATCH, 0x100, b"\x90\x90", b"\x89\x08")]
        c.value_type = None
    add_and_select(win, c)
    assert not win.cheats.build_btn.isEnabled()
    assert win.cheats.build_btn.toolTip() == hints.BUILD_PICK_SINGLE
    win.cheats.do_build(c)
    assert win.status.text() == hints.BUILD_PICK_SINGLE


def test_unsupported_type_says_use_lock_it(win, game, h):
    c = health_cheat(game, ValueType.DOUBLE)
    add_and_select(win, c)
    win.cheats.build_btn.click()
    assert win.status.text() == UNSUPPORTED_TYPE and h.dialogs == []
    d = MakeItWorkDialog(win.ctl, address=game.health_addr, vtype=ValueType.INT16, name="x")
    assert d.hint.text() == UNSUPPORTED_TYPE and not d.start_btn.isEnabled()


def test_nothing_worked_leaves_the_game_unchanged(win, game, h):
    add_and_select(win, health_cheat(game))
    before = module(game)
    win.cheats.build_btn.click()
    d = h.last
    assert d.hint.text() == hints.BUILD_NONE_WORKED and d.result_cheat is None
    assert module(game) == before and len(win.ctl.cheat_file.cheats) == 1
    assert d.start_btn.isEnabled(), "can try again"
    texts = [d.advanced.item(i).text() for i in range(d.advanced.count())]
    assert any("no hit" in t for t in texts)


def test_nothing_changed_it(win, game, h):
    game.fake.write_hits[game.health_addr] = []
    add_and_select(win, health_cheat(game))
    win.cheats.build_btn.click()
    assert (
        h.last.hint.text() == bd.NOTHING_CHANGED
        and h.last._close_builder not in win.ctl.before_disconnect
    )


def test_always_full_field(win, game, h, clock):
    add_and_select(win, health_cheat(game))
    game.set_health(80.0)

    def pick(d: MakeItWorkDialog) -> None:
        d.effect_btns[Effect.ALWAYS_FULL].setChecked(True)
        assert d.full.isVisibleTo(d) and d.full.text() == "80"
        assert d.hint.text() == hints.BUILD_FILL_UP
        d.full.setText("250")

    h.setup = pick
    clock.events.extend([lambda: game.hit(game.player, 10.0)])
    win.cheats.build_btn.click()
    made = h.last.result_cheat
    assert made is not None and made.value == 250.0 and game.health() == 250.0


def test_always_full_needs_a_number(win, game, h):
    add_and_select(win, health_cheat(game))

    def pick(d):
        d.effect_btns[Effect.ALWAYS_FULL].setChecked(True)
        d.full.setText("lots")

    h.setup = pick
    win.cheats.build_btn.click()
    assert h.last.hint.text() == hints.BUILD_FULL_BAD and h.last.builder is None


def test_only_me_by_field_says_check_after_restart(win, game, h, clock):
    for e in game.enemies:  # same class as the player
        game.fake.poke(fg.PID, e, struct.pack("<Q", fg.PLAYER_VT))
    add_and_select(win, health_cheat(game))
    h.setup = lambda d: d.only_me.setChecked(True)
    fight = []
    for _ in range(3):
        fight.append(lambda: game.hit(game.player, game.health()))
        fight += [lambda e=e: game.hit(e, game.value(e)) for e in game.enemies]
    clock.events.extend(fight)
    test = [lambda: game.hit(game.player, 50.0)] * 3
    # collect polls 80 times, then waits once more before reading the table
    clock.events.extend([None] * (81 - len(fight)) + test)
    h.answers[bd.Question.ENEMIES] = bd.Answer.YES
    win.cheats.build_btn.click()
    d = h.last
    assert h.asked == [bd.Question.ENEMIES], d.hint.text()
    made = d.result_cheat
    assert made is not None and made.name == "Health: Never goes down (only me)"
    assert keeps_text(made) == CHECK_AFTER_RESTART
    assert hints.BUILD_TRY_ONLY_ME not in d.hint.text()
    row = win.ctl.cheat_file.cheats.index(made)
    assert win.cheats.table.item(row, 3).text() == CHECK_AFTER_RESTART


def test_switched_off_fix_asks_whether_you_got_hit(win, game, h):
    site = fg.BASE + 0x500
    game.fake.poke(fg.PID, site, bytes.fromhex("898C88B0020000"))  # no jump fits
    addr = game.player + fg.HP32
    game.fake.write_hits[addr] = [WatchHit(site + 7)] * 3
    c = Cheat("Lives", [Location(LocationKind.SESSION_ADDRESS, addr, b"d\0\0\0", b"d\0\0\0")])
    c.value_type = ValueType.INT32
    add_and_select(win, c)
    h.answers[bd.Question.GOT_HIT] = bd.Answer.YES
    win.cheats.build_btn.click()
    assert h.asked == [bd.Question.GOT_HIT]
    [line] = h.last.result_cheat.locations
    assert line.on == b"\x90" * 7


def test_cancel_during_a_test_puts_everything_back(win, game, h, clock):
    add_and_select(win, health_cheat(game))
    before = module(game)
    clock.events.extend([None, lambda: h.last.reject()])
    win.cheats.build_btn.click()
    d = h.last
    assert module(game) == before and notes.current_note() is None
    assert (
        d.hint.text() == hints.BUILD_CANCELLED
        and h.last._close_builder not in win.ctl.before_disconnect
    )
    assert d.result_cheat is None


def test_disconnect_during_a_test_puts_everything_back_first(win, game, h, clock):
    add_and_select(win, health_cheat(game))
    before = module(game)
    clock.events.extend([None, win.ctl.run_before_disconnect])
    win.cheats.build_btn.click()
    assert module(game) == before and h.last.hint.text() == hints.BUILD_CANCELLED


# -- Find -------------------------------------------------------------------------------
def test_dialog_checks_the_group_first_and_reports_verdicts(win, game, h, clock):
    other = rc.Target(game.player + 0x40, ValueType.FLOAT)
    got = []
    d = h.make(
        address=game.health_addr,
        vtype=ValueType.FLOAT,
        name="Health",
        group=[rc.Target(game.health_addr, ValueType.FLOAT), other],
        on_real_check=got.append,
    )
    clock.events.extend([None] * 12 + [lambda: game.hit(game.player, 30.0)])
    d.exec()
    assert got and (game.health_addr, ValueType.FLOAT) in got[0]
    assert d.result_cheat is not None, d.hint.text()
    assert game.health() == 100.0
    assert win.ctl.cheat_file.cheats[-1] is d.result_cheat, "from Find: added"


def test_find_button_passes_an_unchecked_group_and_merges_verdicts(qtbot):
    from tests.conftest import PID, build_fake
    from tests.ui.test_health_finding import COPIES, HP, full_and_three_hits

    fake = build_fake()
    for a in COPIES:
        fake.add_region(PID, "[anon:hud]", a, 0x10)
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("1.2.3.4")
    w.connect_view.connect_btn.click()
    real_check.SETTLE_S, settle = 0.0, real_check.SETTLE_S
    try:
        full_and_three_hits(w, fake)
        f = w.find
        f._select_row(next(i for i, m in enumerate(f.rows) if m.address == HP))
        assert f.build_btn.isEnabled()
        opened = []
        w.make_build_dialog = lambda **kw: (
            opened.append(kw) or type("D", (), {"exec": lambda s: 0})()
        )
        f.build_btn.click()
        kw = opened[-1]
        assert kw["address"] == HP and kw["vtype"] == f.groups[0].head.vtype
        assert {t.address for t in kw["group"]} >= {HP, *COPIES}, "the row's group, unchecked"
        vt = kw["vtype"]
        kw["on_real_check"]({(HP, vt): real_check.Result(real_check.Verdict.REAL, 4)})
        assert f.verdicts[(HP, vt)].verdict is real_check.Verdict.REAL
        f._select_row(next(i for i, m in enumerate(f.rows) if m.address == HP))
        f.build_btn.click()
        assert opened[-1]["group"] == [], "checked already: not again"
    finally:
        real_check.SETTLE_S = settle
        ctl.shutdown()


# -- connect ----------------------------------------------------------------------------
def test_connect_reports_a_fix_left_open_last_time(qtbot, game):
    notes.open_note(notes.OpenNote(fg.TITLE, fg.VERSION, fg.PID + 1, 0x100, "hook", 3, (), ()))
    ctl = Controller(lambda ip, port: game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("1.2.3.4")
    w.connect_view.connect_btn.click()
    assert w.status.text() == (
        "Fix 3 crashed the game last time. Your game is unchanged after a restart."
    )
    assert notes.is_crashed(fg.TITLE, fg.VERSION, 0x100, "hook")
    ctl.shutdown()


def test_keeps_text():
    c = Cheat("x", [Location(LocationKind.CODE_PATCH, 1, b"\x90", b"\x89")])
    assert keeps_text(c) == "Yes"
    c.check_after_restart = True
    assert keeps_text(c) == CHECK_AFTER_RESTART


def test_notice_not_shown_when_hidden_in_settings(win, game, h):
    win.ctl.settings.build_notice_hidden = True
    add_and_select(win, health_cheat(game))
    win.cheats.build_btn.click()
    assert h.notices == 0

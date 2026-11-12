"""Make it work with linked values in the UI (spec-linked-values L1, L11, L12, L13, L15),
with a sync TaskRunner. The game is tests/fakegame.py with ``linked=True``: health and
its fraction in another object, a percent worked out from the fraction and two HUD
copies of health. Time is the fake clock; every sleep is one game tick, and the game
takes a hit every 20 ticks."""

from __future__ import annotations

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import build_notes as notes
from cheatmaker.core import builder as bd
from cheatmaker.core import linked as lk
from cheatmaker.core import real_check
from cheatmaker.core.cave import Effect
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.values import ValueType
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests import fakegame as fg
from tests.ui.test_build_ui import Harness, add_and_select
from tests.unit.test_builder_flow import Clock
from tests.unit.test_builder_places import pool_of

F = ValueType.FLOAT
V = real_check.Verdict


@pytest.fixture
def game():
    g = fg.build(capture_ticks=False, linked=True)
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


@pytest.fixture
def h(win, clock):
    return Harness(win, clock)


def no_dialog(opened: list[dict]):
    """A make_build_dialog that only notes how it was asked to open."""
    return lambda **kw: opened.append(kw) or type("D", (), {"exec": lambda s: 0})()


def module(g: fg.Game) -> bytes:
    return g.fake.peek(fg.PID, fg.BASE, fg.CODE_SIZE) + g.fake.peek(fg.PID, fg.DATA, fg.DATA_SIZE)


def play(clock, g: fg.Game, every: int = 20, then=None) -> None:
    """The game takes a hit every ``every`` ticks for a long while; ``then`` runs each
    tick too (a Cancel at the right moment)."""

    def tick(i: int) -> None:
        if then is not None:
            then()
        if i % every == every - 1:
            g.damage(10.0)

    clock.events.extend([lambda i=i: tick(i) for i in range(2000)])


def health_row(g: fg.Game, pool: lk.LinkedGroup | None) -> Cheat:
    """A My cheats row made from Health's Find row (its group in memory)."""
    data = F.encode(g.health())
    c = Cheat(
        "Health", [Location(LocationKind.SESSION_ADDRESS, g.health_addr, data, data)], value_type=F
    )
    c.linked = pool
    return c


def texts(d) -> list[str]:
    return [d.advanced.item(i).text() for i in range(d.advanced.count())]


def test_simple_value_shows_no_extra_step(win, game, h, clock):
    """C22d / C22e: everyone keeps up (health and its HUD copies): one test and Done."""
    pool = lk.LinkedGroup(
        (
            lk.PoolMember(game.health_addr, F, 1.0, True, None),
            lk.PoolMember(fg.HUD_COPIES[0], F, 1.0, False, None),
        )
    )
    add_and_select(win, health_row(game, pool))
    seen: list[str] = []
    play(clock, game, then=lambda: seen.append(h.last.hint.text()))
    win.cheats.build_btn.click()
    d = h.last
    made = d.result_cheat
    assert made is not None and d.builder.place == 1 and len(d.builder.tried) == 1
    assert d.hint.text().startswith(hints.BUILD_DONE.format(name=made.name, after="Yes"))
    assert h.asked == [] and not any("second place" in t for t in seen)
    pool_lines = [t for t in texts(d) if t.startswith("Linked ")]
    assert pool_lines == [f"Linked {fg.HUD_COPIES[0]:#x} (FLOAT, x1): not checked, kept up"]


def test_two_places_in_the_dialog(win, game, h, clock):
    add_and_select(win, health_row(game, pool_of(game)))
    seen: list[str] = []
    play(clock, game, then=lambda: seen.append(h.last.hint.text()))
    win.cheats.build_btn.click()
    d = h.last
    made = d.result_cheat
    assert made is not None, d.hint.text()
    assert made.places == 2 and made.name == "Health: Never goes down"
    assert win.ctl.cheat_file.cheats == [made], "it replaced the source row"
    assert d.hint.text().startswith("Done. Health is held in 2 places by one cheat.")
    assert hints.BUILD_LINKED_BEHIND.format(name="Health") in seen
    assert "Testing fix 1 of 1 for place 2: keep playing and take a hit." in seen
    lines = texts(d)
    assert f"Linked {game.fraction_addr:#x} (FLOAT, x0.01): kept, fell behind - fixed" in lines
    assert f"Linked {fg.PERCENT:#x} (FLOAT, x1): copy, kept up - copy: never touched" in lines
    assert any(t.startswith(f"Linked {fg.HUD_COPIES[0]:#x}") for t in lines)
    assert notes.current_note() is None and notes.kept_notes() == []
    assert d._close_builder not in win.ctl.before_disconnect


def test_place_hint_names_the_place(win, game, h, clock):
    add_and_select(win, health_row(game, pool_of(game)))
    seen: list[str] = []
    real_watch = bd.Builder.watch

    def watch(self, cancel=None):
        seen.append(h.last.hint.text())
        return real_watch(self, cancel)

    bd.Builder.watch = watch
    try:
        play(clock, game)
        win.cheats.build_btn.click()
    finally:
        bd.Builder.watch = real_watch
    assert seen[-1] == "Place 2 of 2: play and make it change (take a hit) within 30 seconds."


def test_cancel_during_place_2_puts_both_back(win, game, h, clock):
    add_and_select(win, health_row(game, pool_of(game)))
    before = module(game)

    def cancel_in_place_2() -> None:
        b = h.last.builder
        if b is not None and b.place == 2 and b._installed is not None:
            h.last.reject()

    play(clock, game, then=cancel_in_place_2)
    win.cheats.build_btn.click()
    d = h.last
    assert d.result_cheat is None and d.builder.place == 2
    assert d.hint.text() == hints.BUILD_CANCELLED == "Stopped. Your game is unchanged."
    assert module(game) == before
    assert notes.current_note() is None and notes.kept_notes() == []
    assert d._close_builder not in win.ctl.before_disconnect


def test_place_2_that_cant_be_fixed_is_held_by_a_freeze(win, game, h, clock):
    game.fake.write_hits[game.fraction_addr] = []  # nothing seen writing the fraction
    add_and_select(win, health_row(game, pool_of(game)))
    play(clock, game)
    try:
        win.cheats.build_btn.click()
        d = h.last
        made = d.result_cheat
        assert made is not None and made.combined and made.freeze and made.enabled
        assert d.hint.text() == hints.BUILD_LINKED_FREEZE
        assert id(made) in win.ctl.live.active
        assert any(t.startswith("Place 2: Nothing changed it") for t in texts(d))
    finally:
        win.ctl.freezer.clear()


def test_half_damage_place_2_that_cant_be_fixed_warns(win, game, h, clock):
    game.fake.write_hits[game.fraction_addr] = []
    add_and_select(win, health_row(game, pool_of(game)))
    h.setup = lambda d: d.effect_btns[Effect.HALF].setChecked(True)
    play(clock, game)
    win.cheats.build_btn.click()
    d = h.last
    assert d.result_cheat is not None and not d.result_cheat.combined
    assert d.hint.text() == hints.BUILD_LINKED_HALF.format(name="Health")


def test_only_a_row_made_from_a_group_has_a_pool(win, game, h):
    opened: list[dict] = []
    win.make_build_dialog = no_dialog(opened)
    pool = pool_of(game)
    add_and_select(win, health_row(game, pool))
    win.cheats.build_btn.click()
    assert opened[-1]["pool"] is pool
    win.ctl.cheat_file.cheats.clear()
    add_and_select(win, health_row(game, None))
    win.cheats.build_btn.click()
    assert opened[-1]["pool"] is None


def test_find_passes_the_pool_for_every_group_and_make_remembers_it(qtbot):
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
    try:
        full_and_three_hits(w, fake)
        f = w.find
        row = next(i for i, m in enumerate(f.rows) if m.address == HP)
        f._select_row(row)
        opened = []
        w.make_build_dialog = no_dialog(opened)
        f.build_btn.click()
        vt = opened[-1]["vtype"]
        pool = opened[-1]["pool"]
        assert opened[-1]["group"], "unchecked: checked first, as before"
        assert pool is not None and pool.members[0].mark is None
        assert {m.address for m in pool.members} >= {HP, *COPIES}
        f.merge_verdicts({(HP, vt): real_check.Result(V.REAL, 3)})
        f._select_row(row)
        f.build_btn.click()
        assert opened[-1]["group"] == [], "checked already: not again"
        pool = opened[-1]["pool"]
        assert pool is not None, "the pool goes along for a checked group too"
        assert next(m for m in pool.members if m.address == HP).mark is V.REAL
        f._select_row(row)
        f.do_make()
        made = ctl.cheat_file.cheats[-1]
        assert made.linked is not None and {m.address for m in made.linked.members} >= {HP}
    finally:
        ctl.shutdown()


# -- review fixes: the partial hints, reasons in Advanced, L4 verdicts back to Find ---------------
@pytest.mark.parametrize(
    ("freeze", "reason", "want"),
    [
        (False, bd.LIMIT, "more places than the app can fix on its own"),
        (True, bd.LIMIT, hints.BUILD_LINKED_LEFT),
        (False, "Couldn't watch it: unloaded", "couldn't fix or hold"),
    ],
)
def test_partial_results_say_what_was_left(win, game, h, freeze, reason, want):
    d = h.make(address=game.health_addr, vtype=F, name="Health")
    cheat = Cheat(
        "Health: Never goes down", [Location(LocationKind.CODE_PATCH, 0x100, b"\x90", b"\xc3")]
    )
    cheat.freeze = freeze
    res = bd.BuildResult(cheat, "Yes", places=3, linked="partial", reason=reason)
    text = d._done_text(res)
    assert want in text
    if not freeze:
        assert "second place couldn't be fixed" not in text
        assert text.startswith("Health is held in 3 places")


def test_advanced_says_why_a_place_is_held(win, game, h, clock):
    game.fake.write_hits[game.fraction_addr] = []
    add_and_select(win, health_row(game, pool_of(game)))
    play(clock, game)
    try:
        win.cheats.build_btn.click()
        line = next(t for t in texts(h.last) if t.startswith(f"Linked {game.fraction_addr:#x}"))
        assert line.endswith(f"held by a freeze ({bd.NOTHING_CHANGED})"), line
    finally:
        win.ctl.freezer.clear()


def test_verdicts_of_the_linked_check_go_back_to_find(win, game, h, clock):
    got: list[dict] = []
    d = h.make(
        address=game.health_addr,
        vtype=F,
        name="Health",
        pool=pool_of(game),
        on_real_check=got.append,
    )
    play(clock, game)
    d.exec()
    assert d.result_cheat is not None
    assert got and got[-1][(fg.PERCENT, F)].verdict is V.COPY

"""Notes brought over from another game version, in the app (spec-game-update AC-U3, U5,
U8): the question, Yes / No, class learned again, "check it" / "test it" texts, gating."""

from __future__ import annotations

import dataclasses
import struct

import pytest
from PySide6.QtCore import Qt

from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.values import ValueType
from cheatmaker.settings import Settings
from cheatmaker.ui import carry as carry_ui
from cheatmaker.ui import hints
from cheatmaker.ui import main_window as mw
from cheatmaker.ui.cheats_view import COL_KEEPS, COL_ON, FOUND_AGAIN, keeps_text
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests import fakegame as fg

NEW = fg.V2.version
V2_HP = fg.V2.hp


class Console:
    def __init__(self, layout: fg.Layout = fg.V2, **kw) -> None:
        self.layout, self.kw = layout, kw
        self.game = fg.build(1, connect=False, layout=layout, **kw)

    def restart(self, run: int) -> fg.Game:
        self.game = fg.build(run, connect=False, layout=self.layout, **self.kw)
        return self.game


@pytest.fixture
def console():
    return Console()


@pytest.fixture
def asked():
    return []


@pytest.fixture
def answers():
    return []  # popped in order; True when empty


@pytest.fixture
def win(qtbot, console, monkeypatch, asked, answers):
    monkeypatch.setattr(rf, "SLOT_WAIT_S", 0.5)
    ctl = Controller(lambda ip, port: console.game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)

    def ask(text: str) -> bool:
        asked.append(text)
        return answers.pop(0) if answers else True

    w.cheats.finder.ask_confirm = ask
    w.cheats.finder.sleep = lambda _s: None
    yield w
    ctl.shutdown()


def connect(w):
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()


def tick(w, row=0):
    w.cheats.table.item(row, COL_ON).setCheckState(Qt.CheckState.Checked)


def keeps(w, row=0) -> str:
    return w.cheats.table.item(row, COL_KEEPS).text()


def path() -> pj.Project:
    return pj.load(pj.project_path(fg.TITLE, NEW))


def hooked_health() -> pj.ObjectSpec:
    """Health as a bring-over leaves it on V2: new hook, new class, "check it"."""
    g = fg.build(1, layout=fg.V2)
    cand = ah.HookCandidate(
        fg.V2.reader, fg.V2.vmovss_r, "vmovss xmm0,[r12+2B4h]", 2, "reader", "r12", V2_HP, True
    )
    hook = ah.plan_hook(g.session, cand, fg.V2.player_vt - fg.BASE)
    return pj.ObjectSpec(
        "obj1",
        "Health",
        fg.V2.player_vt - fg.BASE,
        [
            pj.Field("Health", V2_HP, ValueType.FLOAT, (0, pj.CLASS_FLOAT_LIMIT), 100.0),
            pj.Field("Health 32", fg.V2.hp32, ValueType.INT32, (0, 2**31 - 1), 100),
        ],
        [hook, pj.VtableAnchor()],
        check=True,
    )


def chain_health() -> pj.ObjectSpec:
    """Found by a memory path: its class was cleared when brought over."""
    return pj.ObjectSpec(
        "obj1",
        "Health",
        None,
        [pj.Field("Health", V2_HP, ValueType.FLOAT, (0, 10_000), 100.0)],
        [pj.ChainAnchor(fg.STATIC_PTR - fg.BASE, (0x10, V2_HP), "Health")],
        check=True,
        relearn_class=True,
    )


def save_notes(obj: pj.ObjectSpec, cheats=None) -> pj.Project:
    p = pj.Project(
        fg.TITLE,
        NEW,
        objects=[obj],
        cheats=cheats or [pj.ProjectCheat("Infinite health", "obj1", "Health", "freeze", 999.0)],
        carried_from=fg.VERSION,
    )
    pj.save(pj.project_path(fg.TITLE, NEW), p)
    return p


# -- the question (AC-U8) -----------------------------------------------------------------
def test_question_names_the_old_version_and_yes_confirms(win, console, asked):
    save_notes(hooked_health())
    connect(win)
    tracker = win.ctl.live.trackers["obj1"]
    assert tracker.address == console.game.player and not tracker.confirmed
    assert keeps(win) == hints.KEEPS_CHECK == "Found again each run (check it)"
    tick(win)
    [text] = asked
    assert text.startswith("Is this your Health? It was brought over from version 03.000.005")
    assert "shows 100.00 right now" in text and "Press Yes only if it does." in text
    assert console.game.health() == 999.0 and tracker.confirmed
    saved = path()
    assert not saved.objects[0].check and saved.carried_from == fg.VERSION
    assert keeps(win) == FOUND_AGAIN


def test_no_writes_nothing_and_points_to_the_search(win, console, answers):
    save_notes(hooked_health())
    connect(win)
    answers.append(False)
    console.game.fake.writes.clear()
    tick(win)
    hp = console.game.health_addr
    assert not any(a <= hp < a + len(d) for _p, a, d in console.game.fake.writes)
    assert console.game.health() == 100.0 and not win.cheats.cheats[0].enabled
    assert win.status.text() == (
        "OK, nothing was changed. Search for Health to find it in this version."
    )
    assert path().objects[0].check, "nothing written to the notes either"


def test_asked_again_next_run_until_yes(win, console, asked, answers):
    save_notes(hooked_health())
    connect(win)
    answers.append(False)
    tick(win)
    win.ctl.shutdown()
    win.ctl.cheat_file.cheats.clear()
    console.restart(2)
    connect(win)
    tick(win)  # Yes this time
    assert "brought over from version 03.000.005" in asked[1]
    assert console.game.health() == 999.0 and not path().objects[0].check
    win.ctl.shutdown()
    win.ctl.cheat_file.cheats.clear()
    console.restart(3)
    connect(win)
    tick(win)
    assert asked[2] == hints.CONFIRM.format(name="Health", value="100.00"), "today's question"


def test_a_pick_does_not_confirm_a_brought_over_object(win, console):
    save_notes(hooked_health())
    connect(win)
    finder = win.cheats.finder
    obj = win.ctl.project.objects[0]
    assert not finder._track(obj, console.game.player, confirmed=True).confirmed
    obj.check = False
    assert finder._track(obj, console.game.player, confirmed=True).confirmed


def test_yes_learns_the_class_again_and_brings_the_memory_search_back(qtbot, asked):
    console = Console(chain=True)
    ctl = Controller(lambda ip, port: console.game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.cheats.finder.ask_confirm = lambda text: asked.append(text) or True
    try:
        save_notes(chain_health())
        connect(w)
        assert w.ctl.live.trackers["obj1"].address == console.game.player
        tick(w)
        assert "brought over from version" in asked[0]
        obj = path().objects[0]
        assert obj.vtable == fg.V2.player_vt - fg.BASE and not obj.relearn_class
        assert obj.anchors_of(pj.VtableAnchor) and not obj.check
        assert obj.field("Health").range == (0, pj.CLASS_FLOAT_LIMIT)
        assert console.game.health() == 999.0
    finally:
        ctl.shutdown()


def test_yes_keeps_no_class_when_the_object_has_none(win, console):
    obj = chain_health()
    save_notes(obj)
    connect(win)
    tracked = win.ctl.project.objects[0]  # not found this run (no memory path here)
    console.game.fake.poke(fg.PID, console.game.player, bytes(8))  # no pointer into the module
    win.cheats.finder.carried_yes(tracked, console.game.player)
    saved = path().objects[0]
    assert saved.vtable is None and not saved.relearn_class and not saved.check
    assert not saved.anchors_of(pj.VtableAnchor)


def test_check_pending_yes_on_a_brought_over_object_confirms_it(win, console):
    save_notes(hooked_health())
    connect(win)
    c = win.cheats.cheats[0]
    c.enabled = True  # on without the question (e.g. found while turning on)
    tracker = win.ctl.live.trackers["obj1"]
    tracker.pending = console.game.player
    win.cheats.finder.check_pending()
    assert tracker.confirmed and not path().objects[0].check
    c.enabled = False


# -- combined cheats: one question, "test it" until the first tick ---------------------------
def combined_notes() -> pj.Project:
    nop = pj.ProjectPart(
        "no death", offset=fg.V2.sub_site - fg.BASE, on=b"\x90" * 6, off=fg.V2.sub_w
    )
    cave = pj.ProjectPart("room", offset=fg.CODE_SIZE - 0x700, on=b"\xc3", off=b"\0")
    god = pj.ProjectCheat(
        "God",
        parts=[
            pj.ProjectPart("health", "obj1", "Health", 999.0),
            pj.ProjectPart("health 32", "obj1", "Health 32", 999),
            nop,
            cave,
        ],
        test=True,
    )
    return save_notes(hooked_health(), cheats=[god])


def test_combined_cheat_one_question_then_test_cleared_at_first_tick(win, console, asked):
    combined_notes()
    connect(win)
    [god] = win.cheats.cheats
    assert god.carried == "test" and keeps(win) == "Brought over: test it"
    tick(win)
    [text] = asked
    assert text.startswith("It was brought over from version 03.000.005. Is this your God?")
    assert "health shows 100.00, health 32 shows 100" in text
    assert god.enabled and console.game.health() == 999.0
    assert console.game.fake.peek(fg.PID, fg.V2.sub_site, 6) == b"\x90" * 6
    assert win.status.text() == hints.CARRY_TEST_HINT
    saved = path()
    assert not saved.cheats[0].test and not saved.objects[0].check
    assert god.carried == "" and keeps(win) == FOUND_AGAIN


def test_combined_cheat_no_keeps_test_and_check(win, console, answers):
    combined_notes()
    connect(win)
    answers.append(False)
    tick(win)
    assert win.status.text() == (  # its value, not the cheat's name (God)
        "OK, nothing was changed. Search for Health to find it in this version."
    )
    saved = path()
    assert saved.cheats[0].test and saved.objects[0].check
    assert console.game.fake.peek(fg.PID, fg.V2.sub_site, 6) == fg.V2.sub_w


def test_keep_rooms_keeps_the_notes_part_rooms(win, console):
    combined_notes()
    connect(win)
    room = (fg.CODE_SIZE - 0x700, fg.CODE_SIZE - 0x700 + 1)
    assert room in win.ctl.session.reserved_rooms
    assert (fg.V2.sub_site - fg.BASE, fg.V2.sub_site - fg.BASE + 6) not in (
        win.ctl.session.reserved_rooms
    ), "a patch over the game's own code isn't a room"


# -- adopt the notes a bring-over made ------------------------------------------------------
def test_adopt_carried_saves_tracks_unconfirmed_and_shows_check(win, console):
    connect(win)
    assert win.ctl.project is None
    obj = hooked_health()
    p = pj.Project(
        fg.TITLE,
        NEW,
        objects=[obj],
        cheats=[pj.ProjectCheat("Infinite health", "obj1", "Health", "freeze", 999.0)],
        carried_from=fg.VERSION,
    )
    seen = []
    win.cheats.finder.notes_changed.connect(lambda: seen.append(1))
    win.cheats.finder.adopt_carried(p, {"obj1": console.game.player})
    assert win.ctl.project is p and seen, "saved (the learner then saves its fingerprints)"
    assert path() == p
    tracker = win.ctl.live.trackers["obj1"]
    assert tracker.address == console.game.player and not tracker.confirmed
    assert [c.name for c in win.cheats.cheats] == ["Infinite health"]
    assert keeps(win) == hints.KEEPS_CHECK


def test_adopt_carried_merges_and_skips_objects_not_found(win, console):
    connect(win)
    have = pj.ObjectSpec("obj1", "Ammo", None, [pj.Field("Ammo", 0, ValueType.INT32)])
    into = pj.Project(fg.TITLE, NEW, objects=[have])
    win.ctl.project = into
    obj = dataclasses.replace(chain_health(), id="obj2")
    obj.anchors = []
    p = pj.Project(
        fg.TITLE,
        NEW,
        objects=[obj],
        cheats=[pj.ProjectCheat("Infinite health", "obj2", "Health", "freeze", 999.0)],
        carried_from=fg.VERSION,
    )
    win.cheats.finder.adopt_carried(p, {"obj2": None}, merge_into=into)
    assert win.ctl.project is into and [o.id for o in into.objects] == ["obj1", "obj2"]
    assert into.carried_from == fg.VERSION and "obj2" not in win.ctl.live.trackers
    assert [o.label for o in path().objects] == ["Ammo", "Health"]


# -- bringing over: other work waits (AC-U3) ---------------------------------------------------
def test_carrying_turns_off_find_next_time_combine_find_again_lock_and_build(
    win, console, monkeypatch
):
    save_notes(hooked_health())
    connect(win)
    raw = struct.pack("<f", 5.0)
    for name in ("A", "B"):
        win.ctl.cheat_file.cheats.append(
            Cheat(
                name,
                [Location(LocationKind.SESSION_ADDRESS, fg.HEAP + 0x40, raw, raw)],
                value_type=ValueType.FLOAT,
            )
        )
    win.ctl.cheats_changed.emit()
    win.cheats.table.selectRow(1)
    assert win.cheats.again_btn.isEnabled() and win.cheats.learn_btn.isEnabled()
    finder = win.cheats.finder
    finder.carrying = True
    assert not win.cheats.again_btn.isEnabled() and not win.cheats.learn_btn.isEnabled()
    win.cheats.table.selectAll()
    assert not win.cheats.combine_btn.isEnabled()
    monkeypatch.setattr(mw, "LockWizard", lambda *a: pytest.fail("Lock it waits"))
    monkeypatch.setattr(win, "make_build_dialog", lambda **kw: pytest.fail("Make it work waits"))
    win.open_lock(win.cheats.cheats[1])
    assert win.status.text() == hints.CARRY_BUSY
    win.status.setText("")
    win.open_build(address=fg.HEAP + 0x40, vtype=ValueType.FLOAT, name="A")
    assert win.status.text() == hints.CARRY_BUSY
    finder.carrying = False
    win.cheats.table.selectRow(1)
    assert win.cheats.again_btn.isEnabled() and win.cheats.learn_btn.isEnabled()


# -- "Works after restart?" texts ----------------------------------------------------------------
def test_keeps_text_for_brought_over_cheats():
    raw = b"\0" * 4
    found = Cheat("H", [Location(LocationKind.SESSION_ADDRESS, 0, raw, raw)], anchor=("o", "f"))
    assert keeps_text(found) == FOUND_AGAIN
    found.carried = "check"
    assert keeps_text(found) == "Found again each run (check it)"
    found.carried = "test"
    assert keeps_text(found) == "Brought over: test it"
    session = Cheat("S", [Location(LocationKind.SESSION_ADDRESS, 0, raw, raw)])
    session.carried = "check"
    assert keeps_text(session) == "This session only", "check only for found-again cheats"
    assert found == Cheat(
        "H", [Location(LocationKind.SESSION_ADDRESS, 0, raw, raw)], anchor=("o", "f")
    ), "carried is not part of the cheat's identity"


# == AC-U2: fingerprints are learned while the notes' version runs ========================
class V1Console(Console):
    def __init__(self) -> None:
        super().__init__(layout=fg.V1)


@pytest.fixture
def v1(qtbot, monkeypatch):
    console = V1Console()
    monkeypatch.setattr(rf.ah, "WATCH_SECONDS", 1)
    monkeypatch.setattr(rf.ah, "PROVE_SECONDS", 1)
    monkeypatch.setattr(rf, "SLOT_WAIT_S", 0.5)
    reads = []
    real = carry_ui.up.read_code_image

    def counted(session, progress=None, cancel=None):
        reads.append(1)
        return real(session, progress, cancel)

    monkeypatch.setattr(carry_ui.up, "read_code_image", counted)
    ctl = Controller(lambda ip, port: console.game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.cheats.finder.ask_confirm = lambda text: True
    w.cheats.finder.sleep = lambda _s: None
    w.reads = reads
    w.real_read = real
    yield w, console
    ctl.shutdown()


def v1_notes(parts=False) -> pj.Project:
    """Notes from before this change: a hook whose fingerprint was never checked."""
    g = fg.build(1)
    cand = ah.HookCandidate(
        fg.READER, fg.VMOVSS_R, "vmovss xmm0,[r12+2ACh]", 2, "reader", "r12", fg.HP, True
    )
    hook = ah.plan_hook(g.session, cand, fg.PLAYER_VT - fg.BASE)
    assert not hook.sig_unique
    obj = pj.ObjectSpec(
        "obj1",
        "Health",
        fg.PLAYER_VT - fg.BASE,
        [pj.Field("Health", fg.HP, ValueType.FLOAT, (0, pj.CLASS_FLOAT_LIMIT), 100.0)],
        [hook, pj.VtableAnchor()],
    )
    cheats = [pj.ProjectCheat("Infinite health", "obj1", "Health", "freeze", 999.0)]
    if parts:
        nop = pj.ProjectPart("no death", offset=fg.SUB_SITE - fg.BASE, on=b"\x90" * 6, off=fg.SUB_W)
        cheats.append(
            pj.ProjectCheat("God", parts=[pj.ProjectPart("health", "obj1", "Health", 999.0), nop])
        )
    p = pj.Project(fg.TITLE, fg.VERSION, objects=[obj], cheats=cheats)
    pj.save(pj.project_path(fg.TITLE, fg.VERSION), p)
    return p


def test_connect_learns_a_unique_fingerprint_and_writes_only_the_hook(v1, caplog):
    w, console = v1
    v1_notes()
    caplog.set_level("INFO")
    console.game.fake.writes.clear()
    connect(w)
    saved = pj.load(pj.project_path(fg.TITLE, fg.VERSION))
    h = saved.objects[0].hook
    assert h.sig_unique and w.reads == [1]
    img = w.real_read(w.ctl.session)
    clean = img.overlay([(h.hook, h.orig)])
    assert clean.find(up.Signature.from_text(h.signature), 2) == [h.hook]
    hook_bytes = {fg.READER, fg.BASE + h.cave, fg.BASE + h.slot}
    assert {a for _p, a, _d in console.game.fake.writes} <= hook_bytes, "only the hook went in"
    assert "action=learn_signature source=notes object=obj1 offset=0x200" in caplog.text
    assert "action=learn_signatures source=notes saved=1 version=03.000.005" in caplog.text
    w.cheats.finder.save()
    assert w.reads == [1], "once per version per run"


def test_code_parts_go_to_the_signatures_file(v1):
    w, _console = v1
    v1_notes(parts=True)
    connect(w)
    sigs = up.load_signatures(up.signatures_path(fg.TITLE, fg.VERSION))
    assert set(sigs) == {fg.SUB_SITE - fg.BASE}
    assert sigs[fg.SUB_SITE - fg.BASE].text().startswith("29 88 B0 02 00 00")


def test_nothing_to_learn_means_no_read(v1):
    w, _console = v1
    p = v1_notes()
    p.objects[0].hook.sig_unique = True
    pj.save(pj.project_path(fg.TITLE, fg.VERSION), p)
    connect(w)
    assert w.reads == []


def test_disconnect_cancels_and_nothing_is_saved(v1, monkeypatch):
    w, _console = v1
    v1_notes()
    real = carry_ui.up.read_code_image

    def cut(session, progress=None, cancel=None):
        w.ctl.run_before_disconnect()  # the player pressed Disconnect meanwhile
        return real(session, progress, cancel)

    monkeypatch.setattr(carry_ui.up, "read_code_image", cut)
    connect(w)
    assert not w.fingerprints.running and not w.ctl.code_reading
    saved = pj.load(pj.project_path(fg.TITLE, fg.VERSION))
    assert not saved.objects[0].hook.sig_unique
    assert w.fingerprints._done == set(), "tried again on the next connect"


def test_one_code_read_at_a_time(v1):
    w, _console = v1
    v1_notes()
    w.ctl.code_reading = True  # v2's cheat-file learner is reading
    connect(w)
    assert (
        w.reads == []
        and not pj.load(pj.project_path(fg.TITLE, fg.VERSION)).objects[0].hook.sig_unique
    )
    w.ctl.code_reading = False
    w.cheats.finder.save()
    assert w.reads == [1]
    assert pj.load(pj.project_path(fg.TITLE, fg.VERSION)).objects[0].hook.sig_unique


def test_cheat_file_learner_waits_for_a_notes_read(v1, monkeypatch):
    w, _console = v1
    connect(w)
    learned = []
    monkeypatch.setattr(w.ctl.tasks, "run", lambda *a: learned.append(a))
    w.ctl.code_reading = True
    w.cheats._maybe_learn()
    assert learned == []

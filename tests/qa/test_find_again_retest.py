"""QA re-test of the BUG-01..09 fixes (82aec57..59201ae), 2026-11-02: regressions the
fixes introduced or left. See qa/reports/2026-11-02-find-it-again.md, section 8.
``xfail(strict=True)`` = open bug: drop the marker when it is fixed."""

from __future__ import annotations

import json
import struct

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.values import ValueType
from cheatmaker.ui.find_again import LearnDialog
from tests import fakegame as fg
from tests.qa.test_find_again_qa import (
    HOOKED,
    add_health_cheat,
    connect,
    learn_core,
    learn_ui,
    set_on,
)


# == BUG-10: the tracker jumps to another object of the same class =====================
@pytest.mark.xfail(reason="BUG-10: tracker follows a same-class twin without asking", strict=True)
def test_r01_second_object_of_the_same_class_is_never_followed_silently():
    """Two live objects of the player's class (a second character, a clone, a co-op
    partner) both go through the hooked code. The player's object still verifies, so
    switching to the other one is a disagreement (AC-12.3): ask, or keep the old one."""
    game = fg.build(1)
    obj = learn_core(game).obj
    ah.install(game.session, obj)
    tracker = rf.Tracker(game.session, obj, game.player)
    assert tracker.current() == game.player
    twin = fg.HEAP + 0x9000
    game.fake.poke(fg.PID, twin, game.fake.peek(fg.PID, game.player, 0x300))
    game.touched = [*game.touched, twin]  # the player is still touched every frame too
    game.fake.read(fg.PID, fg.BASE, 1)
    assert ah.read_slot(game.session, obj.hook)[0] == twin
    assert tracker.current() != twin, "never write another object without asking"


# == BUG-01 (reopened, partial): Refresh from the home screen, then close ================
@pytest.mark.xfail(reason="BUG-01: hook left in after home-screen Refresh then close", strict=True)
def test_r02_refresh_from_home_screen_then_close_takes_the_hook_out(win, console):
    connect(win)
    learn_ui(win, add_health_cheat(win, console.game))
    fake = console.game.fake
    fake.foreground_pid = None  # home screen in front; the game still runs, same pid
    win.connect_view.refresh_btn.click()
    win.ctl.shutdown()  # close the app from here
    assert fake.peek(fg.PID, fg.READER, 10) == HOOKED


# == BUG-11: found by a memory path only: a move pauses the freeze for good ==============
@pytest.mark.xfail(reason="BUG-11: chain-only object never re-found after a move", strict=True)
def test_r03_chain_found_value_is_followed_after_a_move(win, console):
    console.restart(1, chain=True)
    connect(win)
    dlg = LearnDialog(win.ctl, win.cheats.finder, add_health_cheat(win, console.game), win)
    dlg.pointers.setChecked(True)
    dlg.start_btn.click()
    win.ctl.shutdown()
    run2 = console.restart(2, chain=True)
    connect(win)
    assert "obj1" in win.cheats.finder.by_chain
    set_on(win, 0, True)
    run2.move_player(fg.HEAP + 0x6000)  # new biome: the path's holder is stale
    tracker = win.ctl.live.trackers["obj1"]
    for _ in range(3):
        run2.fake.read(fg.PID, fg.BASE, 1)
        tracker._last_try = -10.0  # skip the 1 s retry wait
        win.ctl.freezer.tick()
    assert tracker.address == run2.player or "Find it" in win.status.text(), (
        "either follow it (put the hook in) or tell the player the freeze stopped"
    )


# == BUG-12: notes from format 1 keep the old 10,000 cap ================================
@pytest.mark.xfail(reason="BUG-12: migrated format-1 field keeps the 10,000 cap", strict=True)
def test_r04_format1_notes_get_the_class_range_on_migration(win, console):
    connect(win)
    learn_ui(win, add_health_cheat(win, console.game))
    win.ctl.shutdown()
    path = pj.project_path(fg.TITLE, fg.VERSION)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["format_version"] = 1
    doc["objects"][0]["fields"][0]["range"] = [0, 10000]  # what format 1 wrote
    path.write_text(json.dumps(doc), encoding="utf-8")
    run2 = console.restart(2)
    run2.set_health(25_000.0)
    connect(win)
    assert win.ctl.live.trackers.get("obj1") is not None, "same class, value simply grew"


# == passing regression checks ==========================================================
def test_r05_two_values_get_their_own_routine_and_slot_and_both_fill(win, console):
    connect(win)
    g = console.game
    learn_ui(win, add_health_cheat(win, g))
    g.fake.poke(fg.PID, fg.BASE + 0x300, bytes.fromhex("C4C17A108424B0020000"))  # shield
    g.fake.poke(fg.PID, g.player + 0x2B0, struct.pack("<f", 50.0))
    g.fake.read_hits[g.player + 0x2B0] = [WatchHit(fg.BASE + 0x30A, {"r12": g.player})]
    raw = struct.pack("<f", 77.0)
    c2 = Cheat(
        "Shield",
        [Location(LocationKind.SESSION_ADDRESS, g.player + 0x2B0, raw, raw)],
        freeze=True,
        value_type=ValueType.FLOAT,
    )
    win.ctl.ensure_file().cheats.append(c2)
    learn_ui(win, c2)
    h1, h2 = (o.hook for o in win.ctl.project.objects)
    rooms = [(ah.keep_cave(h), h.cave + 256) for h in (h1, h2)]
    assert rooms[0][1] <= rooms[1][0] or rooms[1][1] <= rooms[0][0], "rooms don't overlap"
    assert h1.slot != h2.slot and abs(h1.slot - h2.slot) >= 16
    assert g.fake.peek(fg.PID, fg.READER, 1) == b"\xe9"
    assert g.fake.peek(fg.PID, fg.BASE + 0x300, 1) == b"\xe9"
    win.ctl.shutdown()
    assert g.fake.peek(fg.PID, fg.READER, 10) == HOOKED
    assert g.fake.peek(fg.PID, fg.BASE + 0x300, 10) == bytes.fromhex("C4C17A108424B0020000")


def test_r06_format1_hook_dropped_found_by_search_and_saved_as_current(win, console):
    connect(win)
    learn_ui(win, add_health_cheat(win, console.game))
    win.ctl.shutdown()
    path = pj.project_path(fg.TITLE, fg.VERSION)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["format_version"] = 1
    path.write_text(json.dumps(doc), encoding="utf-8")
    run2 = console.restart(2)
    connect(win)
    assert run2.fake.peek(fg.PID, fg.READER, 10) == HOOKED, "old hook never installed"
    assert "older Cheat Maker" in win.status.text()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["format_version"] == pj.FORMAT_VERSION
    assert [a["kind"] for a in saved["objects"][0]["anchors"]] == ["vtable_scan"]


def test_r07_connect_again_with_a_frozen_cheat_takes_out_and_puts_back_once(win, console):
    connect(win)
    learn_ui(win, add_health_cheat(win, console.game))
    set_on(win, 0, True)
    fake = console.game.fake
    connect(win)  # Connect again
    assert fake.peek(fg.PID, fg.READER, 1) == b"\xe9", "put back on connect"
    assert "obj1" in win.cheats.finder.installed
    win.ctl.shutdown()
    assert fake.peek(fg.PID, fg.READER, 10) == HOOKED

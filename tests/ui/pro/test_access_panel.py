"""What does this instruction access (spec PR-3, AC-3.4/3.5) in the Pro window, on the
fake game whose CPU runs the sampler cave."""

from __future__ import annotations

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.console.fake import FakeStop
from cheatmaker.core.pro import access_sampler as sm
from cheatmaker.errors import LockFailed
from cheatmaker.ui.pro import hints
from tests import fakegame as fg


@pytest.fixture
def panel(pro, game):
    p = pro.watch.access
    p.sleep = game.fight(1)
    p.seconds = 0.5
    p.clock = game.fake.debugger.clock
    return p


def test_sample_lists_addresses_and_unhooks(panel, game, pro):  # AC-3.4
    panel.place.setText(f"eboot.bin+{fg.WRITER - fg.BASE:#x}")
    panel.run_btn.click()
    assert panel.table.rowCount() == 2
    assert panel.table.item(0, 0).text() in {
        f"{o + fg.HP:#x}" for o in (fg.PLAYER_AT[1],) + fg.ENEMIES_AT[1]
    }
    assert pro.status.text() == hints.ACCESS_DONE.format(n=2, polls=2)
    assert game.fake.peek(fg.PID, fg.WRITER, len(fg.VMOVSS_W)) == fg.VMOVSS_W
    assert not panel.sampling and panel.run_btn.isEnabled()


def test_add_a_sampled_address_to_the_list(panel, pro):
    panel.sample_at(fg.WRITER)
    panel.table.selectRow(0)
    panel.add_btn.click()
    assert pro.addresses.rows[0].address == panel.rows[0].address


def test_no_room_offers_a_watch_instead(panel, game, pro, monkeypatch):  # AC-3.5
    def full(*_a, **_k):
        raise LockFailed("no spare space at the end of the game's code for the patch")

    monkeypatch.setattr(sm.cv, "find_cave", full)
    asked: list[str] = []
    panel.ask_watch = lambda reason: asked.append(reason) or True
    regs = {"r12": fg.PLAYER_AT[1]}
    game.fake.debugger.stops = [FakeStop(0, WatchHit(fg.WRITER, regs)) for _ in range(2)]
    panel.sample_at(fg.WRITER)
    assert asked and "No room for the sampler" in asked[0]
    assert ("set", 0, fg.WRITER, 1, "execute") in game.fake.debugger.calls
    assert [(r.address, r.count) for r in panel.rows] == [(fg.PLAYER_AT[1] + fg.HP, 2)]
    assert pro.status.text() == hints.ACCESS_WATCHED.format(n=1, hits=2)


def test_no_room_declined_writes_nothing(panel, game, monkeypatch):
    monkeypatch.setattr(sm.cv, "find_table", lambda s: (_ for _ in ()).throw(LockFailed("full")))
    panel.ask_watch = lambda reason: False
    n = len(game.fake.writes)
    panel.sample_at(fg.WRITER)
    assert len(game.fake.writes) == n and game.fake.debugger.sessions == 0


def test_code_view_and_watch_rows_open_the_sampler(pro, panel, game):
    pro.code.go(fg.WRITER)
    row = next(k for k, ln in enumerate(pro.code.rows) if ln.ip == fg.WRITER)
    pro.code.table.selectRow(row)
    assert pro.code.access_btn.isEnabled()
    pro.code.access_btn.click()
    assert pro.tabs.currentWidget() is pro.watch and panel.rows
    assert panel.place.text() == f"eboot.bin+{fg.WRITER - fg.BASE:#x}"


def test_a_running_cheat_at_the_instruction_is_refused(panel, pro, game):
    from tests.ui.pro.test_cheats_tab import new_cheat

    cheat = new_cheat(pro.cheats)
    pro.cheats.on_box.click()
    assert cheat.enabled
    n = len(game.fake.writes)
    panel.sample_at(fg.WRITER)
    assert "Turn it off first" in pro.status.text() and len(game.fake.writes) == n

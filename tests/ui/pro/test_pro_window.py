"""Pro step 11: the window shell, shared game header, liveness poll, safety notice."""

from __future__ import annotations

import dataclasses

from cheatmaker.settings import Settings
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.pro_window import POLL_MS
from tests import fakegame as fg


def _banners(pro) -> list[str]:
    return [t.banner.text() for t in pro.pro_tabs if not t.banner.isHidden()]


def test_pro_shows_the_main_windows_game_and_opens_no_connection(win, pro, game):
    """AC-1.1: same title ID, version and PID; no new console connection."""
    made = []
    win.ctl.session._factory = lambda ip, port: made.append(ip) or game.fake
    g = win.ctl.session.game
    assert g.title_id in pro.header.text() and g.version in pro.header.text()
    assert f"PID {g.pid}" in pro.header.text()
    assert pro.tabs.count() == len(pro.pro_tabs) == 6
    assert all(t.game == g and t.running and t.body.isEnabled() for t in pro.pro_tabs)
    assert _banners(pro) == []
    pro.poll()
    assert made == [], "Pro uses the main window's connection"


def test_every_tab_says_game_not_running_when_it_closes(pro, game):
    """AC-1.2: the poll (every 2 s, under 5 s) notices the game has gone."""
    assert POLL_MS <= 2500
    game.fake.remove_process(fg.PID)
    pro.poll()
    assert _banners(pro) == [hints.GAME_NOT_RUNNING] * len(pro.pro_tabs)
    assert not any(t.body.isEnabled() for t in pro.pro_tabs)
    assert hints.GAME_NOT_RUNNING in pro.header.text()


def test_a_suspended_game_in_the_background_is_still_running(pro, game):
    game.fake.foreground_pid = None  # the PS5 home screen is up
    pro.poll()
    assert _banners(pro) == []


def test_a_new_game_run_clears_the_banner(win, pro, game):
    g = win.ctl.session.game
    game.fake.remove_process(fg.PID)
    pro.poll()
    win.ctl.session._set_game(dataclasses.replace(g, pid=g.pid + 1))  # Refresh found it again
    assert _banners(pro) == []
    assert f"PID {g.pid + 1}" in pro.header.text()


def test_no_game_after_refresh(win, pro):
    win.ctl.session._set_game(None)
    assert _banners(pro) == [hints.NO_GAME] * len(pro.pro_tabs)


def test_lost_connection_during_the_poll_is_handled(win, pro, game):
    game.fake.disconnect()
    pro.poll()
    assert not win.ctl.session.connected
    assert _banners(pro) == [hints.NOT_CONNECTED] * len(pro.pro_tabs)


def test_safety_notice_once_per_game(win, pro, notices):
    assert notices == [hints.NOTICE_TITLE]
    win.open_pro()  # opened again: not again
    assert notices == [hints.NOTICE_TITLE]
    assert win.ctl.session.game.title_id in Settings.load().pro_notice_shown


def test_pro_button_opens_one_window(win, qtbot):
    win.pro_btn.click()
    qtbot.addWidget(win.pro)
    first = win.pro
    win.pro_btn.click()
    assert win.pro is first and first.isVisible()


def test_closing_pro_keeps_the_connection(win, pro):
    pro.close()
    assert win.ctl.session.connected and not pro.timer.isActive()


def test_bad_notice_setting_is_dropped(tmp_path):
    import json

    path = Settings.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pro_notice_shown": "PPSA01285"}), encoding="utf-8")
    assert Settings.load().pro_notice_shown == []

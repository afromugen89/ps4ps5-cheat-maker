"""QA round for Cheat Maker Pro R1 steps 11-15 (PR #39): the Pro window, Cheats tab and
cave editor, export / import dialogs, Code view, Memory view, Scanner tab and address
list, plus the review fixes (Pro status line, drafts, search ownership, My cheats
toggles through Pro's rules, per-row freezes, liveness with ``busy.short_wait``).
Spec docs/spec-pro.md (PR-1, PR-2, PR-6, PR-7, PR-8, PR-12, PR-14); design
docs/architecture-pro.md steps 11-15. Report: qa/reports/2026-11-22-pro-steps-11-15.md.

Bugs are strict xfail tests named after their BUG id; remove the marker once fixed.
FakeConsole stands in for the PS5 and FakeAssembler for NASM. Tests marked "real
threads" switch the Pro window's TaskRunner to Qt's thread pool and gate the worker
with events, so the interleavings are deterministic.
"""

from __future__ import annotations

import contextlib
import threading
import time

import pytest
from PySide6.QtCore import Qt

from cheatmaker import busy
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.pro import code_page as cp
from cheatmaker.core.pro import compile as pc_compile
from cheatmaker.core.pro import memory_page as mp
from cheatmaker.core.pro import scan as sc
from cheatmaker.core.pro.asm import AssemblerNotFound, FakeAssembler
from cheatmaker.core.session import ALIVE_WAIT
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import ConsoleBusy
from cheatmaker.ui.pro import hints
from cheatmaker.ui.tasks import TaskRunner
from tests import fakegame as fg
from tests.ui.pro.conftest import game as game
from tests.ui.pro.conftest import notices as notices
from tests.ui.pro.conftest import pro as pro
from tests.ui.pro.conftest import questions as questions
from tests.ui.pro.conftest import tab as tab
from tests.ui.pro.conftest import warnings as warnings
from tests.ui.pro.conftest import win as win
from tests.ui.pro.test_cheats_tab import _cave_file, new_cheat

HP_ADDR = fg.PLAYER_AT[1] + fg.HP32
STATIC = fg.DATA + 0x100  # a static value in the module's data
SOURCE_A = "nop\noriginal\njmp return\n"
SOURCE_B = "nop\nnop\noriginal\njmp return\n"


# -- helpers --------------------------------------------------------------------------------
def _tick(win, cheat: Cheat, on: bool) -> None:
    """Tick or untick the cheat in My cheats (the main window)."""
    row = win.ctl.cheat_file.cheats.index(cheat)
    state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
    win.cheats.table.item(row, 0).setCheckState(state)


def _threaded(tasks: TaskRunner) -> None:
    """Run this runner's work on Qt's thread pool from now on (real threads)."""
    tasks.sync = False


def _gated_build(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """compile.build waits on ``gate`` (worker thread); ``entered`` says it started."""
    gate, entered = threading.Event(), threading.Event()
    real = pc_compile.build

    def slow(*a, **kw):
        entered.set()
        gate.wait(5)
        return real(*a, **kw)

    monkeypatch.setattr(pc_compile, "build", slow)
    return gate, entered


def _frozen_static_cheat(win, game) -> tuple[Cheat, bytes]:
    off = game.fake.peek(fg.PID, STATIC, 4)
    c = Cheat(
        "Frozen static",
        [Location(LocationKind.MODULE_VALUE, STATIC - fg.BASE, b"\x63\x00\x00\x00", off)],
        freeze=True,
        value_type=ValueType.INT32,
    )
    win.ctl.ensure_file().cheats.append(c)
    win.ctl.cheats_changed.emit()
    return c, off


# == Pro window lifecycle and liveness (step 11) ===============================================
def test_open_before_connecting_says_so_then_shows_the_game(
    qtbot, game, notices, warnings, questions
):  # TC-01
    from cheatmaker.settings import Settings
    from cheatmaker.ui.controller import Controller
    from cheatmaker.ui.main_window import MainWindow

    ctl = Controller(lambda ip, port: game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    w.pro_assembler_factory = FakeAssembler
    qtbot.addWidget(w)
    try:
        w.open_pro()
        assert w.pro.header.text() == hints.NOT_CONNECTED and notices == []
        w.connect_view.ip.setText("192.168.1.50")
        w.connect_view.connect_btn.click()
        assert fg.TITLE in w.pro.header.text() and str(fg.PID) in w.pro.header.text()
        assert notices == [hints.NOTICE_TITLE], "safety notice once the game is known"
    finally:
        w.pro.force_close = True
        w.pro.close()
        ctl.shutdown()


def test_liveness_poll_on_a_real_worker_marks_every_tab(win, pro, game, qtbot):  # TC-02
    _threaded(pro.tasks)
    game.fake.remove_process(fg.PID)
    started = time.monotonic()
    pro.poll()
    qtbot.waitUntil(lambda: not pro.alive, timeout=5000)
    assert time.monotonic() - started < 2.0
    assert all(not t.banner.isHidden() and not t.body.isEnabled() for t in pro.pro_tabs)
    assert hints.GAME_NOT_RUNNING in pro.header.text()


def test_short_wait_never_leaks_to_later_jobs_on_pool_threads(win, qtbot):  # TC-03
    """The liveness limit is per call: guided jobs reusing the pool's threads wait as
    long as it takes (thread_wait() is None), also after a poll that raised."""
    runner = TaskRunner(sync=False)
    session = win.ctl.session
    errors: list[BaseException] = []
    for _ in range(8):
        runner.run(session.game_alive, lambda _r: None, errors.append)
    qtbot.waitUntil(lambda: not runner.busy, timeout=5000)

    def poll_that_raises():
        with busy.short_wait(ALIVE_WAIT):
            raise ConsoleBusy("busy")

    for _ in range(4):
        runner.run(poll_that_raises, lambda _r: None, lambda _e: None)
    qtbot.waitUntil(lambda: not runner.busy, timeout=5000)
    seen: list[object] = []
    for _ in range(16):
        runner.run(busy.thread_wait, seen.append, seen.append)
    qtbot.waitUntil(lambda: not runner.busy, timeout=5000)
    assert errors == [] and seen == [None] * 16


def test_ng_lock_on_a_guided_worker_still_waits_for_a_long_search():  # TC-04
    """Guided paths are unaffected: a worker without short_wait queues behind a holder
    for longer than ALIVE_WAIT and then gets the socket (no ConsoleBusy)."""
    from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient

    client = Ps5DebugNgClient.__new__(Ps5DebugNgClient)
    client._lock = threading.RLock()
    client._end_pending = False
    held = threading.Event()
    seen: list[str] = []

    def long_search():
        with client._lock:
            held.set()
            time.sleep(ALIVE_WAIT + 0.3)

    def guided():
        held.wait(2)
        try:
            with client._locked():
                seen.append("got it")
        except ConsoleBusy:
            seen.append("busy")

    def poll():
        held.wait(2)
        with busy.short_wait(0.05):
            try:
                with client._locked():
                    seen.append("poll got it")
            except ConsoleBusy:
                seen.append("poll busy")

    threads = [threading.Thread(target=f) for f in (long_search, guided, poll)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(seen) == ["got it", "poll busy"]


def test_closing_the_main_window_stops_pro_freezes_without_asking(win, pro, questions):  # TC-05
    lst = pro.addresses
    lst.add(HP_ADDR, ValueType.INT32)
    lst.table.item(0, 4).setCheckState(Qt.CheckState.Checked)
    assert win.ctl.freezer.keys
    win.close()
    assert questions == [] and not win.ctl.freezer.keys


def test_refresh_to_another_game_info_drops_pro_freezes_consistently(win, pro, game):  # TC-06
    lst = pro.addresses
    row = lst.add(HP_ADDR, ValueType.INT32)
    lst.table.item(0, 4).setCheckState(Qt.CheckState.Checked)
    game.fake.foreground_pid = None  # the PS5 home screen: the version can't be read
    win.ctl.session.refresh_game()
    assert row.frozen is None and win.ctl.freezer.keys == set()
    assert lst.table.item(0, 4).checkState() == Qt.CheckState.Unchecked


def test_bug09_site_changed_dialog_has_real_line_breaks(win, tab, game, warnings):  # TC-07
    new_cheat(tab)
    game.fake.poke(fg.PID, fg.WRITER, b"\x90" * 5)
    tab.on_box.click()
    ((_title, text),) = warnings
    assert "\\n" not in text
    assert "\n    expected: C4 C1 7A 11 8C" in text


# == Cheats tab, cave editor and My cheats toggles (step 12 + review fixes) ======================
def test_drafts_survive_switching_cheats_and_never_reach_the_cheat(win, tab):  # TC-08
    a = new_cheat(tab, "A")
    b = new_cheat(tab, "B", hook=f"eboot.bin+{fg.READER - fg.BASE:#x}")
    tab.select_cheat(a)
    tab.editor.source.setPlainText(SOURCE_B)
    tab.select_cheat(b)
    assert tab.editor.source.toPlainText() == pc_compile.DEFAULT_SOURCE
    tab.select_cheat(a)
    assert tab.editor.source.toPlainText() == SOURCE_B
    assert a.pro.entries[0].source == pc_compile.DEFAULT_SOURCE


def test_toggles_are_disabled_while_a_build_runs(win, tab, qtbot, monkeypatch):  # TC-09
    new_cheat(tab)
    _threaded(tab.tasks)
    gate, entered = _gated_build(monkeypatch)
    tab.editor.source.setPlainText(SOURCE_A)
    tab.editor.build_btn.click()
    assert entered.wait(5)
    try:
        assert not tab.on_box.isEnabled() and not tab.new_btn.isEnabled()
        assert not tab.import_btn.isEnabled() and not tab.add_var_btn.isEnabled()
    finally:
        gate.set()
        qtbot.waitUntil(lambda: not tab.tasks.busy, timeout=5000)


def test_bug01_build_never_changes_a_cheat_that_went_on_meanwhile(
    win, tab, game, qtbot, monkeypatch
):  # TC-10 (real threads)
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    _threaded(tab.tasks)
    gate, entered = _gated_build(monkeypatch)
    tab.editor.source.setPlainText("nop\nnop\nnop\noriginal\njmp return\n")
    tab.editor.build_btn.click()
    assert entered.wait(5)
    _tick(win, cheat, True)  # the player ticks it in the main window meanwhile
    gate.set()
    qtbot.waitUntil(lambda: not tab.tasks.busy, timeout=5000)
    if cheat.enabled:  # what Pro records as on must be what the game runs
        in_game = game.fake.peek(fg.PID, fg.BASE + entry.cave, len(entry.cave_bytes))
        assert in_game == entry.cave_bytes


def test_bug05_typing_during_a_build_is_kept(win, tab, qtbot, monkeypatch):  # TC-11
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    _threaded(tab.tasks)
    gate, entered = _gated_build(monkeypatch)
    tab.editor.source.setPlainText(SOURCE_A)
    tab.editor.build_btn.click()
    assert entered.wait(5)
    editable = not tab.editor.source.isReadOnly()
    if editable:
        tab.editor.source.setPlainText(SOURCE_B)  # the player keeps typing
    gate.set()
    qtbot.waitUntil(lambda: not tab.tasks.busy, timeout=5000)
    assert entry.source == SOURCE_A
    if editable:  # either the editor is locked during the build, or the text survives
        assert tab.editor.source.toPlainText() == SOURCE_B
        assert tab.editor.has_draft(entry)
    assert not tab.editor.build_btn.isEnabled() or not editable


def test_bug08_a_list_refresh_keeps_typing_and_its_assemble(win, tab, qtbot):  # TC-12
    new_cheat(tab)
    ed = tab.editor
    ed.source.setPlainText(SOURCE_A)
    cur = ed.source.textCursor()
    cur.setPosition(5)
    ed.source.setTextCursor(cur)
    win.ctl.cheats_changed.emit()  # e.g. a tracker answer in the main window
    assert ed.source.textCursor().position() == 5
    qtbot.waitUntil(lambda: ed.bytes_view.toPlainText().startswith("90 "), timeout=1500)


@pytest.mark.parametrize("where", ["pro", "my_cheats"])
def test_bug02_adopted_freeze_cheat_turns_on_or_writes_nothing(
    win, pro, game, qtbot, where
):  # TC-13
    tab = pro.cheats
    tab.sleep = lambda _s: None
    c, off = _frozen_static_cheat(win, game)
    tab.select_cheat(c)
    tab.adopt_btn.click()
    if c.pro is None:
        return  # refusing to adopt a freeze cheat is a fine answer too
    with qtbot.captureExceptions() as raised:
        if where == "pro":
            tab.select_cheat(c)
            tab.on_box.click()
        else:
            _tick(win, c, True)
    assert raised == []
    assert c.enabled or game.fake.peek(fg.PID, STATIC, 4) == off


def test_bug03_pro_cheat_still_in_the_game_after_refresh_can_be_turned_off(
    win, tab, game, warnings
):  # TC-14
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    tab.on_box.click()
    assert cheat.enabled
    game.fake.foreground_pid = None  # Refresh from the PS5 home screen (same PID)
    win.ctl.session.refresh_game()
    assert game.fake.peek(fg.PID, fg.WRITER, 1) == b"\xe9", "the cheat still runs"
    if not cheat.enabled:  # shown off: ticking it must work (it is already in)
        tab.select_cheat(cheat)
        tab.on_box.click()
    assert cheat.enabled, "the game runs the cheat, so the app must be able to turn it off"
    tab.on_box.click()
    assert game.fake.peek(fg.PID, fg.WRITER, len(entry.off)) == entry.off


def test_bug07_convert_says_why_when_the_assembler_fails(win, tab, tmp_path, qtbot):  # TC-15
    path = _cave_file(tmp_path)
    tab.pick_file = lambda: str(path)

    class Gone(FakeAssembler):
        def assemble(self, *a, **kw):
            raise AssemblerNotFound("The assembler couldn't start: access denied")

    tab.assembler = Gone()
    said: list[str] = []

    def run(dlg):
        dlg.do_convert()
        said.append(dlg.message.text())
        dlg.reject()

    tab.exec_dialog = run
    with qtbot.captureExceptions() as raised:
        tab.import_btn.click()
    assert raised == [] and said and said[0]


def test_my_cheats_untick_of_a_cave_cheat_never_blocks_the_ui(win, tab, game, qtbot):  # TC-16
    """Observation O1, fixed: the jump goes back at once; the AC-8.5 wait before the
    cave is zeroed runs on a timer, not on the UI thread."""
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    tab.on_box.click()
    started = time.monotonic()
    _tick(win, cheat, False)
    assert not cheat.enabled
    assert time.monotonic() - started < pc_compile.DISABLE_WAIT
    assert game.fake.peek(fg.PID, fg.WRITER, len(entry.off)) == entry.off
    cave = fg.BASE + entry.cave
    assert game.fake.peek(fg.PID, cave, len(entry.cave_bytes)) == entry.cave_bytes, "not yet"
    qtbot.waitUntil(
        lambda: game.fake.peek(fg.PID, cave, len(entry.cave_bytes)) == bytes(len(entry.cave_bytes)),
        timeout=3000,
    )


def test_import_of_another_games_file_is_added_without_a_word(win, tab, tmp_path):  # TC-17
    """Observation O3, pinned: no title / version check on import."""
    from cheatmaker import formats
    from cheatmaker.core.cheats import CheatFile

    other = CheatFile(
        "Other",
        "CUSA99999",
        "09.99",
        [Cheat("X", [Location(LocationKind.CODE_PATCH, 0x100, b"\x90", b"\x00")])],
    )
    path = tmp_path / "CUSA99999_09.99.json"
    path.write_bytes(formats.dumps(other, "json"))
    tab.pick_file = lambda: str(path)
    tab.exec_dialog = lambda dlg: dlg.do_add()
    tab.import_btn.click()
    assert [c.name for c in win.ctl.cheat_file.cheats] == ["X"]
    assert win.pro.status.text() == hints.IMPORTED.format(n=1)


# == Code view and Memory view (steps 13-14) ======================================================
def test_bug06_code_view_shows_the_patched_bytes_after_a_toggle(pro):  # TC-18
    tab = pro.cheats
    tab.sleep = lambda _s: None
    new_cheat(tab, "Infinite health")
    code = pro.code
    code.go(fg.WRITER)
    assert code.selected_line().text.startswith("vmovss")
    tab.on_box.click()
    line = code.selected_line()
    assert line.mark == "Infinite health (on)"
    assert line.text.startswith("jmp"), "the game now holds the jump"


def test_bug10_one_cheat_is_named_once_per_instruction(win, pro):  # TC-19
    c = Cheat(
        "Two",
        [
            Location(LocationKind.CODE_PATCH, fg.WRITER - fg.BASE, b"\x90", b"\xc4"),
            Location(LocationKind.CODE_PATCH, fg.WRITER - fg.BASE + 1, b"\x90", b"\xc1"),
        ],
    )
    c.enabled = True
    win.ctl.ensure_file().cheats.append(c)
    code = pro.code
    code.go(fg.WRITER)
    code.original.setChecked(True)
    marks = [ln.mark for ln in code.rows if ln.mark]
    assert marks == ["Two (on)"]


def test_bug12_failed_go_leaves_back_alone(pro):  # TC-20
    code = pro.code
    code.go(fg.WRITER)
    code.go(0x10)  # not mapped: "isn't in the game's memory"
    assert code.page.target == fg.WRITER
    assert code.back == [] and not code.back_btn.isEnabled()


@pytest.mark.parametrize("view", ["memory", "code"])
def test_bug11_the_last_page_asked_for_wins(pro, qtbot, monkeypatch, view):  # TC-21
    """Real threads: the first request is held back until the second has shown."""
    _threaded(pro.tasks)
    mod = mp if view == "memory" else cp
    tab = pro.memory if view == "memory" else pro.code
    first, last = (fg.HEAP, fg.BASE + 0x40) if view == "memory" else (fg.READER, fg.WRITER)
    gate = threading.Event()
    real = mod.read_page

    def slow(session, addr):
        if addr == first:
            gate.wait(5)
        return real(session, addr)

    monkeypatch.setattr(mod, "read_page", slow)
    tab.go(first)
    tab.go(last)
    qtbot.waitUntil(lambda: tab.page is not None, timeout=3000)
    gate.set()
    qtbot.waitUntil(lambda: not pro.tasks.busy, timeout=3000)
    shown = tab.target if view == "memory" else tab.page.target
    assert shown == last


def test_memory_view_unknown_bytes_and_inspector_at_a_map_edge(pro):  # TC-22
    m = pro.memory
    edge = fg.HEAP + fg.HEAP_SIZE - 2  # the last 2 bytes of the heap; then unmapped
    m.go(edge)
    assert m.selected_address() == edge
    assert m.values["UINT16"].text() != mp.UNKNOWN
    assert m.values["INT32"].text() == mp.UNKNOWN, "runs off the page"
    assert len(m.page.data) == mp.PAGE and None not in m.page.data
    m.go(fg.HEAP + fg.HEAP_SIZE)  # the next page: nothing mapped there
    assert set(m.page.data) == {None} and m.values["UINT8"].text() == mp.UNKNOWN
    assert m.table.item(0, 0).text() == mp.UNKNOWN


# == Scanner tab, search ownership, address list (step 15 + review fixes) =========================
def _race(win, monkeypatch, guided) -> list[str]:
    """A Pro first scan holds Session.search_lock and is about to call scan_start (it
    has checked it owns the search); then a guided search starts. Returns the order
    in which the two reach the console's scan_start."""
    session = win.ctl.session
    client = session.require_client()
    ps = sc.ProScan(session)
    ps.claim()
    order: list[str] = []
    pro_in, release = threading.Event(), threading.Event()
    real = client.scan_start

    def spy(*a, **kw):
        who = threading.current_thread().name
        if who == "pro":
            pro_in.set()
            release.wait(5)
        order.append(who)
        return real(*a, **kw)

    monkeypatch.setattr(client, "scan_start", spy)

    def pro_scan():
        ps.first(sc.GAME_DATA, ValueType.INT32, Compare.EXACT, 100)

    def quiet(fn):
        def run():
            with contextlib.suppress(Exception):  # the order is what is checked
                fn()

        return run

    t = threading.Thread(target=quiet(pro_scan), name="pro")
    t.start()
    assert pro_in.wait(5)
    g = threading.Thread(target=quiet(guided), name="guided")
    g.start()
    g.join(0.5)  # a guided search that honours search_lock is still waiting here
    release.set()
    t.join(5)
    g.join(5)
    return order


def test_guided_value_search_waits_for_the_pro_scan_holding_the_lock(win, monkeypatch):  # TC-23
    order = _race(win, monkeypatch, lambda: win.ctl.scanner.first_scan(100))
    assert order == ["pro", "guided"]
    assert win.ctl.session.search_owner is win.ctl.scanner


@pytest.mark.parametrize("which", ["percent", "class_search"])
def test_bug04_every_guided_search_start_honours_the_search_lock(win, monkeypatch, which):  # TC-24
    from cheatmaker.core.vtable_find import find_by_vtable

    if which == "percent":

        def guided():
            win.ctl.scanner.percent_scan(50)
    else:

        def guided():
            find_by_vtable(win.ctl.session, fg.PLAYER_VT - fg.BASE)

    order = _race(win, monkeypatch, guided)
    assert order == ["pro", "guided"], "else the console holds the Pro search for the guided one"


def test_pro_first_scan_refuses_unsigned_negative_and_keeps_the_guided_search(win, pro):  # TC-25
    win.ctl.scanner.first_scan(100)
    s = pro.scanner
    s.vtype.setCurrentIndex(s.vtype.findData(ValueType.UINT32))
    s.value.setText("-1")
    s.first_btn.click()
    assert win.ctl.scanner.active, "a refused Pro scan takes nothing"
    assert pro.status.text() and not s.scan.active


def test_frozen_row_keeps_a_newly_typed_value(win, pro, game):  # TC-26
    import struct

    lst = pro.addresses
    row = lst.add(HP_ADDR, ValueType.INT32)
    lst.table.item(0, 4).setCheckState(Qt.CheckState.Checked)
    lst.table.item(0, 3).setText("777")
    assert row.frozen == 777
    assert struct.unpack("<i", game.fake.peek(fg.PID, HP_ADDR, 4)) == (777,)


# == Re-test of c963c4e: break-it pass on the fixes ================================================
def test_rt_my_cheats_refuses_a_cheat_pro_is_building(win, tab, qtbot, monkeypatch):  # TC-32
    """pro_busy: the tick is refused with a reason and nothing is written; the busy
    mark is gone once the build ends."""
    from cheatmaker.ui import hints as guided_hints

    cheat = new_cheat(tab)
    _threaded(tab.tasks)
    gate, entered = _gated_build(monkeypatch)
    tab.editor.source.setPlainText(SOURCE_A)
    tab.editor.build_btn.click()
    assert entered.wait(5)
    try:
        assert id(cheat) in win.ctl.pro_busy
        _tick(win, cheat, True)
        assert not cheat.enabled
        assert win.status.text() == guided_hints.PRO_BUSY.format(name=cheat.name)
    finally:
        gate.set()
        qtbot.waitUntil(lambda: not tab.tasks.busy, timeout=5000)
    assert win.ctl.pro_busy == set()
    _tick(win, cheat, True)
    assert cheat.enabled


def test_rt_build_result_dropped_when_the_cheat_went_on_another_way(
    win, tab, game, qtbot, monkeypatch
):  # TC-33
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    before = entry.cave_bytes
    _threaded(tab.tasks)
    gate, entered = _gated_build(monkeypatch)
    tab.editor.source.setPlainText(SOURCE_B)
    tab.editor.build_btn.click()
    assert entered.wait(5)
    pc_compile.enable(win.ctl.session, cheat.pro, pc_compile.ProWriter(win.ctl.session))
    win.ctl.live.mark_on(cheat)  # on by a path that ignores pro_busy
    gate.set()
    qtbot.waitUntil(lambda: not tab.tasks.busy, timeout=5000)
    assert entry.cave_bytes == before and entry.source == pc_compile.DEFAULT_SOURCE
    assert tab.editor.has_draft(entry), "the dropped build's text stays a draft"


def test_rt_build_keeps_a_newer_draft(win, tab):  # TC-34
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    ed = tab.editor
    ed.source.setPlainText(SOURCE_B)  # typed after the build was started with A
    ed.built(entry, SOURCE_A)
    assert ed.draft_of(entry) == SOURCE_B
    ed.built(entry, SOURCE_B)
    assert ed.draft_of(entry) == entry.source


def test_rt_already_on_writes_nothing_and_shows_on(win, tab, game):  # TC-35
    cheat = new_cheat(tab)
    tab.on_box.click()
    win.ctl.live.turn_off(cheat, restore=False)  # the app forgot; the game didn't
    win.ctl.cheats_changed.emit()
    n = len(game.fake.writes)
    _tick(win, cheat, True)
    assert cheat.enabled and len(game.fake.writes) == n
    assert win.status.text() == f"{cheat.name} was already on in the game."


def test_rt_cave_left_from_a_gap_is_re_enabled_normally(win, tab, game):  # TC-36
    """Jump back, cave still there (the QTimer gap): an ordinary enable, jump written."""
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    tab.on_box.click()
    pc_compile.restore_sites(win.ctl.session, cheat.pro, pc_compile.ProWriter(win.ctl.session))
    win.ctl.live.turn_off(cheat, restore=False)
    assert not pc_compile.is_on(win.ctl.session, cheat.pro)
    assert (
        pc_compile.enable(win.ctl.session, cheat.pro, pc_compile.ProWriter(win.ctl.session))
        is False
    )
    assert game.fake.peek(fg.PID, fg.WRITER, 1) == b"\xe9"
    assert game.fake.peek(fg.PID, fg.BASE + entry.cave, len(entry.cave_bytes)) == (entry.cave_bytes)


def test_rt_jump_on_but_cave_gone_is_refused(win, tab, game):  # TC-37
    """Partial match the other way round: still SiteChanged, nothing written."""
    from cheatmaker.core.pro.compile import SiteChanged

    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    tab.on_box.click()
    game.fake.poke(fg.PID, fg.BASE + entry.cave, bytes(len(entry.cave_bytes)))
    win.ctl.live.turn_off(cheat, restore=False)
    assert not pc_compile.is_on(win.ctl.session, cheat.pro)
    n = len(game.fake.writes)
    with pytest.raises(SiteChanged):
        pc_compile.enable(win.ctl.session, cheat.pro, pc_compile.ProWriter(win.ctl.session))
    assert len(game.fake.writes) == n


def test_rt_refresh_probe_leaves_a_partly_on_cheat_off(win, tab, game):  # TC-38
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    tab.on_box.click()
    game.fake.poke(fg.PID, fg.WRITER, entry.off)  # the jump is gone, the cave stays
    game.fake.foreground_pid = None
    win.ctl.session.refresh_game()
    assert not cheat.enabled
    tab.select_cheat(cheat)
    tab.on_box.click()  # an ordinary enable from here
    assert cheat.enabled and game.fake.peek(fg.PID, fg.WRITER, 1) == b"\xe9"


def test_rt_pro_cheat_with_freeze_is_refused_before_any_write(win, tab, game):  # TC-39
    cheat = new_cheat(tab)
    cheat.freeze = True  # e.g. an old project
    n = len(game.fake.writes)
    _tick(win, cheat, True)
    assert not cheat.enabled and len(game.fake.writes) == n
    tab.select_cheat(cheat)
    tab.set_on(cheat, True)
    assert not cheat.enabled and len(game.fake.writes) == n


def test_rt_adopt_refuses_a_freeze_cheat_and_says_why(win, pro, game):  # TC-40
    from cheatmaker.core.pro.asm_text import FREEZE_REFUSED

    tab = pro.cheats
    c, _off = _frozen_static_cheat(win, game)
    tab.select_cheat(c)
    assert not tab.adopt_btn.isEnabled()
    assert tab.editor.error.text() == FREEZE_REFUSED


def test_bug13_cave_cleared_at_least_the_wait_after_the_last_untick(
    win, tab, qtbot, monkeypatch
):  # TC-41
    cheat = new_cheat(tab)
    tab.on_box.click()
    restores: list[float] = []
    clears: list[float] = []
    rs, cc = pc_compile.restore_sites, pc_compile.clear_caves

    def restore(*a, **k):
        restores.append(time.monotonic())
        return rs(*a, **k)

    def clear(*a, **k):
        clears.append(time.monotonic())
        return cc(*a, **k)

    monkeypatch.setattr(pc_compile, "restore_sites", restore)
    monkeypatch.setattr(pc_compile, "clear_caves", clear)
    _tick(win, cheat, False)
    qtbot.wait(150)
    _tick(win, cheat, True)
    qtbot.wait(150)
    _tick(win, cheat, False)
    qtbot.wait(1200)
    assert clears
    assert all(t - restores[-1] >= pc_compile.DISABLE_WAIT - 0.02 for t in clears)


def test_bug13_pro_on_during_the_cave_clear_gap_keeps_the_cave(
    win, tab, game, qtbot, monkeypatch
):  # TC-42 (real threads)
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    tab.on_box.click()
    _tick(win, cheat, False)  # jump back now, cave cleared 0.5 s later
    _threaded(tab.tasks)
    gate, wrote = threading.Event(), threading.Event()
    real = pc_compile.enable

    def slow(*a, **k):
        result = real(*a, **k)  # cave and jump written
        wrote.set()
        gate.wait(5)  # the worker hasn't reported back yet
        return result

    monkeypatch.setattr(pc_compile, "enable", slow)
    tab.set_on(cheat, True)
    assert wrote.wait(5)
    qtbot.wait(700)  # My cheats' timer fires meanwhile
    gate.set()
    qtbot.waitUntil(lambda: not tab.tasks.busy, timeout=5000)
    cave = game.fake.peek(fg.PID, fg.BASE + entry.cave, len(entry.cave_bytes))
    assert game.fake.peek(fg.PID, fg.WRITER, 1) == b"\xe9" and cheat.enabled
    assert cave == entry.cave_bytes, "the jump leads into a zeroed cave"


def test_bug14_pro_tab_follows_a_my_cheats_tick(win, tab):  # TC-43
    cheat = new_cheat(tab)
    _tick(win, cheat, True)
    assert cheat.enabled
    assert tab.on_box.isChecked()
    assert tab.cheat_list.item(0).text().startswith("● ")
    assert tab.editor.source.isReadOnly() and not tab.editor.build_btn.isEnabled()


def test_bug15_bytes_view_shows_a_patched_variable(win, tab):  # TC-44
    from cheatmaker.ui.pro.cave_editor import hex_lines

    cheat = new_cheat(tab)
    tab.add_var_btn.click()
    entry = cheat.pro.entries[0]
    tab.editor.build_btn.click()  # builds the layout with the new variable
    tab.vars.item(0, 2).setText("7")
    assert entry.cave_bytes.endswith(b"\x07\x00\x00\x00")
    assert tab.editor.bytes_view.toPlainText() == hex_lines(entry.cave_bytes)


# == Re-test of ac50954 ===========================================================================
def test_rt2_clear_caves_keeps_a_cave_a_jump_leads_into(win, tab, game):  # TC-45
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    tab.on_box.click()
    writer = pc_compile.ProWriter(win.ctl.session)
    n = len(game.fake.writes)
    assert pc_compile.clear_caves(win.ctl.session, cheat.pro, writer) is False
    assert len(game.fake.writes) == n
    cave = game.fake.peek(fg.PID, fg.BASE + entry.cave, len(entry.cave_bytes))
    assert cave == entry.cave_bytes


def test_rt2_guided_ticks_do_no_console_work_or_saves_on_the_ui_thread(
    win, pro, game, qtbot, monkeypatch, tmp_path
):  # TC-46
    """A My cheats tick now emits cheats_changed for every cheat. With Pro open (a
    guided cave cheat previewed, a Code page loaded), guided ticks still only write
    their own bytes on the UI thread; the Code view re-reads on the worker; nothing
    is saved."""
    from cheatmaker import formats
    from cheatmaker.core import project as pj
    from cheatmaker.core import updater as up

    tab = pro.cheats
    new_cheat(tab)
    off = game.fake.peek(fg.PID, STATIC, 4)
    nop = Cheat(
        "NOP sub",
        [Location(LocationKind.CODE_PATCH, fg.SUB_SITE - fg.BASE, b"\x90" * 6, fg.SUB_W)],
    )
    val = Cheat("Static", [Location(LocationKind.MODULE_VALUE, STATIC - fg.BASE, b"\5\0\0\0", off)])
    cave_cheat = formats.load_path(_cave_file(tmp_path)).cheats[0]
    win.ctl.ensure_file().cheats.extend([nop, val, cave_cheat])
    win.ctl.cheats_changed.emit()
    _threaded(win.ctl.tasks)
    _threaded(pro.tasks)
    qtbot.waitUntil(lambda: not win.ctl.tasks.busy and not pro.tasks.busy, timeout=10000)
    tab.select_cheat(cave_cheat)
    assert tab.preview is not None
    pro.code.go(fg.SUB_SITE)
    qtbot.waitUntil(lambda: not pro.tasks.busy, timeout=5000)
    main = threading.main_thread()
    calls: list[tuple[str, bool]] = []
    for name in ("read", "write", "maps", "foreground_app", "scan_start"):
        real = getattr(game.fake, name)

        def spy(*a, _n=name, _r=real, **k):
            calls.append((_n, threading.current_thread() is main))
            return _r(*a, **k)

        monkeypatch.setattr(game.fake, name, spy)
    saves: list[str] = []
    monkeypatch.setattr(pj, "save", lambda *a, **k: saves.append("project"))
    monkeypatch.setattr(up, "save_signatures", lambda *a, **k: saves.append("signatures"))
    for c in (nop, val):
        _tick(win, c, True)
        assert c.enabled
        _tick(win, c, False)
        assert not c.enabled
    qtbot.waitUntil(lambda: not win.ctl.tasks.busy and not pro.tasks.busy, timeout=10000)
    assert [n for n, ui in calls if ui] == ["write"] * 4, "only the ticks' own writes"
    assert saves == []
    assert not win.cheats._learning

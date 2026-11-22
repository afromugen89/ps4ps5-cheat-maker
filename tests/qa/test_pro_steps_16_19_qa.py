"""QA round for Cheat Maker Pro R1 steps 16-19: DebugSession and FakeDebugger, the Watch
tab and WatchSession, Session.claim_debugger, the access sampler, JobGuard, the
Windows build's --self-check and the O10 guided preview cache. Spec docs/spec-pro.md
(PR-3, AC-1.2, AC-1.3, AC-3.1 to AC-3.5); design docs/architecture-pro.md steps 16-19;
notes docs/implementation-notes.md. Report: qa/reports/2026-11-22-pro-steps-16-19.md.

Bugs are strict xfail tests named after their BUG id; remove the marker once fixed.
DebugSession tests run against local sockets (no PS5); the rest use FakeConsole, its
FakeDebugger and the fake game whose CPU really runs the sampler cave.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from cheatmaker import app
from cheatmaker.console import debugger as dbg
from cheatmaker.console.client import StopEvent, WatchHit
from cheatmaker.console.fake import FakeConsole, FakeStop
from cheatmaker.core import cave as cv
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.pro import access_sampler as sm
from cheatmaker.core.pro import watch_session as ws
from cheatmaker.core.pro.compile import ProWriter
from cheatmaker.core.session import GUIDED_DEBUGGER
from cheatmaker.errors import (
    DebuggerError,
    DebuggerInUse,
    FirewallBlocked,
    LockFailed,
    PortInUse,
)
from cheatmaker.settings import Settings
from cheatmaker.ui.pro import hints
from tests import fakegame as fg
from tests.ui.pro.conftest import game as game
from tests.ui.pro.conftest import notices as notices
from tests.ui.pro.conftest import pro as pro
from tests.ui.pro.conftest import questions as questions
from tests.ui.pro.conftest import warnings as warnings
from tests.ui.pro.conftest import win as win
from tests.unit.test_debugger import DebugServer, stop_packet
from tests.unit.test_debugger import session as debug_session

HP_ADDR = fg.PLAYER_AT[1] + fg.HP
AFTER_WRITER = fg.WRITER + len(fg.VMOVSS_W)


# -- helpers --------------------------------------------------------------------------------
@pytest.fixture
def server():
    s = DebugServer()
    yield s
    s.close()


@pytest.fixture
def g():
    return fg.build(1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _wait(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _hit(rip: int = AFTER_WRITER, **regs: int) -> WatchHit:
    return WatchHit(rip, {"rip": rip, "r12": fg.PLAYER_AT[1], **regs})


def _fake_watch(g, targets, stops, **kw):
    """A WatchSession on the fake game's FakeDebugger."""
    d = g.fake.open_debugger(fg.PID)
    d.stops = list(stops)
    kw.setdefault("clock", d.clock)
    kw.setdefault("seconds", 5.0)
    read = lambda a, n: g.fake.read(fg.PID, a, n)  # noqa: E731
    return d, ws.WatchSession(d, read, targets, **kw)


class _SilentServer:
    """A PS5 that takes connections but never answers anything (attach or detach)."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)

    def close(self) -> None:
        for c in self.conns:
            c.close()
        self.sock.close()


# == DebugSession (step 16) ======================================================================
def test_tc01_real_dr6_with_reserved_bits_names_the_slot(server):
    """x86 DR6 reads back with its reserved bits set (0xFFFF0FF0); only bits 0-3 count."""
    s = debug_session(server)
    s.attach()
    try:
        server.send_stop(stop_packet(0x401000, dr6=0xFFFF0FF0 | 0b0100))
        stop = s.next_stop(2.0)
        assert stop is not None and stop.slot == 2 and stop.signal == dbg.SIGTRAP
    finally:
        s.detach()


def test_tc02_detach_is_idempotent_and_a_second_attach_works(server):
    s = debug_session(server)
    s.attach()
    s.detach()
    s.detach()  # nothing more sent
    detaches = server.names().count("DEBUG_DETACH")
    assert detaches == 1 and s.state == "detached"
    s.attach()  # the port is free again
    s.detach()
    assert server.names().count("DEBUG_ATTACH") == 2


def test_tc03_commands_after_lost_raise_debuggerlost_never_connectionlost(server):
    s = debug_session(server)
    s.attach()
    try:
        server.send_stop(stop_packet(0x401000, status=0x097F))  # SIGKILL
        with pytest.raises(DebuggerError, match="game closed"):
            s.next_stop(2.0)
        with pytest.raises(DebuggerError):
            s.resume()
        assert s.state == "lost"
    finally:
        s.detach()
    assert s.state == "detached"


def test_tc04_port_in_use_sends_nothing_and_names_the_port(server):
    s = debug_session(server)
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", s.listen_port))
    blocker.listen(1)
    try:
        with pytest.raises(PortInUse, match=str(s.listen_port)):
            s.attach()
    finally:
        blocker.close()
    assert server.commands == []


def test_bug02_firewall_said_within_the_deadline_when_the_ps5_is_silent():
    """AC-1.3: "within 10 s". With no attach reply, the fresh-connection detach waits
    FRESH_DETACH_TIMEOUT (2 s) for its answer before FirewallBlocked is raised."""
    srv = _SilentServer()
    s = dbg.DebugSession(
        "127.0.0.1", srv.port, 1, listen_port=_free_port(), timeout=1.0, deadline=0.5
    )
    started = time.monotonic()
    try:
        with pytest.raises(FirewallBlocked):
            s.attach()
        took = time.monotonic() - started
    finally:
        srv.close()
    assert took <= s.deadline + 0.75, f"took {took:.2f} s for a {s.deadline} s deadline"


def test_tc06_dialled_back_but_no_attach_reply_isnt_blamed_on_the_firewall(server):
    """m-A (12ecd4e): the PS5 connected back, so the firewall is open."""
    server.dial_first = True
    server.reply_attach = False
    s = debug_session(server, deadline=0.5)
    with pytest.raises(DebuggerError) as info:
        s.attach()
    assert not isinstance(info.value, FirewallBlocked)
    assert "never answered the attach" in str(info.value)
    # the fresh detach runs on a thread since BUG-02 (the error isn't held for it)
    assert s.state == "detached" and _wait(lambda: server.fresh_detaches == 1)


# == WatchSession (step 17) =====================================================================
def test_tc10_several_dr6_bits_pick_the_watch_the_operand_touches(g):
    """m3: DR6 names slots 0 and 1; the writer's [r12+0x2ac] is HP, which is slot 1."""
    targets = [ws.WatchTarget(fg.PLAYER_AT[1], 4, "write"), ws.WatchTarget(HP_ADDR, 4, "write")]
    _d, w = _fake_watch(g, targets, [FakeStop(0, _hit(), dr6=0b11)])
    result = w.run(threading.Event())
    assert [(r.slot, r.text) for r in result.rows] == [(1, "vmovss [r12+0x2ac], xmm1")]


def test_tc11_a_failing_set_watch_ends_failed_and_detaches(g):
    targets = [ws.WatchTarget(HP_ADDR, 4, "write")]
    d, w = _fake_watch(g, targets, [])

    def broken(*_a):
        raise DebuggerError("set watch: the PS5 answered ERROR")

    d.set_watch = broken
    result = w.run(threading.Event())
    assert result.ended == ws.FAILED and "ERROR" in result.message
    assert d.state == "detached" and d.calls[-1] == ("detach",)


def test_tc12_an_error_in_the_ui_callback_still_detaches(g):
    targets = [ws.WatchTarget(HP_ADDR, 4, "write")]

    def boom(*_a):
        raise RuntimeError("UI gone")

    d, w = _fake_watch(g, targets, [FakeStop(0, _hit())], on_update=boom)
    with pytest.raises(RuntimeError):
        w.run(threading.Event())
    assert d.state == "detached"


def test_tc13_game_closing_ends_lost_and_keeps_the_rows(g):  # AC-1.2, AC-3.2
    targets = [ws.WatchTarget(HP_ADDR, 4, "write")]
    d, w = _fake_watch(g, targets, [FakeStop(0, _hit()), FakeStop(0, _hit())])
    d.game_exits_after = 2
    result = w.run(threading.Event())
    assert result.ended == ws.LOST and "game closed" in result.message
    assert result.rows and result.rows[0].count == 2 and d.state == "detached"


def test_tc14_a_game_writing_the_same_value_every_frame_is_not_a_repeat(g):
    """M4: only an execute breakpoint can fire again without the game moving on. A data
    watch traps after the instruction, so a writer that stores the same value from the
    same place every frame (same registers) must keep being counted."""
    targets = [ws.WatchTarget(HP_ADDR, 4, "write")]
    stops = [FakeStop(0, _hit()) for _ in range(40)]
    _d, w = _fake_watch(g, targets, stops)
    result = w.run(threading.Event())
    assert result.ended == ws.TIME and result.hits == 40


def test_tc18_main_link_lost_mid_watch_is_raised_after_the_detach(g):
    """m-B (12ecd4e): ConnectionLost reaches the Controller, the debugger detached first."""
    from cheatmaker.errors import ConnectionLost

    targets = [ws.WatchTarget(HP_ADDR, 4, "write")]
    d = g.fake.open_debugger(fg.PID)
    d.stops = [FakeStop(0, _hit())]

    def lost(_a, _n):
        raise ConnectionLost("not connected to the PS5")

    w = ws.WatchSession(d, lost, targets, clock=d.clock, seconds=5.0)
    with pytest.raises(ConnectionLost):
        w.run(threading.Event())
    assert d.state == "detached" and d.calls[-1] == ("detach",)


def test_tc15_an_execute_watch_firing_in_place_ends_repeats(g):
    targets = [ws.WatchTarget(fg.WRITER, 1, "execute")]
    stops = [FakeStop(0, _hit(fg.WRITER)) for _ in range(40)]
    d, w = _fake_watch(g, targets, stops)
    result = w.run(threading.Event())
    assert result.ended == ws.REPEATS and result.hits == ws.REPEAT_HITS
    assert d.state == "detached"


def test_tc16_misaligned_and_bad_targets_are_refused():
    with pytest.raises(ValueError, match=r"0x1000"):
        ws.WatchTarget(0x1002, 4, "write")
    ws.WatchTarget(0x1002, 2, "write")  # aligned for its size
    for size in (0, 3, 16):
        with pytest.raises(ValueError):
            ws.WatchTarget(0x1000, size, "write")
    with pytest.raises(ValueError):
        ws.WatchSession(object(), lambda a, n: b"", [])  # type: ignore[arg-type]
    five = [ws.WatchTarget(0x1000 + 8 * k, 8, "write") for k in range(5)]
    with pytest.raises(ValueError, match="1 to 4"):
        ws.WatchSession(object(), lambda a, n: b"", five)  # type: ignore[arg-type]


def test_tc17_stop_event_slot_is_lowest_bit():
    hit = WatchHit(1, {})
    assert StopEvent(1, "t", hit, dr6=0b1010).slot == 1
    assert StopEvent(1, "t", hit, dr6=0xFFFF0FF0).slot is None


# == Session.claim_debugger (step 17) ===========================================================
def test_tc20_one_debugger_owner_named_and_released_on_error(g):
    s = g.session
    with (
        s.claim_debugger(hints.WATCH_OWNER),
        pytest.raises(DebuggerInUse, match="Pro's Watch tab"),
        s.claim_debugger(GUIDED_DEBUGGER),
    ):
        pass
    with pytest.raises(RuntimeError), s.claim_debugger(GUIDED_DEBUGGER):
        raise RuntimeError("watch failed")
    assert s.debug_owner is None
    with s.claim_debugger(GUIDED_DEBUGGER):  # free again
        assert s.debug_owner == GUIDED_DEBUGGER


def test_tc21_guided_lock_it_refused_at_once_while_pro_watches(g):
    """The guided watch doesn't wait: it is refused at once, naming the Watch tab (the
    user guide says it "waits": see BUG-04)."""
    from cheatmaker.core.locker import find_writers

    with (
        g.session.claim_debugger(hints.WATCH_OWNER),
        pytest.raises(DebuggerInUse, match="Pro's Watch tab"),
    ):
        find_writers(g.session, HP_ADDR, 4, 0.1)
    assert g.fake.until_answers == []  # no watch was started


# == access sampler (step 18) ===================================================================
def test_bug01_a_guided_collect_cant_take_the_table_a_sample_is_using(g):
    """While a sample runs on an instruction that hasn't fired yet (a health writer
    waits for a hit), find_table hands the very same table to the guided Player only
    collect (Make it work / Lock it). The sampler zeroes it every 0.25 s, so the guided
    collect loses its objects and the sample lists the guided writer's objects."""
    used: list[int] = []
    real = cv.find_table

    def spy(session):
        used.append(real(session))
        return used[-1]

    offered: list[int | Exception] = []

    def sleep(_s: float) -> None:
        if not offered:
            try:
                offered.append(real(g.session))  # what a guided collect would get
            except LockFailed as exc:
                offered.append(exc)

    import unittest.mock

    with unittest.mock.patch.object(sm.cv, "find_table", spy):
        sm.sample(g.session, fg.WRITER, ProWriter(g.session), seconds=0.5, sleep=sleep)
    assert offered and offered[0] != used[0], "the guided collect got the sampler's table"


def test_tc30_sample_refuses_an_instruction_inside_an_enabled_cheats_span(g):
    """The hook's 5-byte jump covers the next instruction too: a cheat there is refused."""
    span = cv.hook_span(lambda a, n: g.fake.read(fg.PID, a, n), fg.WRITER)
    assert len(span) >= 1
    off = fg.WRITER - fg.BASE + 2  # inside the covered bytes
    other = Cheat("Other", [Location(LocationKind.CODE_PATCH, off, b"\x90", fg.VMOVSS_W[2:3])])
    other.enabled = True
    with pytest.raises(sm.sites.Overlap, match="Other"):
        sm.sample(g.session, fg.WRITER, ProWriter(g.session), seconds=0.25, enabled=[other])
    assert g.fake.peek(fg.PID, fg.WRITER, len(fg.VMOVSS_W)) == fg.VMOVSS_W


def test_tc31_cave_cleared_only_after_the_wait(g):
    """m7 / AC-8.5: the hook goes first, the cave is zeroed after DISABLE_WAIT."""
    order: list[str] = []
    cave = cv.find_cave(g.session, cv.CAVE_ROOM)

    def sleep(s: float) -> None:
        if s == sm.DISABLE_WAIT:
            hooked = g.fake.peek(fg.PID, fg.WRITER, len(fg.VMOVSS_W)) != fg.VMOVSS_W
            cleared = g.fake.peek(fg.PID, cave, 16) == bytes(16)
            order.append(f"wait hooked={hooked} cleared={cleared}")

    sm.sample(g.session, fg.WRITER, ProWriter(g.session), seconds=0.25, sleep=sleep)
    assert order == ["wait hooked=False cleared=False"]
    assert g.fake.peek(fg.PID, cave, cv.CAVE_ROOM) == bytes(cv.CAVE_ROOM)


def test_tc32_repeated_samples_dont_use_up_the_cave_room(g):
    first = cv.find_cave(g.session, cv.CAVE_ROOM)
    for _ in range(5):
        sm.sample(g.session, fg.WRITER, ProWriter(g.session), seconds=0.25, sleep=g.fight(1))
    assert cv.find_cave(g.session, cv.CAVE_ROOM) == first


# == Watch tab / access panel (UI) ==============================================================
@pytest.fixture
def watch(pro, game):
    tab = pro.watch
    tab.clock = game.fake.debugger.clock
    return tab


def test_tc40_misaligned_typed_address_is_refused_with_the_fix(watch, pro):
    watch.place.setText(hex(HP_ADDR + 2))
    watch.add_btn.click()
    assert watch.targets == [] and f"{HP_ADDR:#x}" in pro.status.text()


def test_tc41_every_end_path_frees_the_debugger_and_guard(watch, game, pro):
    d = game.fake.debugger
    for setup in (
        lambda: setattr(d, "back_channel", False),
        lambda: setattr(d, "port_in_use", True),
        lambda: setattr(d, "lost_after", 0),
    ):
        d.back_channel, d.port_in_use, d.lost_after = True, False, None
        setup()
        watch.targets = [ws.WatchTarget(HP_ADDR, 4, "write")]
        watch._fill_targets()
        watch.start_btn.click()
        assert not watch.watching and watch.ctl.session.debug_owner is None
        assert watch._guard._stop not in watch.ctl.before_disconnect
        assert d.state == "detached"


def test_tc42_guided_preview_cache_follows_edits(pro, win):
    """O10: a list refresh reuses the preview; a changed line makes a new one."""
    off = fg.WRITER - fg.BASE
    c = Cheat("NOP writer", [Location(LocationKind.CODE_PATCH, off, b"\x90" * 10, fg.VMOVSS_W)])
    win.ctl.ensure_file().cheats.append(c)
    win.ctl.cheats_changed.emit()
    tab = pro.cheats
    tab.select_cheat(c)
    first = tab.preview
    assert first is not None
    win.ctl.cheats_changed.emit()
    tab.select_cheat(c)
    assert tab.preview is first  # cached
    c.locations[0] = Location(LocationKind.CODE_PATCH, off, b"\xcc" * 10, fg.VMOVSS_W)
    win.ctl.cheats_changed.emit()
    tab.select_cheat(c)
    assert tab.preview is not first
    assert tab.preview.entries[0].on == b"\xcc" * 10
    c.freeze = True  # now it can't be adopted: the reason shows
    win.ctl.cheats_changed.emit()
    tab.select_cheat(c)
    assert tab.preview is None and tab.preview_why


# == settings / self-check (step 19) ============================================================
def test_tc50_watch_limits_are_clamped(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(Settings, "path", classmethod(lambda cls: tmp_path / "s.json"))
    for raw, want in (
        ((0, 0), (5, 1)),
        ((10_000, 10_000_000), (600, 5000)),
        (("60", None), (60, 500)),
        ((True, 2.5), (60, 500)),
        ((-1, -1), (5, 1)),
    ):
        (tmp_path / "s.json").write_text(
            json.dumps({"pro_watch_seconds": raw[0], "pro_watch_hits": raw[1]})
        )
        s = Settings.load()
        assert (s.pro_watch_seconds, s.pro_watch_hits) == want, raw


def test_tc51_self_check_uses_only_the_bundled_nasm(tmp_path, monkeypatch):
    import io

    monkeypatch.setattr(app, "self_check_path", lambda: tmp_path / "self-check.txt")
    monkeypatch.setattr("cheatmaker.paths.app_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("cheatmaker.paths.bundled_nasm_dir", lambda: tmp_path / "nasm")
    on_path = tmp_path / "bin"
    on_path.mkdir()
    (on_path / "nasm").write_text("")
    monkeypatch.setenv("PATH", str(on_path))
    out = io.StringIO()
    assert app.self_check(out) == 1  # PATH's nasm doesn't count
    assert "MISSING" in (tmp_path / "self-check.txt").read_text()
    from cheatmaker.paths import NASM_EXE_NAME

    (tmp_path / "nasm").mkdir()
    (tmp_path / "nasm" / NASM_EXE_NAME).write_text("")
    assert app.self_check(io.StringIO()) == 0
    assert "modules: ok" in (tmp_path / "self-check.txt").read_text()


def test_tc52_self_check_falls_back_when_the_exe_folder_is_read_only(tmp_path, monkeypatch):
    import io

    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    monkeypatch.setattr(app, "self_check_path", lambda: blocked / "self-check.txt")
    monkeypatch.setattr("cheatmaker.paths.app_data_dir", lambda: tmp_path / "data")
    out = io.StringIO()
    app.self_check(out)
    assert (tmp_path / "data" / "self-check.txt").is_file()
    assert "report:" in out.getvalue()


def test_tc53_fake_debugger_matches_the_session_on_detach():
    fake = FakeConsole()
    fake.add_process(7)
    d = fake.open_debugger(7)
    d.attach()
    d.detach()
    d.detach()
    assert d.calls.count(("detach",)) == 1 and d.state == "detached"


def test_tc54_struct_layout_of_a_watch_body():
    body = dbg.watch_body(3, 0x1000, 8, "write")
    slot, enabled, _length, _kind, addr = struct.unpack("<IIIIQ", body)
    assert (slot, enabled, addr) == (3, 1, 0x1000)
    with pytest.raises(ValueError):
        dbg.watch_body(4, 0x1000, 8, "write")


def test_bug05_a_new_game_during_a_watch_clears_the_old_games_list(watch, pro):
    """A new game while a watch runs: the watch stops; when it has ended, the old game's
    addresses and rows mustn't stay listed for the new game."""
    watch.targets = [ws.WatchTarget(HP_ADDR, 4, "write")]
    watch._fill_targets()
    watch._cancel = threading.Event()  # as if a watch were running
    watch.running_targets = list(watch.targets)
    watch.game_state_changed(True)
    assert watch._cancel.is_set()
    row = ws.InstructionRow(fg.WRITER, "vmovss [r12+0x2ac], xmm1", 0, 1, _hit(), _hit())
    watch._ended(ws.WatchResult([row], 1, ws.STOPPED, 1.0))
    assert watch.targets == [] and watch.target_table.rowCount() == 0


# == re-test of a758686: Session.claim_table on the guided paths (BUG-01 fix) ==================
def _writer():
    from cheatmaker.core.locker import Writer

    return Writer(fg.WRITER, fg.VMOVSS_W, "vmovss [r12+0x2ac], xmm1", 1)


def test_tc70_guided_collect_unchanged_and_releases_the_table(g):
    """No sample: Player only's collect works as before, and frees the table."""
    objs = cv.collect_objects(g.session, _writer(), 0.25, sleep=g.fight(1))
    assert fg.PLAYER_AT[1] in objs and g.session.table_owner is None
    assert g.fake.peek(fg.PID, fg.WRITER, len(fg.VMOVSS_W)) == fg.VMOVSS_W


def test_tc71_guided_collect_releases_the_table_on_cancel_and_on_a_lost_link(g):
    from cheatmaker.errors import ConnectionLost, ScanCancelled

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        cv.collect_objects(g.session, _writer(), 0.25, cancel, sleep=lambda _s: None)
    assert g.session.table_owner is None

    def drop(_s: float) -> None:
        raise ConnectionLost("not connected to the PS5")

    with pytest.raises(ConnectionLost):
        cv.collect_objects(g.session, _writer(), 0.25, sleep=drop)
    assert g.session.table_owner is None
    cv.collect_objects(g.session, _writer(), 0.25, sleep=g.fight(1))  # usable again


def test_tc72_guided_collect_during_a_sample_writes_nothing(g):
    from cheatmaker.core.session import SAMPLER_TABLE
    from cheatmaker.errors import TableInUse

    n = len(g.fake.writes)
    with g.session.claim_table(SAMPLER_TABLE), pytest.raises(TableInUse, match="sampler"):
        cv.collect_objects(g.session, _writer(), 0.25, sleep=g.fight(1))
    assert len(g.fake.writes) == n and g.session.table_owner is None


def test_tc73_sample_during_a_guided_collect_is_refused_not_no_room(g):
    from cheatmaker.core.session import GUIDED_COLLECT
    from cheatmaker.errors import TableInUse

    n = len(g.fake.writes)
    with g.session.claim_table(GUIDED_COLLECT), pytest.raises(TableInUse) as info:
        sm.sample(g.session, fg.WRITER, ProWriter(g.session), seconds=0.25)
    assert not isinstance(info.value, sm.NoRoomForSampler)
    assert len(g.fake.writes) == n


def test_tc74_racing_claims_one_wins_and_nothing_deadlocks(g):
    from cheatmaker.errors import TableInUse

    won, refused = [], []
    go = threading.Barrier(8)

    def claim(k: int) -> None:
        go.wait()
        try:
            with g.session.claim_table(f"owner {k}"):
                won.append(k)
                time.sleep(0.05)
        except TableInUse:
            refused.append(k)

    threads = [threading.Thread(target=claim, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads)
    assert len(won) >= 1 and len(won) + len(refused) == 8
    assert g.session.table_owner is None


@pytest.mark.xfail(
    reason="BUG-07: Find it next time drops the Player only filter silently during a sample",
    strict=True,
)
def test_bug07_find_it_next_time_says_the_sampler_has_the_table(g):
    """The guide (10.3) says the Player only collect "can't start: it says the sampler is
    using the space". refind._field_filter swallows TableInUse like any collect failure
    and learns the hook with no filter (it may catch an enemy's object next run)."""
    from types import SimpleNamespace

    from cheatmaker.core import refind
    from cheatmaker.core.session import SAMPLER_TABLE
    from cheatmaker.errors import TableInUse

    cand = SimpleNamespace(address=fg.WRITER, code=fg.VMOVSS_W, text="vmovss", hits=1)
    with g.session.claim_table(SAMPLER_TABLE), pytest.raises(TableInUse):
        refind._field_filter(g.session, cand, fg.PLAYER_AT[1], 0.25, None)


@pytest.mark.xfail(
    reason="BUG-07: Bring them over drops the hook way as 'no spare room' during a sample",
    strict=True,
)
def test_bug07_bring_them_over_doesnt_call_a_busy_table_no_room(g):
    """carry.collect_classes turns TableInUse (a LockFailed) into CarryFailed(NO_ROOM);
    the dialog then drop_hook()s the object's hook way for the new version."""
    from types import SimpleNamespace

    from cheatmaker.core import carry
    from cheatmaker.core.session import SAMPLER_TABLE

    hook = SimpleNamespace(filter=SimpleNamespace(kind="field"))
    site = SimpleNamespace(offset=fg.WRITER - fg.BASE, code=fg.VMOVSS_W, text="vmovss")
    item = SimpleNamespace(old=SimpleNamespace(hook=hook), site=site, obj=SimpleNamespace(id="hp"))
    with g.session.claim_table(SAMPLER_TABLE):
        try:
            carry.collect_classes(g.session, item, seconds=0.25, sleep=g.fight(1))  # type: ignore[arg-type]
        except carry.CarryFailed as exc:
            assert exc.reason != carry.NO_ROOM, "a busy table reported as 'no spare room'"

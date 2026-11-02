"""QA for v3 "Find it again" (AC-11..AC-14), 2026-11-02. See
qa/reports/2026-11-02-find-it-again.md for the test plan (TC-xx) and bugs (BUG-xx).

Tests marked ``xfail(strict=True)`` reproduce an open bug: they fail today and must
start passing (then drop the marker) once the bug is fixed.
"""

from __future__ import annotations

import json
import struct

import iced_x86 as ix
import pytest
from PySide6.QtCore import Qt

from cheatmaker.console.fake import PROT_R, PROT_W, PROT_X, FakeConsole
from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import cave as cv
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.locker import Writer
from cheatmaker.core.scanner import Scanner
from cheatmaker.core.session import Session
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import FindFailed, LockFailed, ProjectFileError
from cheatmaker.settings import Settings
from cheatmaker.ui.cheats_view import COL_ON
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.find_again import LearnDialog
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests import fakegame as fg


# -- helpers -----------------------------------------------------------------------------
class Console:
    def __init__(self) -> None:
        self.game = fg.build(1, connect=False)

    def restart(self, run: int, **kw) -> fg.Game:
        self.game = fg.build(run, connect=False, **kw)
        return self.game


@pytest.fixture
def console():
    return Console()


@pytest.fixture
def win(qtbot, console, monkeypatch):
    monkeypatch.setattr(rf.ah, "WATCH_SECONDS", 1)
    monkeypatch.setattr(rf.ah, "PROVE_SECONDS", 1)
    monkeypatch.setattr(rf, "SLOT_WAIT_S", 0.5)
    ctl = Controller(lambda ip, port: console.game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.cheats.finder.ask_confirm = lambda text: True
    w.cheats.finder.sleep = lambda _s: None
    yield w
    ctl.shutdown()


def connect(w) -> None:
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()


def add_health_cheat(w, game: fg.Game) -> Cheat:
    raw = struct.pack("<f", 999.0)
    c = Cheat(
        "Infinite Health",
        [Location(LocationKind.SESSION_ADDRESS, game.health_addr, raw, raw)],
        freeze=True,
        value_type=ValueType.FLOAT,
    )
    w.ctl.ensure_file().cheats.append(c)
    w.ctl.cheats_changed.emit()
    return c


def learn_ui(w, cheat) -> LearnDialog:
    dlg = LearnDialog(w.ctl, w.cheats.finder, cheat, w)
    dlg.start_btn.click()
    return dlg


def set_on(w, row: int, on: bool) -> None:
    w.cheats.table.item(row, COL_ON).setCheckState(
        Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
    )


def learn_core(game: fg.Game, **kw) -> rf.Learned:
    return rf.learn(
        game.session,
        game.health_addr,
        ValueType.FLOAT,
        "Health",
        "obj1",
        watch_seconds=1,
        prove_seconds=1,
        **kw,
    )


def write_project(doc) -> None:
    p = pj.project_path(fg.TITLE, fg.VERSION)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")


HOOKED = fg.VMOVSS_R  # the reader the fake game's learn picks


# == TC-05/06/07, BUG-01: the hook must come out on every disconnect (AC-11.2) ==========
def test_tc05_reconnect_still_takes_the_hook_out_on_close(win, console):
    connect(win)
    learn_ui(win, add_health_cheat(win, console.game))
    assert console.game.fake.peek(fg.PID, fg.READER, 1) == b"\xe9"
    connect(win)  # the player presses Connect again (same PS5, same game)
    win.ctl.shutdown()  # close the app
    assert console.game.fake.peek(fg.PID, fg.READER, 10) == HOOKED


def test_tc06_refresh_from_home_screen_keeps_the_hook_ours(win, console):
    connect(win)
    learn_ui(win, add_health_cheat(win, console.game))
    fake = console.game.fake
    fake.foreground_pid = None  # PS button: home screen in front, game still running
    win.connect_view.refresh_btn.click()
    fake.foreground_pid = fg.PID  # back in the game
    win.connect_view.refresh_btn.click()
    win.ctl.shutdown()
    assert fake.peek(fg.PID, fg.READER, 10) == HOOKED


def test_tc07_lost_link_then_reconnect_takes_the_hook_out_on_close(win, console):
    connect(win)
    learn_ui(win, add_health_cheat(win, console.game))
    win.ctl.handle_lost()  # Wi-Fi blip: hook can't be removed now (expected)
    connect(win)
    win.ctl.shutdown()  # but this time it can, and it's ours
    assert console.game.fake.peek(fg.PID, fg.READER, 10) == HOOKED


# == TC-14, BUG-02: freeze follows the new object while the old one is still alive ======
def _move_keeping_old(game: fg.Game, new: int) -> int:
    """UE4-style move: the new pawn is built elsewhere; the old one is pending kill and
    keeps its vtable and fields until the garbage collector runs (up to ~60 s)."""
    old = game.player
    game.fake.poke(fg.PID, new, game.fake.peek(fg.PID, old, 0x300))
    game.touched = [new if o == old else o for o in game.touched]
    game.player = new
    return old


def test_tc14_freeze_follows_the_new_object_when_the_old_is_still_alive():
    game = fg.build(1)
    obj = learn_core(game).obj
    ah.install(game.session, obj)
    tracker = rf.Tracker(game.session, obj, game.player)
    assert tracker.current() == game.player
    new = fg.HEAP + 0x6000
    _move_keeping_old(game, new)
    game.fake.read(fg.PID, fg.BASE, 1)  # a frame: the game's code now handles `new`
    assert ah.read_slot(game.session, obj.hook)[0] == new, "the slot already knows"
    assert tracker.current() in (new, None), "never keep writing the old object"


def test_tc13_freeze_follows_the_move_when_the_old_object_is_wiped():
    game = fg.build(1)
    obj = learn_core(game).obj
    ah.install(game.session, obj)
    tracker = rf.Tracker(game.session, obj, game.player, clock=lambda: 100.0)
    game.move_player(fg.HEAP + 0x6000)
    game.fake.read(fg.PID, fg.BASE, 1)
    assert tracker.current() == fg.HEAP + 0x6000


# == TC-19, BUG-03: the PC-off routine must stay in place when turned off ===============
def test_tc19_pc_off_cheat_off_leaves_its_routine_for_threads_inside(win, console):
    connect(win)
    dlg = learn_ui(win, add_health_cheat(win, console.game))
    fake = console.game.fake
    keep = fg.BASE + ah.keep_cave(win.ctl.project.objects[0].hook)
    dlg.pc_off_btn.click()
    set_on(win, 1, True)
    routine = fake.peek(fg.PID, keep, 32)
    assert routine != bytes(32)
    set_on(win, 1, False)
    assert fake.peek(fg.PID, keep, 32) == routine, "a thread inside it must find valid code"


# == TC-17/18/33: the encoded routine ==================================================
SITE, CAVE, SLOT = 0x2B7B43F, 0x65D7000, 0x7F3FFF0
FORMS = {
    "writer vmovss [r12+2ACh],xmm1": bytes.fromhex("C4C17A118C24AC020000"),
    "reader vmovss xmm0,[r12+2ACh]": bytes.fromhex("C4C17A108424AC020000"),
    "reader mov rax,[rax+2ACh]": bytes.fromhex("488b80ac020000"),
    "writer sub [rax+2ACh],ecx": bytes.fromhex("2988ac020000"),
    "reader mov eax,[rbp+2ACh]": bytes.fromhex("8b85ac020000"),
    "reader mov eax,[r15+2ACh]": bytes.fromhex("418b87ac020000"),
}
_info = ix.InstructionInfoFactory()
_WRITE = {ix.OpAccess.WRITE, ix.OpAccess.READ_WRITE, ix.OpAccess.COND_WRITE}


def _routine(code: bytes, orig: bytes):
    insns = list(ix.Decoder(64, code, ip=CAVE))
    return insns, [i for i in insns if code[i.ip - CAVE : i.ip - CAVE + i.len] == orig]


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("vtable", [0x5A1B2C0, 0x8_0040_1000])  # PS5 low base / above 2 GiB
@pytest.mark.parametrize("filt", ["vtable", "field", "none"])
def test_tc17_capture_routine_changes_nothing_the_game_can_see(form, vtable, filt):
    """Before the original instruction runs, the routine only moves rsp (red zone),
    saves/restores flags, and pushes/pops a scratch register: no general register
    the game uses is changed. (Also run on a real x86 emulator, see the report.)"""
    orig = FORMS[form]
    span = cv.decode_at(orig + b"\x90" * 8, SITE)[:1]
    target = cv.writer_target(Writer(SITE, orig, form, 1))
    code = cv.capture_code(span, target, SLOT, CAVE, filt, vtable=vtable, field=8, field_value=7)
    insns, copies = _routine(code, orig)
    assert len(copies) == 1, "the original instruction runs on the 'other' path"
    pushed: set = set()
    for ins in insns:
        if ins.ip == copies[0].ip:
            break
        if ins.mnemonic == ix.Mnemonic.PUSH:
            pushed.add(ins.op0_register)
        elif ins.mnemonic == ix.Mnemonic.POP:
            pushed.discard(ins.op0_register)
        for u in _info.info(ins).used_registers():
            if u.access in _WRITE and u.register != ix.Register.RSP:
                assert u.register in pushed or ins.mnemonic == ix.Mnemonic.POP, (
                    f"{ins} changes {u.register}"
                )
        for m in _info.info(ins).used_memory():
            if m.access in _WRITE and m.base not in (ix.Register.RSP, ix.Register.NONE):
                raise AssertionError(f"{ins} writes game memory before the original runs")
    assert not pushed, "every scratch push is popped"
    mn = [i.mnemonic for i in insns]
    assert mn.count(ix.Mnemonic.PUSHFQ) == 1 and mn.count(ix.Mnemonic.POPFQ) == 1
    k = insns.index(copies[0])
    assert insns[k - 2].mnemonic == ix.Mnemonic.POPFQ, "flags restored right before"
    assert insns[k - 1].mnemonic == ix.Mnemonic.LEA and insns[k - 1].memory_displacement == 128
    back = insns[k + 1]
    assert back.mnemonic == ix.Mnemonic.JMP and back.near_branch_target == SITE + len(orig)
    slot_refs = {i.memory_displacement for i in insns if i.memory_base == ix.Register.RIP}
    assert slot_refs == {SLOT, SLOT + 8}


def test_tc18_keep_routine_on_a_read_modify_write_keeps_the_games_flags():
    """`sub [rax+2ACh],ecx ; jle dead`: if the routine skips the `sub`, the game's next
    branch sees the flags from before it. Either refuse such a writer for PC-off, or
    still run a flag-setting copy before re-storing the value."""
    orig = FORMS["writer sub [rax+2ACh],ecx"]
    span = cv.decode_at(orig + b"\x90" * 8, SITE)[:1]
    target = cv.writer_target(Writer(SITE, orig, "", 1))
    try:
        code = cv.capture_code(
            span, target, SLOT, CAVE, "vtable", vtable=0x5A1B2C0, value=struct.pack("<i", 999)
        )
    except LockFailed:
        return  # refusing is a fix too
    insns, copies = _routine(code, orig)
    first_popfq = next(i for i in insns if i.mnemonic == ix.Mnemonic.POPFQ)
    keep_path = [i for i in insns if first_popfq.ip <= i.ip < copies[-1].ip]
    assert any(i.mnemonic == ix.Mnemonic.SUB for i in keep_path)


def test_tc33_wide_vtable_routine_is_installed_and_filled_on_a_high_module():
    """PS4-style module above 4 GiB: vtable > 2 GiB, routine and slot RIP-relative."""
    base = 0x8_0000_0000
    fake = FakeConsole()
    fake.add_process(5, "eboot.bin", "CUSA00001", "Old Game", "01.00")
    code = bytearray(b"\xcc" * 0x1000) + bytearray(0x3000)
    code[0x200:0x20A] = HOOKED
    fake.add_region(5, "eboot.bin", base, bytes(code), PROT_R | PROT_X)
    fake.add_region(5, "eboot.bin", base + 0x4000, 0x1000, PROT_R | PROT_W)
    fake.add_region(5, "eboot.bin", base + 0x5000, 0x1000, PROT_R)
    s = Session(lambda ip, port: fake)
    s.connect("1.2.3.4")
    s.refresh_game()
    assert s.game.title_id == "CUSA00001" and s.game.module_base == base
    from cheatmaker.console.client import WatchHit

    player = 0x9_0000_0100
    fake.add_region(5, "[anon]", 0x9_0000_0000, 0x1000, PROT_R | PROT_W)
    obj = bytearray(0x300)
    obj[0:8] = struct.pack("<Q", base + 0x5100)
    obj[fg.HP : fg.HP + 4] = struct.pack("<f", 100.0)
    fake.poke(5, player, bytes(obj))
    fake.read_hits[player + fg.HP] = [WatchHit(base + 0x20A, {"r12": player})]
    learned = rf.learn(
        s, player + fg.HP, ValueType.FLOAT, "Health", "obj1", watch_seconds=0, prove_seconds=0
    )
    assert learned.methods == ["hook_slot", "vtable_scan"]
    assert learned.obj.vtable == 0x5100
    assert ah.install(s, learned.obj) == "installed"
    hook = learned.obj.hook
    routine = fake.peek(5, base + hook.cave, 64)
    text = [str(i) for i in ix.Decoder(64, routine, ip=base + hook.cave)][:6]
    assert any("mov" in t and "800005100" in t.lower() for t in text), text
    jmp = fake.peek(5, base + hook.hook, 5)
    assert jmp[0] == 0xE9
    assert base + hook.hook + 5 + struct.unpack("<i", jmp[1:])[0] == base + hook.cave
    assert pj.project_path("CUSA00001", "01.00").name == "CUSA00001_01.00.cmproj"


# == TC-25/26/27/28/29: .cmproj robustness ==============================================
GOOD_HEAD = {"format": "cheatmaker-project", "title_id": fg.TITLE, "version": fg.VERSION}


@pytest.mark.xfail(reason="BUG-05: malformed .cmproj raises a raw Python error", strict=True)
@pytest.mark.parametrize(
    "doc",
    [
        [],  # top level not an object
        {**GOOD_HEAD, "objects": ["player"]},
        {**GOOD_HEAD, "objects": [{"id": "o", "fields": [{"name": "H", "off": "0", "type": 5}]}]},
    ],
    ids=["list", "object-is-string", "type-is-number"],
)
def test_tc25_malformed_project_is_a_plain_project_error(doc):
    write_project(doc)
    with pytest.raises(ProjectFileError):
        pj.load(pj.project_path(fg.TITLE, fg.VERSION))


@pytest.mark.parametrize(
    "text", ["", "{", '{"format": "cheatmaker-project", "format_version": 9}', "\xff\xfe"]
)
def test_tc27_broken_or_newer_project_is_a_plain_project_error(text):
    write_project(text)
    with pytest.raises(ProjectFileError):
        pj.load(pj.project_path(fg.TITLE, fg.VERSION))


def test_tc29_minimal_old_project_without_optional_keys_loads():
    write_project({**GOOD_HEAD, "objects": [{"id": "o", "vtable": None}]})
    p = pj.load(pj.project_path(fg.TITLE, fg.VERSION))
    assert p.objects[0].label == "o" and p.cheats == [] and p.history == []


@pytest.mark.xfail(reason="BUG-05: bad saved cheat value crashes the connect flow", strict=True)
def test_tc26_bad_cheat_value_in_project_does_not_break_connect(win, console, qtbot):
    write_project(
        {
            **GOOD_HEAD,
            "objects": [
                {
                    "id": "obj1",
                    "label": "H",
                    "vtable": None,
                    "fields": [{"name": "H", "off": "0x2ac", "type": "float"}],
                }
            ],
            "cheats": [
                {"name": "H", "object": "obj1", "field": "H", "action": "freeze", "value": "abc"}
            ],
        }
    )
    with qtbot.captureExceptions() as errors:
        connect(win)
    assert errors == [], "a plain message, not an exception in the Qt event loop"


@pytest.mark.xfail(
    reason="BUG-06: unreadable/newer .cmproj overwritten without a backup", strict=True
)
def test_tc28_unreadable_project_is_kept_before_a_new_one_is_saved(win, console):
    newer = json.dumps({**GOOD_HEAD, "format_version": 2, "objects": [], "extra": "data"})
    write_project(newer)
    connect(win)
    assert "can't be read" in win.status.text()
    learn_ui(win, add_health_cheat(win, console.game))
    folder = pj.project_path(fg.TITLE, fg.VERSION).parent
    kept = [p for p in folder.iterdir() if p.read_text(encoding="utf-8") == newer]
    assert kept, "the old file (made by a newer Cheat Maker) must survive somewhere"


# == TC-30, BUG-07: simple games stay simple ============================================
@pytest.mark.xfail(reason="BUG-07: short-chain game still gets a code hook", strict=True)
def test_tc30_short_chain_game_never_gets_a_code_hook(win, console):
    console.restart(1, chain=True)
    connect(win)
    dlg = LearnDialog(win.ctl, win.cheats.finder, add_health_cheat(win, console.game), win)
    dlg.pointers.setChecked(True)
    dlg.start_btn.click()
    assert "chain" in dlg.learned.methods, "a short memory path is there"
    assert console.game.fake.peek(fg.PID, fg.READER, 10) == HOOKED, "no game-code change"


def test_tc02_static_value_learns_without_watching_or_patching():
    from tests.conftest import STATIC_ADDR, build_fake

    fake = build_fake("CUSA01234")  # PS4 title id
    s = Session(lambda ip, port: fake)
    s.connect("1.2.3.4")
    s.refresh_game()
    learned = rf.learn(s, STATIC_ADDR, ValueType.INT32, "Skill points", "obj1")
    assert learned.methods == ["static"] and learned.obj.hook is None
    assert fake.watched == [] and fake.writes == []
    out = rf.find_again(s, learned.obj)
    assert out.found and out.method == "static"


# == TC-31, BUG-08: a value that grows must still be found =============================
@pytest.mark.xfail(reason="BUG-08: plausible range capped at 10x / 10,000", strict=True)
def test_tc31_counter_that_grows_past_ten_thousand_is_still_found():
    game = fg.build(1)
    obj = learn_core(game).obj  # learned at 100.0
    ah.install(game.session, obj)
    game.fake.read(fg.PID, fg.BASE, 1)
    game.set_health(25_000.0)  # e.g. currency/Obolites after a good run
    out = rf.find_again(game.session, obj, sleep=lambda _s: None, slot_wait=0)
    assert out.found, "same object, same class, the value simply grew"


# == TC-23: CheatRunner loaded the PC-off routine before the PC connected (C10d) =========
def test_tc23_cheatrunner_pc_off_routine_is_used_and_not_patched_again():
    run1 = fg.build(1)
    obj = learn_core(run1).obj
    pc_off = ah.hook_cheat(run1.session, obj, "keep", struct.pack("<f", 999.0))
    run2 = fg.build(2)
    for loc in pc_off.locations:  # CheatRunner applied the lines at boot
        run2.fake.poke(fg.PID, fg.BASE + loc.offset, loc.on)
    before = run2.fake.peek(fg.PID, fg.READER, 5)
    with pytest.raises(FindFailed, match="Another cheat"):
        ah.install(run2.session, obj)
    assert run2.fake.peek(fg.PID, fg.READER, 5) == before and run2.fake.writes == []
    run2.fake.read(fg.PID, fg.BASE, 1)
    out = rf.find_again(run2.session, obj, sleep=lambda _s: None)
    assert out.found and out.method == "hook_slot" and out.address == run2.player
    ah.remove(run2.session, obj, lambda _s: None)  # disconnect: not ours, left alone
    assert run2.fake.peek(fg.PID, fg.READER, 5) == before


# == TC-34, BUG-09: Find it again's memory search vs the player's open search ===========
@pytest.mark.xfail(reason="BUG-09: memory search silently drops the player's search", strict=True)
def test_tc34_find_again_search_does_not_drop_an_open_search_silently():
    game = fg.build(2)
    scanner = Scanner(game.session)
    assert scanner.first_scan(100.0, ValueType.FLOAT) > 0
    obj = learn_core(fg.build(1)).obj
    out = rf.find_again(game.session, obj, use_hook=False, sleep=lambda _s: None)
    assert out.found
    if scanner.active:  # still shows "N matches": narrowing down must work
        scanner.next_scan(Compare.UNCHANGED)

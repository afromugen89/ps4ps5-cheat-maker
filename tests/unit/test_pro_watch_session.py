"""Pro Watch: find what writes / accesses (spec PR-3, architecture-pro step 17) on the
FakeDebugger; nothing waits for real (the debugger's FakeClock moves instead)."""

from __future__ import annotations

import threading

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.console.fake import PROT_R, PROT_X, FakeConsole, FakeStop
from cheatmaker.core.pro import watch_session as ws
from cheatmaker.errors import FirewallBlocked

PID = 0x50
CODE = 0x400000
WRITER = CODE + 0x100  # mov [rbx+8], eax  (3 bytes)
READER = CODE + 0x103  # mov ecx, [rbx+0x10]  (3 bytes)
VALUE = 0x20000048


@pytest.fixture
def fake() -> FakeConsole:
    f = FakeConsole()
    f.add_process(PID, title_id="PPSA01234")
    code = bytearray(0x1000)
    code[0x100:0x106] = bytes.fromhex("8943088b4b10")
    f.add_region(PID, "executable", CODE, bytes(code), PROT_R | PROT_X)
    return f


def _hit(rip: int, rax: int = 1) -> WatchHit:
    return WatchHit(rip, {"rip": rip, "rax": rax, "rbx": VALUE - 8})


def _session(fake: FakeConsole, targets=None, **kw) -> ws.WatchSession:
    dbg = fake.open_debugger(PID)
    read = lambda a, n: fake.read(PID, a, n)  # noqa: E731
    targets = targets or [ws.WatchTarget(VALUE, 4, "write")]
    return ws.WatchSession(dbg, read, targets, clock=dbg.clock, **kw)


def test_hits_are_grouped_by_the_instruction_before_rip(fake):  # AC-3.1
    fake.debugger.stops = [FakeStop(0, _hit(WRITER + 3, rax=k)) for k in range(3)]
    result = _session(fake).run(threading.Event())
    (row,) = result.rows
    assert (row.address, row.text, row.count, row.slot) == (WRITER, "mov [rbx+8], eax", 3, 0)
    assert row.first.reg("rax") == 0 and row.last.reg("rax") == 2
    assert result.hits == 3 and result.ended == ws.TIME


def test_time_limit_ends_clears_and_detaches(fake):  # AC-3.2
    result = _session(fake, seconds=60).run(threading.Event())
    d = fake.debugger
    assert result.ended == ws.TIME and result.rows == []
    assert result.seconds == pytest.approx(60, abs=0.3)
    assert d.calls[-5:] == [*[("clear", k) for k in range(4)], ("detach",)]
    assert ("set", 0, VALUE, 4, "write") in d.calls and d.state == "detached"


def test_hit_limit_ends_the_session(fake):
    fake.debugger.flood(600, 0, _hit(WRITER + 3))
    result = _session(fake, max_hits=500).run(threading.Event())
    assert result.ended == ws.HITS and result.hits == 500 and result.rows[0].count == 500
    assert fake.debugger.calls.count(("resume",)) == 500


def test_stop_ends_at_once(fake):
    cancel = threading.Event()
    cancel.set()
    assert _session(fake).run(cancel).ended == ws.STOPPED
    assert fake.debugger.calls[-1] == ("detach",)


def test_game_closing_keeps_the_rows_and_says_why(fake):  # AC-1.2
    d = fake.debugger
    d.stops = [FakeStop(0, _hit(WRITER + 3)) for _ in range(3)]
    d.game_exits_after = 2
    n = len(fake.writes)
    result = _session(fake).run(threading.Event())
    assert result.ended == ws.LOST and "game closed" in result.message
    assert result.rows[0].count == 2 and len(fake.writes) == n
    assert d.calls[-1] == ("detach",) and d.state == "detached"


def test_two_watches_keep_their_own_rows(fake):
    d = fake.debugger
    d.stops = [FakeStop(0, _hit(WRITER + 3)), FakeStop(1, _hit(READER + 3))]
    targets = [ws.WatchTarget(VALUE, 4, "write"), ws.WatchTarget(VALUE + 8, 4, "access")]
    rows = _session(fake, targets).run(threading.Event()).rows
    assert {(r.address, r.slot) for r in rows} == {(WRITER, 0), (READER, 1)}


def test_dr6_naming_no_slot_goes_to_the_only_watch_or_none(fake):
    d = fake.debugger
    d.stops = [FakeStop(0, _hit(WRITER + 3), dr6=0)]
    assert _session(fake).run(threading.Event()).rows[0].slot == 0
    d.stops = [FakeStop(0, _hit(WRITER + 3), dr6=0)]
    targets = [ws.WatchTarget(VALUE, 4, "write"), ws.WatchTarget(VALUE + 8, 4, "write")]
    assert _session(fake, targets).run(threading.Event()).rows[0].slot is None


def test_execute_watch_names_the_instruction_at_rip(fake):
    fake.debugger.stops = [FakeStop(0, _hit(READER))]
    rows = _session(fake, [ws.WatchTarget(READER, 1, "execute")]).run(threading.Event()).rows
    assert (rows[0].address, rows[0].text) == (READER, "mov ecx, [rbx+0x10]")


def test_rip_in_unreadable_memory_is_shown_as_unknown(fake):
    fake.debugger.stops = [FakeStop(0, _hit(0x7000_0000))]
    (row,) = _session(fake).run(threading.Event()).rows
    assert (row.address, row.text) == (0x7000_0000, ws.UNKNOWN_TEXT)


def test_live_updates_and_a_final_one(fake):
    fake.debugger.stops = [FakeStop(0, _hit(WRITER + 3)) for _ in range(5)]
    seen: list[tuple[int, int]] = []
    s = _session(fake, seconds=2, on_update=lambda rows, hits, _t: seen.append((len(rows), hits)))
    s.run(threading.Event())
    assert seen[-1] == (1, 5) and len(seen) >= 2


def test_attach_failure_is_raised_and_nothing_is_left_attached(fake):  # AC-1.3
    fake.debugger.back_channel = False
    with pytest.raises(FirewallBlocked):
        _session(fake).run(threading.Event())
    assert fake.debugger.state == "detached"


def test_targets_are_checked():
    with pytest.raises(ValueError):
        ws.WatchTarget(VALUE, 3, "write")
    with pytest.raises(ValueError):
        ws.WatchTarget(VALUE, 4, "execute")
    with pytest.raises(ValueError, match="1 to 4"):
        ws.WatchSession(None, None, [ws.WatchTarget(VALUE, 4, "write")] * 5)  # type: ignore[arg-type]


def test_registers_text_lists_known_registers_in_order():
    assert ws.registers_text(WatchHit(5, {"rip": 5, "rax": 0x10})) == "rax=0x10 rip=0x5"
    assert ws.registers_text(WatchHit(5)) == ws.UNKNOWN_TEXT


def test_value_target_of_a_cheat():
    from cheatmaker.core.cheats import Cheat, Location, LocationKind

    heap = Location(LocationKind.SESSION_ADDRESS, VALUE, b"\x63\0\0\0", b"\0\0\0\0")
    code = Location(LocationKind.CODE_PATCH, 0x100, b"\x90", b"\x89")
    learned = Location(LocationKind.SESSION_ADDRESS, 0, b"\1\0\0\0", b"\0\0\0\0", anchor=("o", "f"))
    assert ws.value_target(Cheat("h", [code, heap]), CODE) == ws.WatchTarget(VALUE, 4, "write")
    static = Location(LocationKind.MODULE_VALUE, 0x40, b"\1\0", b"\0\0")
    assert ws.value_target(Cheat("s", [static]), CODE) == ws.WatchTarget(CODE + 0x40, 2, "write")
    assert ws.value_target(Cheat("c", [code]), CODE) is None
    assert ws.value_target(Cheat("l", [learned]), CODE) is None


def test_every_hit_is_handed_on(fake):
    fake.debugger.stops = [FakeStop(0, _hit(READER, rax=k)) for k in range(3)]
    seen = []
    target = [ws.WatchTarget(READER, 1, "execute")]
    _session(fake, target, on_hit=lambda stop: seen.append(stop.hit.reg("rax"))).run(
        threading.Event()
    )
    assert seen == [0, 1, 2]

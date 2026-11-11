import struct
import threading
import time

import pytest

from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.templates import BY_KEY, make_cheat
from cheatmaker.core.values import ValueType
from cheatmaker.errors import WriteFailed
from tests.conftest import GOLD_ADDR, HEALTH_ADDR, HEAP_BASE, MODULE_BASE, PID


def _health_cheat():
    return make_cheat(
        BY_KEY["infinite_health"],
        LocationKind.SESSION_ADDRESS,
        HEALTH_ADDR,
        ValueType.INT32,
        struct.pack("<i", 100),
    )


def _i32(fake, addr):
    return struct.unpack("<i", fake.peek(PID, addr, 4))[0]


def test_set_once(session, fake):  # AC-4.1
    c = make_cheat(
        BY_KEY["set_number"],
        LocationKind.SESSION_ADDRESS,
        GOLD_ADDR,
        ValueType.INT32,
        struct.pack("<i", 2450),
        value=99999,
    )
    LiveCheats(session, Freezer(session)).set_once(c)
    assert _i32(fake, GOLD_ADDR) == 99999


def test_freeze_tick_restores_value(session, fake):  # AC-4.2
    fz = Freezer(session)
    live = LiveCheats(session, fz)
    c = _health_cheat()
    live.turn_on(c)
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 40))  # took damage
    fz.tick()
    assert _i32(fake, HEALTH_ADDR) == 100 and c.enabled
    live.turn_off(c)
    fz.stop()
    assert not c.enabled and str(id(c)) not in fz.keys


def test_freeze_thread_runs_at_least_10hz(session, fake):
    fz = Freezer(session)
    fz.freeze("k", [(PID, HEALTH_ADDR, struct.pack("<i", 100))])
    time.sleep(0.35)
    fz.stop()
    assert len(fake.writes) >= 3


def test_toggle_restores_off_bytes_for_code_patch(session, fake):  # AC-5.3
    loc = Location(LocationKind.CODE_PATCH, 0x100, b"\x90\x90", b"\x89\x08")
    c = Cheat("Lock", [loc])
    live = LiveCheats(session, Freezer(session))
    live.turn_on(c)
    assert fake.peek(PID, MODULE_BASE + 0x100, 2) == b"\x90\x90"
    live.turn_off(c)
    assert fake.peek(PID, MODULE_BASE + 0x100, 2) == b"\x89\x08"


def test_refresh_after_value_edit(session, fake):
    live = LiveCheats(session, Freezer(session))
    c = _health_cheat()
    live.turn_on(c)
    c.set_value(500)
    live.refresh(c)
    assert _i32(fake, HEALTH_ADDR) == 500
    live.freezer.stop()


def test_write_failure_raises(session, fake):  # AC-4.3
    fake.fail_writes = True
    with pytest.raises(WriteFailed):
        LiveCheats(session, Freezer(session)).turn_on(_health_cheat())


def test_write_failure_during_freeze_reports_and_unfreezes(session, fake):
    errors = []
    fz = Freezer(session, on_error=lambda k, e: errors.append(k))
    fz.freeze("k", [(PID, HEALTH_ADDR, b"\0\0\0\0")])
    fake.fail_writes = True
    fz.tick()
    fz.stop()
    assert errors == ["k"] and "k" not in fz.keys


def test_connection_drop_stops_and_marks_off(session, fake):  # AC-1.4
    lost = threading.Event()
    fz = Freezer(session, on_lost=lost.set, hz=50)
    live = LiveCheats(session, fz)
    c = _health_cheat()
    live.turn_on(c)
    fake.disconnect()
    assert lost.wait(5), "on_lost must fire within 5 s"
    off = live.mark_all_off()
    assert off == [c] and not c.enabled and not fz.keys
    fz.stop()


def test_freeze_logs_address_and_bytes(session, caplog):
    caplog.set_level("INFO", logger="cheatmaker.core.freezer")
    fr = Freezer(session)
    fr.freeze("k", [(PID, HEALTH_ADDR, b"\x9f\x86\x01\x00")])
    fr.stop()
    assert f"action=freeze key=k addr={HEALTH_ADDR:#x} bytes=9f860100" in caplog.text


def test_mark_on_writes_nothing(session, fake):
    live = LiveCheats(session, Freezer(session))
    c = Cheat("Kept", [Location(LocationKind.CODE_PATCH, 0x100, b"\x90", b"\xcc")])
    writes = len(fake.writes)
    live.mark_on(c)
    assert c.enabled and live.active[id(c)] is c and len(fake.writes) == writes
    frozen = _health_cheat()
    frozen.freeze = True
    with pytest.raises(ValueError):
        live.mark_on(frozen)


# -- combined cheats with learned parts (spec-restart-combined AC-3, AC-4) --------------
OBJ_AT = HEAP_BASE + 0x400  # where the learned object is "found" this run


class _Tracker:
    """Stands in for refind.Tracker: where the object is now (None = not verified)."""

    def __init__(self, obj, address):
        self.obj, self.address, self.calls = obj, address, 0

    def current(self):
        self.calls += 1
        return self.address


def _parts_cheat(fake):
    from cheatmaker.core import project as pj
    from cheatmaker.core.cheats import combine

    obj = pj.ObjectSpec(
        "obj1",
        "Health",
        None,
        [pj.Field("health", 0x10, ValueType.FLOAT), pj.Field("fraction", 0x14, ValueType.FLOAT)],
    )
    obj2 = pj.ObjectSpec("obj2", "Shield", None, [pj.Field("shield", 0x8, ValueType.FLOAT)])

    def found(name, obj_id, v):
        raw = struct.pack("<f", v)
        return Cheat(
            name,
            [Location(LocationKind.SESSION_ADDRESS, 0, raw, raw)],
            freeze=True,
            value_type=ValueType.FLOAT,
            anchor=(obj_id, name),
        )

    patch = Cheat("god", [Location(LocationKind.CODE_PATCH, 0x100, b"\x90\x90", b"\x89\x08")])
    c = combine(
        [
            found("health", "obj1", 3.0),
            found("fraction", "obj1", 1.0),
            found("shield", "obj2", 9.0),
            patch,
        ],
        name="Health",
    )
    trackers = {"obj1": _Tracker(obj, OBJ_AT), "obj2": _Tracker(obj2, OBJ_AT + 0x100)}
    return c, trackers


def _f32(fake, addr):
    return struct.unpack("<f", fake.peek(PID, addr, 4))[0]


def _live_with(session, cheat_trackers):
    live = LiveCheats(session, Freezer(session))
    live.trackers.update(cheat_trackers)
    return live


def test_parts_are_written_only_at_their_checked_place(session, fake):  # AC-3
    c, trackers = _parts_cheat(fake)
    live = _live_with(session, trackers)
    live.turn_on(c)
    assert _f32(fake, OBJ_AT + 0x10) == 3.0 and _f32(fake, OBJ_AT + 0x14) == 1.0
    assert _f32(fake, OBJ_AT + 0x108) == 9.0
    assert fake.peek(PID, MODULE_BASE + 0x100, 2) == b"\x90\x90"
    assert trackers["obj1"].calls == 1, "each object is checked once per write"
    live.freezer.stop()


def test_lost_part_stops_the_tick_with_its_name_and_writes_nothing(session, fake):  # AC-3
    from cheatmaker.errors import FindFailed

    c, trackers = _parts_cheat(fake)
    trackers["obj2"].address = None
    live = _live_with(session, trackers)
    before = len(fake.writes)
    with pytest.raises(FindFailed) as err:
        live.turn_on(c)
    assert str(err.value) == (
        "Health: shield isn't found right now, so nothing was changed. Press Find it again."
    )
    assert len(fake.writes) == before and not c.enabled
    del live.trackers["obj2"]  # never found this run at all: the same
    with pytest.raises(FindFailed, match="shield isn't found"):
        live.set_once(c)
    assert len(fake.writes) == before


def test_freeze_skips_a_lost_part_and_keeps_the_others(session, fake, caplog):  # AC-4
    caplog.set_level("INFO", logger="cheatmaker.core.freezer")
    c, trackers = _parts_cheat(fake)
    live = _live_with(session, trackers)
    fz = live.freezer
    live.turn_on(c)
    key = str(id(c))
    trackers["obj2"].address = None  # shield's object stopped checking out
    fake.poke(PID, OBJ_AT + 0x10, struct.pack("<f", 1.5))  # took a hit
    fake.poke(PID, OBJ_AT + 0x108, struct.pack("<f", 4.0))
    fz.tick()
    fz.tick()
    assert _f32(fake, OBJ_AT + 0x10) == 3.0, "the other parts keep being written"
    assert _f32(fake, OBJ_AT + 0x108) == 4.0, "the lost part isn't written"
    assert caplog.text.count(f"action=freeze key={key} part=shield paused") == 1
    assert key not in fz.paused, "the cheat itself keeps going"
    trackers["obj2"].address = OBJ_AT + 0x200  # found again elsewhere
    fz.tick()
    assert _f32(fake, OBJ_AT + 0x208) == 9.0
    assert f"action=freeze key={key} part=shield resumed" in caplog.text
    fz.stop()


def test_freeze_pauses_the_cheat_when_every_part_is_lost(session, fake):
    c, trackers = _parts_cheat(fake)
    c.locations = c.learned_parts  # no fixed part left to write
    live = _live_with(session, trackers)
    live.turn_on(c)
    for t in trackers.values():
        t.address = None
    live.freezer.tick()
    assert str(id(c)) in live.freezer.paused
    live.freezer.stop()


def test_turn_off_restores_fixed_parts_and_leaves_learned_ones(session, fake):  # AC-4
    c, trackers = _parts_cheat(fake)
    live = _live_with(session, trackers)
    live.turn_on(c)
    live.turn_off(c)
    assert fake.peek(PID, MODULE_BASE + 0x100, 2) == b"\x89\x08"
    assert _f32(fake, OBJ_AT + 0x10) == 3.0, "learned parts are left as they are"
    assert not c.enabled and str(id(c)) not in live.freezer.keys
    fake.poke(PID, MODULE_BASE + 0x100, b"\x90\x90")
    trackers["obj1"].address = trackers["obj2"].address = None
    live.turn_off(c)  # nothing found now: no error, the fixed part is still restored
    assert fake.peek(PID, MODULE_BASE + 0x100, 2) == b"\x89\x08"
    live.freezer.stop()


def test_learned_parts_never_write_to_0_or_their_offset(session, fake):  # design hazard
    c, trackers = _parts_cheat(fake)
    for loc in c.learned_parts:
        assert loc.offset == 0  # unused: the tracker holds the address
    c.learned_parts[0].offset = 0x40  # even if it held something, it is never used
    live = _live_with(session, trackers)
    live.turn_on(c)
    trackers["obj2"].address = None
    live.freezer.tick()
    live.turn_off(c)
    trackers["obj2"].address = 0  # "nothing there" is never an address
    live.freezer.freeze_tracked("k", lambda: live._part_writes(c, True, strict=False))
    live.freezer.tick()
    live.freezer.stop()
    addrs = {a for _p, a, _d in fake.writes}
    assert addrs and 0 not in addrs and 0x40 not in addrs and 8 not in addrs
    learned = {OBJ_AT + 0x10, OBJ_AT + 0x14, OBJ_AT + 0x108}
    assert addrs <= learned | {MODULE_BASE + 0x100}


def test_a_tick_after_turn_off_leaves_no_paused_part_behind(session, fake):  # review 7
    c, trackers = _parts_cheat(fake)
    live = _live_with(session, trackers)
    live.turn_on(c)
    resolve = live.freezer._entries[str(id(c))]
    live.turn_off(c)
    trackers["obj2"].address = None
    resolve()  # a tick that was already running when the cheat went off
    assert live._part_paused == {}
    live.freezer.stop()


def test_fixed_writes_lists_fixed_entries_by_prefix(session):
    fz = Freezer(session)
    fz.freeze("find:0x10", [(0x10, 4, b"\x01\x00\x00\x00")])
    fz.freeze("cheat:1", [(0x20, 4, b"\x02\x00\x00\x00")])
    fz.freeze_tracked("find:0x30", lambda: None)
    try:
        assert fz.fixed_writes("find:") == {"find:0x10": [(0x10, 4, b"\x01\x00\x00\x00")]}
        assert set(fz.fixed_writes()) == {"find:0x10", "cheat:1"}
    finally:
        fz.stop()

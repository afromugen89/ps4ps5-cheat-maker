"""Golden regression for Make it work without a pool (spec-linked-values L17).

Each scenario runs today's builder on the fake game and records, in order, every
console request, every sleep, the crash-notes file after each step and the builder's
log lines. The record must stay exactly as it is: linked values may only add behaviour
behind a pool, and the one visible change allowed is ``places=1 linked=none`` on the
``build_result`` lines (removed here before comparing).

Set ``UPDATE_GOLDEN=1`` to write the record again after a change that is meant to
alter it (then review the diff of tests/fixtures/builder_golden.json).
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import build_notes as notes
from cheatmaker.core import builder as b
from cheatmaker.core.cave import Effect
from cheatmaker.errors import BuildFailed, ScanCancelled
from tests import fakegame as fg

GOLDEN = Path(__file__).parent.parent / "fixtures" / "builder_golden.json"
REQUESTS = ("read", "write", "maps", "processes", "foreground_app", "watch", "watch_writes")
LOGGERS = ("cheatmaker.core.builder", "cheatmaker.core.build_notes")
NEW_RESULT_FIELDS = re.compile(r" places=\d+ linked=\w+")
FORM_ADDR = {"float": fg.HP, "int32": fg.HP32, "int64": fg.HP64}
NOP_SITE = fg.BASE + 0x500
INDEXED = bytes.fromhex("898C88B0020000")  # mov [rax+rcx*4+2B0h],ecx: switch-off only


def _arg(v: object) -> str:
    if isinstance(v, bytes | bytearray):
        return bytes(v).hex()
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return hex(v)
    if isinstance(v, float | str):
        return repr(v)
    return type(v).__name__


class Recorder:
    """Every console request and sleep of one build, in order."""

    def __init__(self, g: fg.Game, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.now = 1000.0
        self.game_events: deque[Callable[[], object] | None] = deque()
        for name in REQUESTS:
            real = getattr(g.fake, name)
            monkeypatch.setattr(g.fake, name, self._wrap(name, real))

    def _wrap(self, name: str, real: Callable) -> Callable:
        def call(*args, **kw):
            parts = [_arg(a) for a in args] + [f"{k}={_arg(v)}" for k, v in sorted(kw.items())]
            self.events.append(" ".join([name, *parts]))
            return real(*args, **kw)

        return call

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.events.append(f"sleep {seconds!r}")
        self.now += seconds
        if self.game_events:
            ev = self.game_events.popleft()
            if ev is not None:
                ev()

    def step(self, label: str) -> None:
        """The notes file as it is after a step (times zeroed: they are wall clock)."""
        path = notes.notes_path()
        if not path.exists():
            self.events.append(f"notes {label} <none>")
            return
        text = re.sub(r'"time": [0-9.e+-]+', '"time": 0', path.read_text(encoding="utf-8"))
        self.events.append(f"notes {label} {text}")


def _setup(monkeypatch, form="float", effect=Effect.NEVER_DOWN, **kw):
    g = fg.build(capture_ticks=False)
    f = fg.FORMS[form]
    addr = g.player + FORM_ADDR[form]
    g.fake.write_hits[addr] = [WatchHit(f.end)] * 3
    rec = Recorder(g, monkeypatch)
    bl = b.Builder(
        g.session,
        addr,
        f.vtype,
        effect,
        name="Health",
        sleep=rec.sleep,
        clock=rec.clock,
        **kw,
    )
    return g, rec, bl


def _fight(g: fg.Game, rounds: int) -> list[Callable[[], object]]:
    out: list[Callable[[], object]] = []
    for _ in range(rounds):
        out.append(lambda: g.hit(g.player, g.health()))
        for e in g.enemies:
            out.append(lambda e=e: g.hit(e, g.value(e)))
    return out


def _try(rec: Recorder, bl: b.Builder, cancel: threading.Event | None = None) -> b.TryResult:
    out = bl.try_candidate(bl.next_candidate(), cancel)
    rec.step(f"try={out.result.value}")
    return out.result


def _finalize(rec: Recorder, bl: b.Builder, cancel: threading.Event | None = None) -> None:
    res = bl.finalize(cancel)
    rec.events.append(f"cheat {res.cheat.name!r} {res.after_restart!r}")
    rec.events += [
        f"line {loc.offset:#x} {loc.on.hex()} {loc.off.hex()}" for loc in res.cheat.locations
    ]
    rec.step("finalize")


# -- scenarios: each returns the recorder after a whole build -------------------------
def never_down(mp) -> Recorder:
    g, rec, bl = _setup(mp)
    bl.watch()
    rec.step("watch")
    rec.game_events.extend([lambda: g.hit(g.player, 60.0), None, lambda: g.hit(g.player, 120.0)])
    assert _try(rec, bl) is b.TryResult.PASS
    _finalize(rec, bl)
    return rec


def always_full(mp) -> Recorder:
    g, rec, bl = _setup(mp, "int64", Effect.ALWAYS_FULL)
    g.set_value(g.player, 90_000, "int64")
    bl.watch()
    rec.step("watch")
    rec.game_events.extend([lambda: g.hit(g.player, 5_000, "int64")])
    assert _try(rec, bl) is b.TryResult.PASS
    _finalize(rec, bl)
    return rec


def half_damage(mp) -> Recorder:
    g, rec, bl = _setup(mp, "float", Effect.HALF)
    bl.watch()
    rec.step("watch")
    rec.game_events.extend([lambda: g.hit(g.player, 40.0), None, lambda: g.hit(g.player, 50.0)])
    assert _try(rec, bl) is b.TryResult.PASS
    _finalize(rec, bl)
    return rec


def flags_fix_int32(mp) -> Recorder:
    g, rec, bl = _setup(mp, "int32")
    bl.watch()
    rec.step("watch")
    rec.game_events.extend([lambda: g.hit(g.player, -5, "int32")])
    assert _try(rec, bl) is b.TryResult.PASS
    _finalize(rec, bl)
    return rec


def only_me(mp) -> Recorder:
    g, rec, bl = _setup(mp, only_me=True)
    bl.watch()
    rec.step("watch")
    rec.game_events.extend(_fight(g, 3))
    assert bl.collect() is None
    rec.step("collect")
    rec.game_events.clear()
    rec.game_events.extend(
        [
            lambda: g.hit(g.player, 50.0),
            lambda: g.hit(g.enemies[0], 200.0),
            lambda: g.hit(g.enemies[1], 200.0),
        ]
    )
    assert _try(rec, bl) is b.TryResult.PASS
    _finalize(rec, bl)
    return rec


def only_me_by_field(mp) -> Recorder:
    g, rec, bl = _setup(mp, only_me=True)
    for e in g.enemies:
        g.fake.poke(fg.PID, e, struct.pack("<Q", fg.PLAYER_VT))
    bl.watch()
    rec.game_events.extend(_fight(g, 3))
    assert bl.collect() is None
    rec.step("collect")
    rec.game_events.clear()
    rec.game_events.extend([lambda: g.hit(g.player, 50.0)])
    assert _try(rec, bl) is b.TryResult.NEEDS_ANSWER
    assert bl.answer(b.Question.ENEMIES, b.Answer.YES).result is b.TryResult.PASS
    rec.step("answer")
    _finalize(rec, bl)
    return rec


def switch_off(mp) -> Recorder:
    g, rec, bl = _setup(mp, "int32")
    g.fake.poke(fg.PID, NOP_SITE, INDEXED)
    g.fake.write_hits[bl.address] = [WatchHit(NOP_SITE + len(INDEXED))] * 3
    bl.watch()
    assert _try(rec, bl) is b.TryResult.NEEDS_ANSWER
    assert bl.answer(b.Question.GOT_HIT, b.Answer.YES).result is b.TryResult.PASS
    rec.step("answer")
    _finalize(rec, bl)
    return rec


def effect_broken(mp) -> Recorder:
    g, rec, bl = _setup(mp)
    bl.watch()
    rec.game_events.extend([lambda: g.hit(g.player, 60.0), lambda: g.set_health(50.0)])
    assert _try(rec, bl) is b.TryResult.FAIL
    assert bl.next_candidate() is None
    bl.log_end(b.NOTHING_WORKED)
    return rec


def no_hit_twice(mp) -> Recorder:
    _g, rec, bl = _setup(mp)
    bl.watch()
    assert _try(rec, bl) is b.TryResult.NO_HIT
    assert _try(rec, bl) is b.TryResult.NO_HIT
    bl.log_end(b.NOTHING_WORKED)
    return rec


def cancel_in_test(mp) -> Recorder:
    _g, rec, bl = _setup(mp)
    bl.watch()
    cancel = threading.Event()
    rec.game_events.extend([None, None, cancel.set])
    assert _try(rec, bl, cancel) is b.TryResult.CANCELLED
    bl.log_end("Your game is unchanged.")
    assert bl.close() is b.Rollback.NOTHING
    return rec


def game_gone(mp) -> Recorder:
    g, rec, bl = _setup(mp)
    bl.watch()
    rec.game_events.extend([lambda: g.fake.remove_process(fg.PID)])
    assert _try(rec, bl) is b.TryResult.LOST
    return rec


def link_lost(mp) -> Recorder:
    g, rec, bl = _setup(mp)
    bl.watch()
    rec.game_events.extend([g.fake.disconnect])
    assert _try(rec, bl) is b.TryResult.LOST
    return rec


def close_after_pass(mp) -> Recorder:
    g, rec, bl = _setup(mp)
    bl.watch()
    rec.game_events.extend([lambda: g.hit(g.player, 50.0)])
    assert _try(rec, bl) is b.TryResult.PASS
    assert bl.close() is b.Rollback.RESTORED
    rec.step("close")
    return rec


def finalize_cancelled(mp) -> Recorder:
    g, rec, bl = _setup(mp)
    bl.watch()
    rec.game_events.extend([lambda: g.hit(g.player, 50.0)])
    assert _try(rec, bl) is b.TryResult.PASS
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        bl.finalize(cancel)
    rec.step("finalize")
    return rec


def nothing_changed(mp) -> Recorder:
    g, rec, bl = _setup(mp)
    g.fake.write_hits[bl.address] = []
    with pytest.raises(BuildFailed):
        bl.watch()
    rec.step("watch")
    return rec


SCENARIOS: dict[str, Callable[[pytest.MonkeyPatch], Recorder]] = {
    f.__name__: f
    for f in (
        never_down,
        always_full,
        half_damage,
        flags_fix_int32,
        only_me,
        only_me_by_field,
        switch_off,
        effect_broken,
        no_hit_twice,
        cancel_in_test,
        game_gone,
        link_lost,
        close_after_pass,
        finalize_cancelled,
        nothing_changed,
    )
}


def _logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        "log " + NEW_RESULT_FIELDS.sub("", r.getMessage())
        for r in caplog.records
        if r.name in LOGGERS
    ]


def _load() -> dict[str, list[str]]:
    return json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_no_pool_build_is_byte_for_byte_today(name, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    rec = SCENARIOS[name](monkeypatch)
    got = rec.events + _logs(caplog)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        data = _load()
        data[name] = got
        GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return
    want = _load().get(name)
    assert want is not None, f"no golden record for {name}: run with UPDATE_GOLDEN=1"
    for i, (w, g) in enumerate(zip(want, got, strict=False)):
        assert g == w, f"{name}: event {i} differs"
    assert len(got) == len(want), f"{name}: {len(got)} events, golden has {len(want)}"


def test_every_scenario_has_a_record():
    assert set(_load()) == set(SCENARIOS)

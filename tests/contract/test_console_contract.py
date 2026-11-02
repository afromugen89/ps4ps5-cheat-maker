"""Contract tests: the same cases for every ConsoleClient.

They run on FakeConsole here. To run them on a real PS5 (console test S1), set
CHEATMAKER_PS5_IP and start a game whose memory you know; see docs/console-tests.md.
"""

import os
import struct

import pytest

from cheatmaker.console.client import ConsoleClient
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import ConnectionLost, ScanCancelled, WriteFailed
from tests.conftest import GOLD_ADDR, HEALTH_ADDR, PID, build_fake


def _clients():
    yield pytest.param("fake", id="fake")
    if os.environ.get("CHEATMAKER_PS5_IP"):
        yield pytest.param("real", id="real", marks=pytest.mark.real_console)


@pytest.fixture(params=list(_clients()))
def client(request):
    if request.param == "fake":
        return build_fake()
    from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient  # pragma: no cover

    return Ps5DebugNgClient.connect(os.environ["CHEATMAKER_PS5_IP"])  # pragma: no cover


def test_is_a_console_client(client):
    assert isinstance(client, ConsoleClient)


def test_version_and_game(client):
    assert client.version()
    fg = client.foreground_app()
    assert fg is not None and fg.title_id[:4] in ("PPSA", "CUSA")
    assert any(p.pid == fg.pid for p in client.processes())


def test_maps_read_write(client):
    if not hasattr(client, "peek"):
        pytest.skip("write test only on fake")
    assert client.maps(PID)
    client.write(PID, GOLD_ADDR, struct.pack("<i", 99999))
    assert struct.unpack("<i", client.read(PID, GOLD_ADDR, 4))[0] == 99999


def test_write_unmapped_fails(client):
    if not hasattr(client, "peek"):
        pytest.skip("fake only")
    with pytest.raises(WriteFailed):
        client.write(PID, 0x10, b"\0")


def test_scan_exact_then_next(client):
    if not hasattr(client, "peek"):
        pytest.skip("fake only")
    regions = [m for m in client.maps(PID) if m.writable]
    assert client.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100) == 2
    client.poke(PID, HEALTH_ADDR, struct.pack("<i", 90))
    assert client.scan_next(Compare.EXACT, 90) == 1
    [hit] = client.scan_results(10)
    assert hit.address == HEALTH_ADDR


def test_scan_unknown_then_decreased(client):
    if not hasattr(client, "peek"):
        pytest.skip("fake only")
    regions = [m for m in client.maps(PID) if m.writable]
    n = client.scan_start(PID, regions, ValueType.INT32, Compare.UNKNOWN, None)
    assert n > 100
    client.poke(PID, HEALTH_ADDR, struct.pack("<i", 50))
    assert client.scan_next(Compare.DECREASED, None) == 1
    assert client.scan_next(Compare.UNCHANGED, None) == 1


def test_scan_cancel_and_progress(client):
    if not hasattr(client, "peek"):
        pytest.skip("fake only")
    import threading

    regions = [m for m in client.maps(PID) if m.writable]
    seen = []
    client.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100, seen.append)
    assert seen and seen[-1] == 1.0
    ev = threading.Event()
    ev.set()
    with pytest.raises(ScanCancelled):
        client.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100, None, ev)


def test_disconnect_raises_connection_lost(client):
    if not hasattr(client, "peek"):
        pytest.skip("fake only")
    client.disconnect()
    with pytest.raises(ConnectionLost):
        client.read(PID, GOLD_ADDR, 4)


def test_from_dump_roundtrip(tmp_path):
    import json

    (tmp_path / "heap.bin").write_bytes(struct.pack("<i", 42) + b"\0" * 12)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "game": {"pid": 5, "title_id": "CUSA00001", "title": "Dumped", "version": "01.02"},
                "regions": [{"name": "[anon]", "start": "0x1000", "prot": 3, "file": "heap.bin"}],
                "writers": {"0x1000": ["0x400010"]},
            }
        )
    )
    from cheatmaker.console.fake import FakeConsole

    fake = FakeConsole.from_dump(tmp_path)
    assert fake.foreground_app().title_id == "CUSA00001"
    assert fake.read(5, 0x1000, 4) == struct.pack("<i", 42)
    assert fake.watch_writes(5, 0x1000, 4, 1) == [0x400010]


def test_watch_returns_hits_with_registers(client):
    if not hasattr(client, "peek"):
        pytest.skip("fake only: needs the player to make the value change")
    from cheatmaker.console.client import WatchHit

    reader = WatchHit(0x400200, {"rax": HEALTH_ADDR - 0x40, "rip": 0x400200})
    client.read_hits[HEALTH_ADDR] = [reader]
    writes = client.watch(PID, HEALTH_ADDR, 4, "write", 1.0)
    assert writes and all(h.regs == {} for h in writes), "writers recorded without registers"
    access = client.watch(PID, HEALTH_ADDR, 4, "access", 1.0)
    assert access[0] == reader and len(access) == len(writes) + 1
    assert client.watch_writes(PID, HEALTH_ADDR, 4, 1.0) == [h.rip for h in writes]
    with pytest.raises(ValueError):
        client.watch(PID, HEALTH_ADDR, 4, "exec", 1.0)

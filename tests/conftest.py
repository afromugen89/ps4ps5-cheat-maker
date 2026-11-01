from __future__ import annotations

import os
import struct

import pytest

from cheatmaker.console.fake import PROT_R, PROT_W, PROT_X, FakeConsole
from cheatmaker.core.session import Session

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PID = 77
MODULE_BASE = 0x400000
CODE_SIZE = 0x1000
DATA_BASE = MODULE_BASE + CODE_SIZE  # module .data, values here are saveable
HEAP_BASE = 0x2000_0000
HEALTH_ADDR = HEAP_BASE + 0x40  # int32 100
GOLD_ADDR = HEAP_BASE + 0x80  # int32 2450
BAR_ADDR = HEAP_BASE + 0xC0  # float 87.4
STATIC_ADDR = DATA_BASE + 0x10  # int32 5 (skill points in module data)
WRITER_ADDR = MODULE_BASE + 0x100  # "mov [rax],ecx" that writes health


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CHEATMAKER_HOME", str(tmp_path / "home"))


def build_fake(title_id: str = "PPSA01234") -> FakeConsole:
    fake = FakeConsole()
    fake.add_process(PID, "eboot.bin", title_id, "Test Game", "01.000")
    code = bytearray(b"\xcc" * CODE_SIZE)
    code[0x100:0x102] = b"\x89\x08"  # mov [rax],ecx
    fake.add_region(PID, "eboot.bin", MODULE_BASE, bytes(code), PROT_R | PROT_X)
    data = bytearray(0x100)
    data[0x10:0x14] = struct.pack("<i", 5)
    fake.add_region(PID, "eboot.bin", DATA_BASE, bytes(data), PROT_R | PROT_W)
    heap = bytearray(0x1000)
    heap[0x40:0x44] = struct.pack("<i", 100)
    heap[0x80:0x84] = struct.pack("<i", 2450)
    heap[0xC0:0xC4] = struct.pack("<f", 87.4)
    heap[0x200:0x204] = struct.pack("<i", 100)  # a decoy 100
    fake.add_region(PID, "[anon]", HEAP_BASE, bytes(heap), PROT_R | PROT_W)
    fake.writers[HEALTH_ADDR] = [WRITER_ADDR + 2, WRITER_ADDR + 2]
    return fake


@pytest.fixture
def fake() -> FakeConsole:
    return build_fake()


@pytest.fixture
def session(fake) -> Session:
    s = Session(lambda ip, port: fake)
    s.connect("192.168.1.50")
    s.refresh_game()
    return s

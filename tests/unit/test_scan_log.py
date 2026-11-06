"""Scan files: every scan writes its matches (hex address + value) to its own file."""

from __future__ import annotations

import struct
from datetime import datetime
from pathlib import Path

from cheatmaker.console.client import ScanHit
from cheatmaker.core.scan_log import ScanLog, fmt_address, fmt_scan_value, safe_name
from cheatmaker.core.scanner import Scanner
from cheatmaker.core.values import Compare, ValueType, make_scale
from cheatmaker.paths import scans_dir
from cheatmaker.settings import Settings
from tests.conftest import BAR_ADDR, GOLD_ADDR, HEALTH_ADDR, PID

DECOY_ADDR = HEALTH_ADDR - 0x40 + 0x200  # conftest's second int32 100


def scan_files(root: Path) -> list[Path]:
    return sorted(root.rglob("scan-*.txt"))


def rows(path: Path) -> list[tuple[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    body = lines[lines.index("Address\tValue") + 1 :]
    return [tuple(line.split("\t")) for line in body]  # type: ignore[misc]


def header(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("Address\tValue")[0]


def test_first_and_next_scan_each_get_their_own_file(session, fake):
    sc = Scanner(session)
    sc.first_scan(100)
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 90))
    sc.next_scan(Compare.EXACT, 90)
    first, second = scan_files(scans_dir())
    assert first.parent == second.parent  # one folder for the search
    assert (first.name, second.name) == ("scan-001.txt", "scan-002.txt")
    assert rows(first) == [(fmt_address(HEALTH_ADDR), "100"), (fmt_address(DECOY_ADDR), "100")]
    assert rows(second) == [(fmt_address(HEALTH_ADDR), "90")]
    h = header(second)
    assert "scan 2 (next scan)" in h and "Search so far: 100 > 90" in h
    assert "Test Game (PPSA01234, version 01.000)" in h and "Matches: 1" in h
    assert "Listed: 1 (all)" in h and "Whole number (4 bytes)" in h
    assert "PPSA01234_01.000" in str(first)
    assert sc.scan_log.last_file == second


def test_address_is_hex():
    assert fmt_address(0x2000_0040) == "0x0020000040"
    assert fmt_address(0x7F_FFFF_FFF0) == "0x7FFFFFFFF0"


def test_compare_scans_write_a_file_each(session, fake):
    sc = Scanner(session)
    sc.first_scan(None)
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 50.0))
    sc.next_scan(Compare.DECREASED)
    sc.next_scan(Compare.UNCHANGED)
    files = scan_files(scans_dir())
    assert [f.name for f in files] == ["scan-001.txt", "scan-002.txt", "scan-003.txt"]
    assert rows(files[1]) == [(fmt_address(BAR_ADDR), "50")]
    assert "This scan: unchanged" in header(files[2])


def test_start_over_makes_a_new_folder(session):
    sc = Scanner(session)
    sc.first_scan(100)
    sc.first_scan(2450)
    a, b = scan_files(scans_dir())
    assert a.parent != b.parent and b.name == "scan-001.txt"
    assert rows(b) == [(fmt_address(GOLD_ADDR), "2450")]


def test_same_second_searches_dont_share_a_folder(session):
    sc = Scanner(session)
    fixed = datetime(2026, 11, 6, 10, 45, 12)
    sc.scan_log.now = lambda: fixed
    sc.first_scan(100)
    sc.first_scan(100)
    a, b = scan_files(scans_dir())
    assert a.parent.name == "2026-11-06_10-45-12"
    assert b.parent.name == "2026-11-06_10-45-12-2"


def test_zero_matches_still_writes_a_file(session):
    sc = Scanner(session)
    sc.first_scan(123456789)
    [f] = scan_files(scans_dir())
    assert rows(f) == [] and "Matches: 0" in header(f)


def test_big_scans_list_only_the_first_ones(session):
    sc = Scanner(session)
    sc.scan_log.max_lines = 5
    count = sc.first_scan(None)
    assert count > 5
    [f] = scan_files(scans_dir())
    assert len(rows(f)) == 5
    assert f"Listed: the first 5 of {count:,}" in header(f)


def test_unknown_with_range_is_one_scan_one_file(session):
    sc = Scanner(session)
    count = sc.first_scan(None, value_range=(80, 90))
    [f] = scan_files(scans_dir())
    assert (fmt_address(BAR_ADDR), "87.4") in rows(f)
    assert f"Matches: {count:,}" in header(f)


def test_float_values_keep_their_precision():
    assert fmt_scan_value(ValueType.FLOAT, struct.pack("<f", 87.4)) == "87.4"
    assert fmt_scan_value(ValueType.FLOAT, struct.pack("<f", 0.123456)) == "0.123456"
    assert fmt_scan_value(ValueType.DOUBLE, struct.pack("<d", 1234567.891)) == "1234567.891"
    assert fmt_scan_value(ValueType.INT64, struct.pack("<q", -5)) == "-5"
    assert fmt_scan_value(ValueType.INT32, b"\x01") == "?"  # short read


def test_stand_still_writes_a_file_per_round(session):
    sc = Scanner(session)
    sc.first_scan(None)
    sc.stand_still(rounds=3, interval_s=0, sleep=lambda _s: None)
    assert len(scan_files(scans_dir())) == 1 + len(sc.history) - 1


def test_scale_is_noted(session):
    sc = Scanner(session)
    sc.first_scan(0.874, scale=make_scale("fraction", 100))
    [f] = scan_files(scans_dir())
    assert "Game stores it as:" in header(f)


def test_turned_off_writes_nothing(session):
    sc = Scanner(session)
    sc.scan_log.enabled = False
    sc.first_scan(100)
    sc.next_scan(Compare.EXACT, 100)
    assert scan_files(scans_dir()) == [] and sc.scan_log.last_file is None


def test_a_file_that_cant_be_written_never_fails_the_scan(session, tmp_path, caplog):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    sc = Scanner(session)
    sc.scan_log.root = lambda: blocker  # mkdir under a file fails
    assert sc.first_scan(100) == 2
    assert sc.next_scan(Compare.EXACT, 100) == 2
    assert sc.scan_log.last_file is None and "action=scan_file reason=" in caplog.text


def test_write_without_a_search_does_nothing(tmp_path):
    log_ = ScanLog(root=lambda: tmp_path)
    assert log_.write(ValueType.INT32, 1, [ScanHit(0x10, b"\0\0\0\0")], ["1"]) is None
    assert list(tmp_path.iterdir()) == []


def test_safe_name():
    assert safe_name("PPSA01285_03.000.005") == "PPSA01285_03.000.005"
    assert safe_name('a<b>:"c"/d') == "a_b_c_d"
    assert safe_name("..") == "game"


def test_settings_default_on_and_wired(qtbot, fake):
    from cheatmaker.ui.controller import Controller
    from cheatmaker.ui.tasks import TaskRunner

    s = Settings()
    assert s.save_scan_files and s.scan_file_max == 100_000
    s.save_scan_files, s.scan_file_max = False, 7
    ctl = Controller(lambda ip, port: fake, s, TaskRunner(sync=True))
    assert not ctl.scanner.scan_log.enabled and ctl.scanner.scan_log.max_lines == 7
    ctl.shutdown()


def test_fetching_matches_for_the_file_never_fails_the_scan(session, fake, monkeypatch, caplog):
    from cheatmaker.errors import ReadFailed

    sc = Scanner(session)
    monkeypatch.setattr(fake, "scan_results", lambda _n: (_ for _ in ()).throw(ReadFailed("x")))
    assert sc.first_scan(100) == 2 and sc.active
    assert scan_files(scans_dir()) == [] and "action=scan_file_fetch" in caplog.text


def test_a_lost_link_while_fetching_still_reaches_the_ui(session, fake, monkeypatch):
    import pytest

    from cheatmaker.errors import ConnectionLost

    sc = Scanner(session)
    monkeypatch.setattr(fake, "scan_results", lambda _n: (_ for _ in ()).throw(ConnectionLost()))
    with pytest.raises(ConnectionLost):
        sc.first_scan(100)


def test_close_floats_print_differently():
    a = struct.pack("<f", 0.1)
    b = struct.pack("<I", struct.unpack("<I", a)[0] + 1)
    assert fmt_scan_value(ValueType.FLOAT, a) == "0.1"
    assert fmt_scan_value(ValueType.FLOAT, b) != "0.1"
    assert fmt_scan_value(ValueType.FLOAT, struct.pack("<f", float("nan"))) == "nan"


def test_bad_scan_settings_fall_back(tmp_path):
    import json

    Settings.path().parent.mkdir(parents=True, exist_ok=True)
    Settings.path().write_text(json.dumps({"scan_file_max": "5000", "save_scan_files": "no"}))
    s = Settings.load()
    assert s.scan_file_max == 100_000 and s.save_scan_files is True
    Settings.path().write_text(json.dumps({"scan_file_max": -3}))
    assert Settings.load().scan_file_max == 0

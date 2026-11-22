"""Pro step 1: finding the assembler (paths.nasm_exe) and tools/fetch_nasm.py."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from cheatmaker import paths
from cheatmaker.settings import Settings

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _fetch_tool():
    spec = importlib.util.spec_from_file_location("fetch_nasm", TOOLS / "fetch_nasm.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _exe(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / paths.NASM_EXE_NAME
    p.write_bytes(b"MZ")
    return p


@pytest.fixture
def no_nasm(monkeypatch, tmp_path):
    """No bundled copy and nothing on PATH."""
    monkeypatch.setattr(paths, "bundled_nasm_dir", lambda: tmp_path / "bundled")
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    return tmp_path


# -- paths.nasm_exe ---------------------------------------------------------------------
def test_configured_path_wins(no_nasm):
    mine = _exe(no_nasm / "mine")
    _exe(no_nasm / "bundled")
    assert paths.nasm_exe(str(mine)) == mine


def test_bundled_copy_before_path(no_nasm, monkeypatch):
    bundled = _exe(no_nasm / "bundled")
    monkeypatch.setattr(paths.shutil, "which", lambda name: "/usr/bin/nasm")
    assert paths.nasm_exe() == bundled


def test_missing_configured_path_falls_back_and_logs(no_nasm, caplog):
    bundled = _exe(no_nasm / "bundled")
    assert paths.nasm_exe(str(no_nasm / "gone.exe")) == bundled
    assert "action=nasm_path_missing" in caplog.text


def test_path_lookup_last(no_nasm, monkeypatch):
    monkeypatch.setattr(paths.shutil, "which", lambda name: "/opt/bin/nasm")
    assert paths.nasm_exe() == Path("/opt/bin/nasm")


def test_none_when_no_assembler_anywhere(no_nasm):
    assert paths.nasm_exe() is None


def test_frozen_app_looks_next_to_the_app(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.bundled_nasm_dir() == tmp_path / "nasm"


def test_source_checkout_uses_vendor_folder():
    assert paths.bundled_nasm_dir() == paths.REPO_ROOT / "vendor" / "nasm"


def test_nasm_path_setting_round_trip_and_bad_type():
    s = Settings(nasm_path="C:/tools/nasm.exe")
    s.save()
    assert Settings.load().nasm_path == "C:/tools/nasm.exe"
    Settings.path().write_text('{"nasm_path": 5}', encoding="utf-8")
    assert Settings.load().nasm_path == ""


# -- tools/fetch_nasm.py ----------------------------------------------------------------
def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_fetch_refuses_without_a_pinned_hash(monkeypatch, tmp_path, capsys):
    tool = _fetch_tool()
    monkeypatch.setattr(tool, "NASM_ZIP_SHA256", None)
    monkeypatch.setattr(tool, "download", lambda url: pytest.fail("must not download"))
    assert tool.main(["--dest", str(tmp_path / "out")]) == 2
    assert "not pinned" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_fetch_extracts_exe_and_licence_from_a_verified_zip(monkeypatch, tmp_path):
    tool = _fetch_tool()
    data = _zip(
        {
            "nasm-3.02/nasm.exe": b"MZexe",
            "nasm-3.02/LICENSE": b"BSD-2",
            "nasm-3.02/ndisasm.exe": b"MZother",
        }
    )
    monkeypatch.setattr(tool, "NASM_ZIP_SHA256", hashlib.sha256(data).hexdigest().upper())
    monkeypatch.setattr(tool, "download", lambda url: data)
    out = tmp_path / "vendor" / "nasm"
    assert tool.main(["--dest", str(out)]) == 0
    assert (out / "nasm.exe").read_bytes() == b"MZexe"
    assert (out / "LICENSE").read_bytes() == b"BSD-2"
    assert sorted(p.name for p in out.iterdir()) == ["LICENSE", "nasm.exe"]


def test_fetch_rejects_a_zip_with_another_hash(monkeypatch, tmp_path, capsys):
    tool = _fetch_tool()
    zpath = tmp_path / "nasm.zip"
    zpath.write_bytes(_zip({"nasm.exe": b"evil", "LICENSE": b"x"}))
    monkeypatch.setattr(tool, "NASM_ZIP_SHA256", "0" * 64)
    out = tmp_path / "out"
    assert tool.main(["--zip", str(zpath), "--dest", str(out)]) == 2
    assert "expected" in capsys.readouterr().err and not out.exists()


def test_extract_ignores_member_paths(tmp_path):
    tool = _fetch_tool()
    data = _zip({"../../evil/nasm.exe": b"MZ", "a/b/LICENSE": b"L"})
    written = tool.extract(data, tmp_path / "out")
    assert {p.parent for p in written} == {tmp_path / "out"}
    assert not (tmp_path / "evil").exists()


def test_extract_needs_both_files(tmp_path):
    tool = _fetch_tool()
    with pytest.raises(tool.FetchError, match="LICENSE"):
        tool.extract(_zip({"nasm.exe": b"MZ"}), tmp_path)


def test_url_names_the_pinned_version():
    tool = _fetch_tool()
    assert tool.NASM_URL.endswith("/3.02/win64/nasm-3.02-win64.zip")


@pytest.mark.needs_nasm
def test_real_nasm_runs():
    exe = paths.nasm_exe()
    out = subprocess.run([str(exe), "-v"], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0 and "NASM version" in out.stdout

"""Pro step 19: the Windows build spec (cheatmaker.spec), the build's --self-check, and
the ``action=pro_*`` log audit (spec-pro §7). The real build runs in CI on Windows."""

from __future__ import annotations

import io
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from cheatmaker import app, paths

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "cheatmaker.spec"


class _Recorder:
    """Stands in for one PyInstaller class and keeps what it was given."""

    made: ClassVar[list[tuple[str, tuple, dict]]] = []

    def __init__(self, kind: str):
        self.kind = kind

    def __call__(self, *args, **kwargs):
        _Recorder.made.append((self.kind, args, kwargs))
        return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])


def _run_spec(root: Path, monkeypatch, no_nasm: str | None = None) -> dict[str, tuple]:
    _Recorder.made = []
    if no_nasm is None:
        monkeypatch.delenv("CHEATMAKER_NO_NASM", raising=False)
    else:
        monkeypatch.setenv("CHEATMAKER_NO_NASM", no_nasm)
    env = {k: _Recorder(k) for k in ("Analysis", "PYZ", "EXE", "COLLECT")}
    env["SPECPATH"] = str(root)
    exec(compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec"), env)
    return {kind: (args, kwargs) for kind, args, kwargs in _Recorder.made}


@pytest.fixture
def checkout(tmp_path):
    """A copy of the files the spec looks for, with a fetched NASM."""
    for name in ("NOTICE", "LICENSE"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "licenses").mkdir()
    nasm = tmp_path / "vendor" / "nasm"
    nasm.mkdir(parents=True)
    (nasm / "nasm.exe").write_bytes(b"MZ")
    (nasm / "LICENSE").write_text("BSD-2", encoding="utf-8")
    return tmp_path


def test_spec_bundles_nasm_and_the_notices(checkout, monkeypatch):
    made = _run_spec(checkout, monkeypatch)
    _args, kw = made["Analysis"]
    assert kw["binaries"] == [(str(checkout / "vendor" / "nasm" / "nasm.exe"), "nasm")]
    dests = {Path(src).name: dest for src, dest in kw["datas"]}
    assert dests == {"NOTICE": ".", "LICENSE": "nasm", "licenses": "licenses"}
    assert (str(checkout / "LICENSE"), ".") in kw["datas"]
    assert made["Analysis"][0][0] == [str(checkout / "src" / "cheatmaker" / "__main__.py")]
    assert made["EXE"][1]["console"] is False and made["COLLECT"][1]["name"] == "CheatMaker"


def test_spec_refuses_to_build_without_nasm(checkout, monkeypatch):
    (checkout / "vendor" / "nasm" / "nasm.exe").unlink()
    with pytest.raises(SystemExit, match="fetch_nasm"):
        _run_spec(checkout, monkeypatch)
    made = _run_spec(checkout, monkeypatch, no_nasm="1")
    assert made["Analysis"][1]["binaries"] == []


def test_spec_paths_exist_in_this_checkout():
    for rel in ("NOTICE", "LICENSE", "licenses", "src/cheatmaker/__main__.py"):
        assert (ROOT / rel).exists(), rel


def test_frozen_app_looks_for_nasm_where_the_spec_puts_it(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.bundled_nasm_dir() == tmp_path / "nasm"


@pytest.fixture
def bundled(monkeypatch, tmp_path):
    """The bundled NASM folder, empty: tests put the assembler in it or not."""
    folder = tmp_path / "nasm"
    folder.mkdir()
    monkeypatch.setattr(paths, "bundled_nasm_dir", lambda: folder)
    return folder


def test_self_check_reports_and_needs_the_assembler(bundled):
    (bundled / paths.NASM_EXE_NAME).write_bytes(b"")
    out = io.StringIO()
    assert app.self_check(out) == 0
    assert "modules: ok" in out.getvalue() and "assembler:" in out.getvalue()
    (bundled / paths.NASM_EXE_NAME).unlink()
    out = io.StringIO()
    assert app.self_check(out) == 1 and "assembler: MISSING" in out.getvalue()


def test_self_check_ignores_the_nasm_path_setting(bundled, monkeypatch, tmp_path):  # m6
    elsewhere = tmp_path / "nasm.exe"
    elsewhere.write_bytes(b"")
    monkeypatch.setattr(paths, "nasm_exe", lambda configured="": elsewhere)
    assert app.self_check(io.StringIO()) == 1  # only the build's own copy counts


def test_self_check_writes_its_report_next_to_the_exe(bundled, monkeypatch, tmp_path, caplog):
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "CheatMaker.exe"))
    caplog.set_level(logging.INFO)
    out = io.StringIO()
    assert app.self_check(out) == 1
    report = (exe_dir / app.SELF_CHECK_FILE).read_text(encoding="utf-8")
    assert "modules: ok" in report and "assembler: MISSING" in report
    assert "action=self_check assembler: MISSING" in caplog.text
    assert f"report: {exe_dir / app.SELF_CHECK_FILE}" in out.getvalue()


def test_self_check_falls_back_to_the_app_folder(bundled, monkeypatch, tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("")  # a file where the exe's folder would be: can't write there
    monkeypatch.setattr(app, "self_check_path", lambda: blocked / app.SELF_CHECK_FILE)
    app.self_check(io.StringIO())
    assert (paths.app_data_dir() / app.SELF_CHECK_FILE).is_file()


def test_main_runs_the_self_check_without_a_window(monkeypatch):
    from cheatmaker import logs

    started = []
    monkeypatch.setattr(sys, "argv", ["CheatMaker.exe", app.SELF_CHECK])
    monkeypatch.setattr(logs, "setup_logging", lambda: started.append(1) or "log")
    monkeypatch.setattr(app, "self_check", lambda out=None: 7)
    assert app.main() == 7 and started == [1]  # logging first: the report goes there too


# -- log audit (spec-pro §7: every Pro action is logged as action=pro_*) --------------------
PRO_SOURCES = [
    *sorted((ROOT / "src/cheatmaker/core/pro").glob("*.py")),
    *sorted((ROOT / "src/cheatmaker/ui/pro").glob("*.py")),
    ROOT / "src/cheatmaker/console/debugger.py",
]


@pytest.mark.parametrize("path", PRO_SOURCES, ids=lambda p: p.name)
def test_pro_log_lines_use_pro_actions(path):
    actions = re.findall(r"action=([a-z_]+)", path.read_text(encoding="utf-8"))
    assert all(a.startswith("pro_") for a in actions), actions

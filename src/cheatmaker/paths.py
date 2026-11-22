"""Where the app keeps its settings and logs (%APPDATA%\\CheatMaker on Windows)."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR_NAME = "CheatMaker"
REPO_ROOT = Path(__file__).resolve().parents[2]
"""The source checkout (only meaningful when running from source)."""
NASM_EXE_NAME = "nasm.exe" if os.name == "nt" else "nasm"


def app_data_dir() -> Path:
    """Return the per-user app folder. CHEATMAKER_HOME overrides it (used by tests)."""
    override = os.environ.get("CHEATMAKER_HOME")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def scans_dir() -> Path:
    """Folder for the per-scan match lists (Documents\\CheatMaker\\scans). Under
    CHEATMAKER_HOME when that is set, so tests never write to the real Documents."""
    override = os.environ.get("CHEATMAKER_HOME")
    if override:
        return Path(override) / "scans"
    return Path.home() / "Documents" / APP_DIR_NAME / "scans"


def default_save_dir() -> Path:
    """Folder where cheat files are saved on the PC (save-to-PC fallback, AC-6.3)."""
    return Path.home() / "Documents" / APP_DIR_NAME / "cheats"


def bundled_nasm_dir() -> Path:
    """Where the app's own copy of NASM lives: ``nasm/`` inside the frozen (PyInstaller)
    app, or ``vendor/nasm/`` in a source checkout (filled by ``tools/fetch_nasm.py``)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "nasm"
    return REPO_ROOT / "vendor" / "nasm"


def nasm_exe(configured: str = "") -> Path | None:
    """The assembler for Pro caves, or None when there is none.

    Looks in order at ``configured`` (the ``nasm_path`` setting), the bundled copy
    (``bundled_nasm_dir``), then ``nasm`` on PATH. A configured path that isn't a file
    is logged and skipped, so a stale setting never hides a working bundled copy."""
    if configured:
        p = Path(configured)
        if p.is_file():
            return p
        log.warning("action=nasm_path_missing path=%s; trying the bundled copy", p)
    bundled = bundled_nasm_dir() / NASM_EXE_NAME
    if bundled.is_file():
        return bundled
    found = shutil.which("nasm")
    return Path(found) if found else None

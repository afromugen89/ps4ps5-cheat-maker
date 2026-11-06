"""Where the app keeps its settings and logs (%APPDATA%\\CheatMaker on Windows)."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "CheatMaker"


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

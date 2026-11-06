"""Small JSON settings file: last IP, output format, first-run flag (AC-1.3, AC-8.3)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from cheatmaker.paths import app_data_dir

log = logging.getLogger(__name__)

FORMATS = ("json", "shn")


@dataclass
class Settings:
    last_ip: str = ""
    output_format: str = "json"
    first_run_done: bool = False
    ng_port: int = 744
    ftp_port: int = 2121
    show_matches: int = 10  # list matches with live values once this few are left
    save_scan_files: bool = True  # every scan writes its matches to its own text file
    scan_file_max: int = 100_000  # at most this many matches per scan file

    @classmethod
    def path(cls) -> Path:
        return app_data_dir() / "settings.json"

    @classmethod
    def load(cls) -> Settings:
        """Load settings; a missing or broken file gives defaults (and is logged)."""
        p = cls.path()
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings root is not an object")
        except (OSError, ValueError) as exc:
            log.warning("action=load_settings reason=%s; using defaults", exc)
            return cls()
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in raw.items() if k in known})
        if s.output_format not in FORMATS:
            s.output_format = "json"
        if not isinstance(s.save_scan_files, bool):
            s.save_scan_files = True
        if not isinstance(s.scan_file_max, int) or isinstance(s.scan_file_max, bool):
            s.scan_file_max = 100_000
        s.scan_file_max = max(0, s.scan_file_max)
        return s

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(p)

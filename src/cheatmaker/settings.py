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
    scan_connections: int = 4  # connections that scan at once (1 = one, like before)
    fast_scan: bool = True  # ask PS5Debug-NG to read game memory in place (faster)
    percent_pc_limit: int = 20_000  # a Full search checks % on the PC from this many down
    build_notice_hidden: bool = False  # Make it work's "Save your game first" notice

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
        if not isinstance(s.scan_connections, int) or isinstance(s.scan_connections, bool):
            s.scan_connections = 4
        s.scan_connections = max(1, min(8, s.scan_connections))
        if not isinstance(s.fast_scan, bool):
            s.fast_scan = True
        if not isinstance(s.percent_pc_limit, int) or isinstance(s.percent_pc_limit, bool):
            s.percent_pc_limit = 20_000
        s.percent_pc_limit = max(1_000, min(100_000, s.percent_pc_limit))
        if not isinstance(s.build_notice_hidden, bool):
            s.build_notice_hidden = False
        return s

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(p)

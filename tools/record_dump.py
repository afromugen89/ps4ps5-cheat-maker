"""Record a running game's memory into a FakeConsole dump (AC-10.1).

Usage (on the PC, game running, PS5Debug-NG loaded):
    python tools/record_dump.py 192.168.1.20 dumps/my-game --max-mb 64

Writes manifest.json plus one .bin per writable region (up to --max-mb in total),
so tests can replay real memory with FakeConsole.from_dump().
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient
from cheatmaker.core.session import Session


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ip")
    ap.add_argument("out", type=Path)
    ap.add_argument("--max-mb", type=int, default=64)
    args = ap.parse_args()
    s = Session(lambda ip, port: Ps5DebugNgClient.connect(ip, port))
    s.connect(args.ip)
    g = s.refresh_game()
    args.out.mkdir(parents=True, exist_ok=True)
    budget = args.max_mb * 1024 * 1024
    regions = []
    maps = s.require_client().maps(g.pid)
    keep = [
        m for m in maps if m.readable and (m.writable or g.module_base <= m.start < g.module_end)
    ]
    for i, m in enumerate(keep):
        if m.size > budget:
            continue
        data = s.require_client().read(g.pid, m.start, m.size)
        name = f"r{i:03d}.bin"
        (args.out / name).write_bytes(data)
        regions.append({"name": m.name, "start": hex(m.start), "prot": m.prot, "file": name})
        budget -= m.size
    manifest = {
        "server_version": s.server_version,
        "game": {
            "pid": g.pid,
            "title_id": g.title_id,
            "title": g.title,
            "version": g.version,
            "process": g.process,
        },
        "regions": regions,
        "all_maps": [
            {"name": m.name, "start": hex(m.start), "end": hex(m.end), "prot": m.prot} for m in maps
        ],
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Saved {len(regions)} regions to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

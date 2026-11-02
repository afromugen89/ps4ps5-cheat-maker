"""Console test C10a (v3 spike): which instructions touch a value, with their registers.

Usage (on the PC, game running, PS5Debug-NG loaded, value found in Cheat Maker):
    python tools/watch_regs.py 192.168.1.20 0x12e0ea60ac --size 4 --seconds 10
    python tools/watch_regs.py 192.168.1.20 0x12e0ea60ac --kind write --out c10a.json

Sets one hardware watchpoint (read-write by default) for ``--seconds`` while you
play, then prints every instruction that touched the address, whether it reads or
writes, its object register and whether ``register + offset == address`` (that
proves the register layout of the trap packet). Writes nothing to the game.
Allow the Windows Firewall prompt for port 755 if it appears.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient
from cheatmaker.core.anchor_hook import candidates_from_hits
from cheatmaker.core.session import Session


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ip")
    ap.add_argument("address", help="the value's address, e.g. 0x12e0ea60ac")
    ap.add_argument("--size", type=int, default=4, choices=(1, 2, 4, 8))
    ap.add_argument("--kind", default="access", choices=("access", "write"))
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--out", type=Path, help="also save the result as JSON (send it back)")
    args = ap.parse_args()
    address = int(args.address, 16)

    s = Session(lambda ip, port: Ps5DebugNgClient.connect(ip, port))
    s.connect(args.ip)
    g = s.refresh_game()
    client = s.require_client()
    print(f"{g.title_id} {g.version}, module base {g.module_base:#x}")
    print(
        f"Watching {address:#x} ({args.kind}, {args.size} bytes) for {args.seconds:g} s: play now"
    )
    hits = client.watch(g.pid, address, args.size, args.kind, args.seconds)
    print(f"{len(hits)} hits")
    for h in hits:
        regs = " ".join(f"{k}={v:#x}" for k, v in h.regs.items() if k != "rip")
        print(f"  rip={h.rip:#x} (module +{h.rip - g.module_base:#x})  {regs}")
    cands = candidates_from_hits(hits, address, lambda a, n: client.read(g.pid, a, n))
    print("\nInstructions (best hook first):")
    for c in cands:
        ok = "yes" if c.confirmed else "NO"
        print(
            f"  +{c.address - g.module_base:#x}  {c.text:<32} {c.role:<6} hits={c.hits:<3} "
            f"object={c.reg} (+{c.disp:#x})  {c.reg}+offset == address: {ok}"
        )
    if not cands:
        print("  none: nothing touched the value through one object register")
    if args.out:
        doc = {
            "game": {"title_id": g.title_id, "version": g.version, "base": hex(g.module_base)},
            "address": hex(address),
            "kind": args.kind,
            "hits": [
                {"rip": hex(h.rip), "regs": {k: hex(v) for k, v in h.regs.items()}} for h in hits
            ],
            "candidates": [
                {
                    "offset": hex(c.address - g.module_base),
                    "insn": c.text,
                    "role": c.role,
                    "hits": c.hits,
                    "reg": c.reg,
                    "disp": hex(c.disp),
                    "confirmed": c.confirmed,
                }
                for c in cands
            ],
        }
        args.out.write_text(json.dumps(doc, indent=2))
        print(f"\nSaved {args.out}")
    s.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

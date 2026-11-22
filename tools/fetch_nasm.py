"""Fetch the NASM assembler the Pro cave editor runs (architecture-pro, "Packaging").

Usage (Windows, from the repo root; CI runs it before pytest):
    python tools/fetch_nasm.py
    python tools/fetch_nasm.py --zip C:\\Downloads\\nasm-3.02-win64.zip

Downloads the pinned NASM win64 zip (or takes ``--zip``), checks its SHA-256 against
``NASM_ZIP_SHA256`` below, and extracts ``nasm.exe`` and ``LICENSE`` into
``vendor/nasm/`` (git-ignored). Nothing is extracted from a zip whose hash differs, and
the script refuses to run at all while no hash is pinned.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

NASM_VERSION = "3.02"
NASM_ZIP_NAME = f"nasm-{NASM_VERSION}-win64.zip"
NASM_URL = f"https://www.nasm.us/pub/nasm/releasebuilds/{NASM_VERSION}/win64/{NASM_ZIP_NAME}"

# SHA-256 of NASM_ZIP_NAME, pinned 2026-11-22 from the zip downloaded over HTTPS from
# nasm.us on Kein's PC (641,314 bytes; nasm -v: "NASM version 3.02 compiled on Jun 28
# 2026"). NASM publishes no official checksums, so this is trust-on-first-use: any
# later download that differs is refused.
NASM_ZIP_SHA256: str | None = "161d0bfaff53c2f9e9f3e69fd0672323ebabafd1268976a5cec11be92a19aee7"

WANTED = {"nasm.exe": "nasm.exe", "license": "LICENSE"}
"""Zip member base name (lower case) -> file name written to the destination."""

DEFAULT_DEST = Path(__file__).resolve().parent.parent / "vendor" / "nasm"
TIMEOUT_S = 60


class FetchError(Exception):
    """Something the user has to fix; printed without a traceback."""


def check_hash(data: bytes, expected: str | None) -> None:
    if not expected:
        raise FetchError(
            "NASM_ZIP_SHA256 is not pinned in tools/fetch_nasm.py; refusing to use an "
            "unverified assembler. Pin the hash first (see the TODO there)."
        )
    got = hashlib.sha256(data).hexdigest()
    if got != expected.lower():
        raise FetchError(f"{NASM_ZIP_NAME}: SHA-256 is {got}, expected {expected.lower()}")


def extract(data: bytes, dest: Path) -> list[Path]:
    """Write the WANTED members of the zip ``data`` into ``dest``, by base name only (so a
    crafted member path can't write outside ``dest``). Returns the files written."""
    found: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            base = info.filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base in WANTED and not info.is_dir() and base not in found:
                found[base] = zf.read(info)
    missing = [WANTED[k] for k in WANTED if k not in found]
    if missing:
        raise FetchError(f"{NASM_ZIP_NAME} has no {', '.join(missing)}")
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for base, blob in found.items():
        p = dest / WANTED[base]
        p.write_bytes(blob)
        out.append(p)
    return out


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:
        return resp.read()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", type=Path, help=f"use this {NASM_ZIP_NAME} instead of downloading")
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = ap.parse_args(argv)
    try:
        if not NASM_ZIP_SHA256:  # refuse before downloading anything
            check_hash(b"", NASM_ZIP_SHA256)
        data = args.zip.read_bytes() if args.zip else download(NASM_URL)
        check_hash(data, NASM_ZIP_SHA256)
        written = extract(data, args.dest)
    except (FetchError, OSError, zipfile.BadZipFile) as exc:
        print(f"fetch_nasm: {exc}", file=sys.stderr)
        return 2
    for p in written:
        print(f"fetch_nasm: wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# PyInstaller spec for the Windows build (architecture-pro "Packaging", step 19).
#
#   python tools/fetch_nasm.py          # vendor/nasm/nasm.exe + LICENSE (pinned SHA-256)
#   pyinstaller cheatmaker.spec         # -> dist/CheatMaker/CheatMaker.exe (folder build)
#   dist\CheatMaker\CheatMaker.exe --self-check
#
# The build carries NASM in nasm/ (paths.bundled_nasm_dir looks there when frozen) with
# its BSD-2 licence, and NOTICE, LICENSE and licenses/ next to the exe. It refuses to
# build without NASM: a build without it can't assemble caves. Set CHEATMAKER_NO_NASM=1
# to build anyway (the cave editor then says "Assembler not found").
import os
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 (set by PyInstaller)
NASM_DIR = ROOT / "vendor" / "nasm"
NASM_FILES = ("nasm.exe", "LICENSE")
APP_NAME = "CheatMaker"

datas = [
    (str(ROOT / "NOTICE"), "."),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "licenses"), "licenses"),
]
missing = [name for name in NASM_FILES if not (NASM_DIR / name).is_file()]
if missing and os.environ.get("CHEATMAKER_NO_NASM") != "1":
    raise SystemExit(
        f"vendor/nasm is missing {', '.join(missing)}: run python tools/fetch_nasm.py "
        "first (or set CHEATMAKER_NO_NASM=1 to build without the assembler)."
    )
binaries = [] if missing else [(str(NASM_DIR / "nasm.exe"), "nasm")]
if not missing:
    datas.append((str(NASM_DIR / "LICENSE"), "nasm"))

a = Analysis(  # noqa: F821
    [str(ROOT / "src" / "cheatmaker" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=["iced_x86"],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name=APP_NAME,
    upx=False,
)

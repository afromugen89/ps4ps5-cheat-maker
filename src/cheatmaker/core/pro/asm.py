"""Assembling Pro cave sources (architecture-pro "Assembler integration and packaging").

The user writes the cave body only. The app wraps it with the symbols it knows:

- ``hook``: the patched site; ``return``: the first byte after the instructions the
  jump covers; ``original``: a macro emitting those instructions, re-encoded for the
  cave (RIP-relative operands keep pointing at the same addresses);
- each variable by name, placed at the end of the cave (R1: read-only constants);
- any extra names the caller passes (e.g. another cave's variables) as ``equ``.

``NasmAssembler`` runs the bundled NASM as a subprocess (about 4 ms per run on a Linux
PC; AC-8.1 allows 0.5 s). ``FakeAssembler`` understands a tiny subset of the language
so UI and core tests run without NASM. Both follow the ``Assembler`` protocol.

**Bare numbers are module offsets**, the same as ``eboot.bin+offset`` everywhere else
in the app (sources are assembled with ``org`` = the cave's module offset). A memory
operand that is just a number (``[0x5e1540a]``) is made RIP-relative to that offset,
and a ``jmp``/``call``/``jcc`` to a number jumps there; a number outside the module
(``CaveContext.module_size``) is refused with a line error. Reaper/ReaperPy sources
write absolute console addresses: those need the module base subtracted before they
assemble here (a later UI step; not done by the shim).

A light dialect shim lets those sources assemble otherwise: ``dword ptr`` loses
``ptr``, ``xmmword``/``ymmword`` become NASM's ``oword``/``yword`` and
``OriginReturn`` means ``return``. Segment-prefixed operands (``fs:[0x28]``) are left
alone. The shim never adds or removes lines, so error line numbers stay the user's.
"""

from __future__ import annotations

import logging
import re
import struct
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import iced_x86 as ix

from cheatmaker.core.pro.model import Variable
from cheatmaker.errors import CheatMakerError

log = logging.getLogger(__name__)

TIMEOUT = 2.0
"""Seconds NASM may take before the assemble is given up (it normally takes ms)."""
USER_FILE = "user"
"""The file name NASM reports for lines of the user's source (via ``%line``)."""
_VARS_FILE = "cheatmaker-variables"
_SOURCE, _OUTPUT, _MAP, _LIST = "cave.asm", "cave.bin", "cave.map", "cave.lst"
_PREPROCESSED = "cave.i"
VARIABLE_ALIGN = 4


class AssemblerNotFound(CheatMakerError):
    """No assembler on this PC (spec PR-8: "Assembler not found")."""


@dataclass(frozen=True)
class AsmResult:
    """``code`` is empty when ``error`` is set. ``labels`` maps every label and variable
    to its absolute address (from NASM's map file). ``error`` is (user line, message);
    line 0 means the error isn't on a line of the user's source."""

    code: bytes
    labels: dict[str, int] = field(default_factory=dict)
    error: tuple[int, str] | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class CaveContext:
    """Where a cave goes and what it is hooked into.

    ``org``: the cave's address (a module offset for Pro caves). ``hook``: the patched
    site, or None for a plain block of code (no ``hook``/``return``/``original``).
    ``original``: the bytes the hook's jump covers. ``symbols``: more names defined
    with ``equ``. ``module_size``: when set, a bare number used as an address in the
    source must be in ``[0, module_size)``."""

    org: int
    hook: int | None = None
    original: bytes = b""
    symbols: Mapping[str, int] = field(default_factory=dict)
    module_size: int | None = None

    @property
    def ret(self) -> int | None:
        return None if self.hook is None else self.hook + len(self.original)


class Assembler(Protocol):
    def assemble(
        self, source: str, ctx: CaveContext, variables: Sequence[Variable] = ()
    ) -> AsmResult: ...


# -- formatting machine code as NASM text ---------------------------------------
def nasm_formatter() -> ix.Formatter:
    """iced's NASM formatter set up so NASM reads its output back: 0x hex, absolute
    branch targets and ``[rel <address>]`` for RIP-relative operands."""
    f = ix.Formatter(ix.FormatterSyntax.NASM)
    f.hex_prefix = "0x"
    f.hex_suffix = ""
    f.uppercase_hex = False
    f.leading_zeros = False
    f.branch_leading_zeros = False
    f.rip_relative_addresses = False
    f.space_after_operand_separator = True
    return f


_FMT = nasm_formatter()


def insn_text(insn: ix.Instruction) -> str:
    """One instruction in NASM syntax. An operand with no base register (a plain
    address, or ``[N+rcx*4]``) is written ``[abs ...]``: it is a fixed address, not a
    module offset (the shim and the absolute-operand check leave it alone)."""
    text = _FMT.format(insn)
    if any(
        insn.op_kind(i) == ix.OpKind.MEMORY and insn.memory_base == ix.Register.NONE
        for i in range(insn.op_count)
    ):
        text = text.replace("[", "[abs ", 1)
    return text


def original_text(code: bytes, ip: int) -> list[str]:
    """The covered instructions as NASM lines (for the ``original`` macro)."""
    return [insn_text(i) for i in ix.Decoder(64, code, ip=ip)]


# -- the dialect shim ------------------------------------------------------------
_PTR = re.compile(r"\b(byte|word|dword|qword|tword|fword|xmmword|ymmword|zmmword)\s+ptr\b", re.I)
_WIDE = {"xmmword": "oword", "ymmword": "yword", "zmmword": "zword"}
_ORIGIN_RETURN = re.compile(r"\bOriginReturn\b", re.I)
_NUM = r"(?:0x[0-9a-f]+|[0-9][0-9a-f]*h|\d+)"
_PLAIN_ADDRESS = re.compile(
    rf"(?P<seg>\b[cdefgs]s\s*:\s*)?(?<![\w.$])\[\s*(?:rel\s+)?(?P<num>{_NUM})\s*\]", re.I
)
_BRANCH_NUMBER = re.compile(
    rf"^\s*(?:[A-Za-z_.][\w.]*:)?\s*(?:j[a-z]+|call|loop[a-z]*)\s+"
    rf"(?:(?:short|near|strict)\s+)*({_NUM})\s*$",
    re.I,
)
_FORBIDDEN = re.compile(
    r"\bincbin\b|%\s*(?:include|pathsearch|depend|use|deftok)\b|%!|%\+|%\[", re.I
)
"""Directives that read files or the environment on the PC (or build such a word from
pieces): a cave is code the player writes, so none of them is allowed (QA BUG-03,
BUG-11). The preprocessed text is checked too (``preprocessed_error``)."""
_LINE_DIRECTIVE = re.compile(r"%\s*line\b", re.I)
LINE_DIRECTIVE = (
    "%line isn't allowed in a cave (it would hide lines from the error lines and the "
    "[abs N] address check)"
)
_PREPROCESSED_FORBIDDEN = re.compile(r"\bincbin\b|%\s*(?:include|pathsearch|depend|use)\b", re.I)
FILE_NOT_ALLOWED = "{what} isn't allowed in a cave (it reads files on the PC)"


def _number(text: str) -> int:
    t = text.lower()
    if t.endswith("h") and not t.startswith("0x"):
        return int(t[:-1], 16)
    return int(t, 0)


def split_line(line: str) -> tuple[list[tuple[bool, str]], str]:
    """A source line as (is code, text) pieces, strings kept apart, and its comment
    (from the first ``;`` outside a string, included)."""
    parts: list[tuple[bool, str]] = []
    i = start = 0
    while i < len(line):
        ch = line[i]
        if ch == ";":
            break
        if ch in "'\"`":
            end = line.find(ch, i + 1)
            end = len(line) if end < 0 else end + 1
            parts.append((True, line[start:i]))
            parts.append((False, line[i:end]))
            start = i = end
            continue
        i += 1
    parts.append((True, line[start:i]))
    return [p for p in parts if p[1]], line[i:]


def _code_text(line: str) -> str:
    """The line's code with string contents blanked (same length) and no comment."""
    parts, _comment = split_line(line)
    return "".join(t if is_code else t[0] + " " * (len(t) - 1) for is_code, t in parts)


def _shim_code(code: str, org: int) -> str:
    code = _PTR.sub(lambda m: _WIDE.get(m.group(1).lower(), m.group(1)), code)
    code = _ORIGIN_RETURN.sub("return", code)

    def rel(m: re.Match[str]) -> str:
        if m.group("seg"):  # fs:[0x28] stays an absolute, segment-based address
            return m.group(0)
        # NASM can't make a bare number RIP-relative; an offset from the section start can.
        return f"[rel $$+({_number(m.group('num')):#x}-{org:#x})]"

    return _PLAIN_ADDRESS.sub(rel, code)


def shim_line(line: str, org: int) -> str:
    """One source line in NASM's dialect (strings and comments are left as they are)."""
    parts, comment = split_line(line)
    return "".join(_shim_code(t, org) if is_code else t for is_code, t in parts) + comment


def shim(source: str, org: int) -> str:
    return "\n".join(shim_line(line, org) for line in source.split("\n"))


def check_numbers(source: str, module_size: int | None) -> tuple[int, str] | None:
    """The first line using a bare number as an address outside the module, as
    (line, message); None when every one is a module offset (or no size is known)."""
    if module_size is None:
        return None
    for n, line in enumerate(source.split("\n"), 1):
        code = _code_text(line)
        numbers = [m.group("num") for m in _PLAIN_ADDRESS.finditer(code) if not m.group("seg")]
        branch = _BRANCH_NUMBER.match(code)
        if branch:
            numbers.append(branch.group(1))
        for text in numbers:
            value = _number(text)
            if not 0 <= value < module_size:
                return (
                    n,
                    f"{text} is outside the game's module (0 to {module_size - 1:#x}): "
                    "addresses here are module offsets (eboot.bin+offset)",
                )
    return None


def check_source(source: str, module_size: int | None) -> tuple[int, str] | None:
    """What the source may not do, as the first (line, message): read PC files or the
    environment (``incbin``, ``%include``, ...), or use an address outside the module."""
    for n, line in enumerate(source.split("\n"), 1):
        code = _code_text(line)
        if _LINE_DIRECTIVE.search(code):
            return n, LINE_DIRECTIVE
        m = _FORBIDDEN.search(code)
        if m:
            return n, FILE_NOT_ALLOWED.format(what=m.group(0).strip())
    return check_numbers(source, module_size)


_KNOWN_FILES = {_SOURCE, USER_FILE, _VARS_FILE}


def preprocessed_error(text: str) -> tuple[int, str] | None:
    """Check NASM's preprocessed output (``nasm -E``) of the wrapped source: a file
    pulled in (a ``%line`` naming another file) or a file-reading directive built from
    pieces is refused, on the user's line when it is known (QA BUG-11)."""
    where, line, step = "", 0, 1
    for raw in text.splitlines():
        directive = raw.strip().split()
        if directive[:1] == ["%line"] and len(directive) >= 3:
            start, _, inc = directive[1].partition("+")
            where = directive[2]
            line, step = int(start or 0), int(inc or 1)
            if where not in _KNOWN_FILES:
                return 0, FILE_NOT_ALLOWED.format(what=f"reading {where}")
            continue
        m = _PREPROCESSED_FORBIDDEN.search(_code_text(raw))
        if m:
            at = line if where == USER_FILE else 0
            return at, FILE_NOT_ALLOWED.format(what=m.group(0).strip())
        line += step
    return None


# -- the wrapper -----------------------------------------------------------------
def _variable_lines(variables: Sequence[Variable]) -> list[str]:
    if not variables:
        return []
    lines = [f"align {VARIABLE_ALIGN}, db 0"]
    for v in variables:  # raw bytes: exact for every type, float or not
        lines.append(f"{v.name}: db " + ", ".join(f"{b:#04x}" for b in v.encoded()))
    return lines


def wrap(source: str, ctx: CaveContext, variables: Sequence[Variable] = ()) -> str:
    """The whole file NASM assembles. ``%line`` makes NASM report the user's own line
    numbers under the file name ``user``."""
    head = ["bits 64", "default rel", f"org {ctx.org:#x}", f"[map symbols {_MAP}]"]
    names = dict(ctx.symbols)
    if ctx.hook is not None:
        names["hook"] = ctx.hook
        names["return"] = ctx.hook + len(ctx.original)
    head += [f"{name} equ {addr:#x}" for name, addr in names.items()]
    if ctx.hook is not None:
        head.append("%macro original 0")
        head += [shim_line(t, ctx.org) for t in original_text(ctx.original, ctx.hook)]
        head.append("%endmacro")
    body = [f"%line 0+1 {USER_FILE}", shim(source, ctx.org)]
    tail = [f"%line 0+1 {_VARS_FILE}", *_variable_lines(variables)]
    return "\n".join([*head, *body, *tail]) + "\n"


# -- NASM output -----------------------------------------------------------------
_DIAG = re.compile(r"^(?P<file>[^:\n]*):(?P<line>\d+): (?P<kind>error|fatal): (?P<msg>.*)$")
_BARE = re.compile(r"^(?:nasm: )?(?P<kind>error|fatal): (?P<msg>.*)$")


def parse_error(stderr: str) -> tuple[int, str] | None:
    """The first error NASM printed as (user line, message); None if there is none."""
    for raw in stderr.splitlines():
        line = raw.strip()
        m = _DIAG.match(line)
        if m:
            return (int(m["line"]) if m["file"] == USER_FILE else 0, m["msg"].strip())
        m = _BARE.match(line)
        if m:
            return (0, m["msg"].strip())
    return None


_MAP_ROW = re.compile(r"^\s*([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+(\S+)\s*$")


def parse_map(text: str) -> dict[str, int]:
    """Label name -> address from the ``.text`` section of a NASM map file."""
    labels: dict[str, int] = {}
    in_text = False
    for line in text.splitlines():
        if line.startswith("---- Section"):
            in_text = ".text" in line
            continue
        if line.startswith("----") or line.startswith("-- "):
            in_text = False
            continue
        m = _MAP_ROW.match(line) if in_text else None
        if m:
            labels[m[3]] = int(m[2], 16)
    return labels


class NasmAssembler:
    """Runs NASM (``nasm -f bin``) on the wrapped source in a fresh temp folder."""

    def __init__(self, exe: Path | str, timeout: float = TIMEOUT) -> None:
        self.exe = Path(exe)
        self.timeout = timeout

    def _run(self, folder: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.exe), *args],
            cwd=folder,
            capture_output=True,
            # NASM echoes the user's names as UTF-8; never fail on its output
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            check=False,
            # no console window flashing up on Windows
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def assemble(
        self, source: str, ctx: CaveContext, variables: Sequence[Variable] = ()
    ) -> AsmResult:
        started = time.perf_counter()
        bad = check_source(source, ctx.module_size)
        if bad is not None:
            return AsmResult(b"", {}, bad)
        with tempfile.TemporaryDirectory(prefix="cheatmaker-asm-") as tmp:
            folder = Path(tmp)
            (folder / _SOURCE).write_text(wrap(source, ctx, variables), encoding="utf-8")
            try:
                pre = self._run(folder, "-E", "-o", _PREPROCESSED, _SOURCE)
                pre_out = folder / _PREPROCESSED
                if pre.returncode == 0 and pre_out.is_file():
                    bad = preprocessed_error(pre_out.read_text("utf-8", "replace"))
                    if bad is not None:
                        return AsmResult(b"", {}, bad)
                done = self._run(
                    folder,
                    # a number too big for its operand is an error, not a silent cut;
                    # so is an address with no register that can't be RIP-relative
                    "-Werror=number-overflow",
                    "-Werror=ea-absolute",
                    "-f",
                    "bin",
                    "-l",
                    _LIST,
                    "-o",
                    _OUTPUT,
                    _SOURCE,
                )
            except FileNotFoundError:
                raise AssemblerNotFound(f"Assembler not found ({self.exe})") from None
            except subprocess.TimeoutExpired:
                log.warning("action=pro_asm_timeout seconds=%s", self.timeout)
                return AsmResult(b"", {}, (0, "the assembler took too long; try again"))
            except OSError as e:
                raise AssemblerNotFound(f"The assembler couldn't start: {e}") from e
            error = parse_error(done.stderr)
            if error is not None and "ea-absolute" in error[1]:
                error = (error[0], ABSOLUTE_ADDRESS)
            out = folder / _OUTPUT
            if error is None and (done.returncode != 0 or not out.is_file()):
                error = (0, done.stderr.strip() or f"the assembler failed ({done.returncode})")
            if error is not None:
                return AsmResult(b"", {}, error)
            code = out.read_bytes()
            map_file = folder / _MAP
            labels = parse_map(map_file.read_text("utf-8", "replace")) if map_file.is_file() else {}
            listing = folder / _LIST
            rows = parse_listing(listing.read_text("utf-8", "replace")) if listing.is_file() else []
            bad = absolute_operand(code, ctx.org, rows, source)
            if bad is not None:
                return AsmResult(b"", {}, bad)
        log.debug(
            "action=pro_asm bytes=%d ms=%.1f", len(code), (time.perf_counter() - started) * 1000
        )
        return AsmResult(code, labels)


_LIST_ROW = re.compile(r"^\s*(\d+)\s+([0-9A-Fa-f]{8})\s+(\S+)\s+(<\d+>)?\s*(.*)$")
_DATA = r"(?:d[bwdqtoyz]|res[bwdqtoyz]|align|alignb|incbin)\b"
_DATA_WORDS = re.compile(rf"^\s*(?:[A-Za-z_.][\w.]*:\s*)?(?:{_DATA}|times\b.*?\b{_DATA})", re.I)
"""Rows that hold data, not instructions (``times`` only when it repeats data)."""
ABSOLUTE_ADDRESS = (
    "an address with no register must be a module offset written as one number, "
    "[0x...]; write [abs N] for a fixed address"
)


@dataclass(frozen=True)
class ListRow:
    """One listing row that made bytes: where, the user line (None: not a line of the
    user's own text, e.g. a macro expansion) and the source text."""

    offset: int
    line: int | None
    text: str


def parse_listing(text: str) -> list[ListRow]:
    """The rows of a NASM listing that hold code, in the user's part of the wrapper."""
    rows: list[ListRow] = []
    region = ""
    for raw in text.splitlines():
        m = _LIST_ROW.match(raw)
        if m is None:
            directive = raw.strip().split()
            if len(directive) >= 4 and directive[1] == "%line":
                region = directive[3]
            continue
        number, offset, _bytes, macro, src = m.groups()
        if src.lstrip().startswith("%line"):
            region = src.split()[2] if len(src.split()) > 2 else ""
            continue
        if region != USER_FILE or _bytes.startswith("<"):  # "<rep 2h>": a times repeat
            continue
        rows.append(ListRow(int(offset, 16), None if macro else int(number), src))
    return rows


def _first_bracket_line(source: str) -> int:
    for n, line in enumerate(source.split("\n"), 1):
        if "[" in _code_text(line):
            return n
    return 0


def absolute_operand(
    code: bytes, org: int, rows: Sequence[ListRow], source: str
) -> tuple[int, str] | None:
    """(line, message) for the first instruction of the user's code that reads or
    writes a fixed address with no base register (``[N+rcx*4]``, ``[dword N]``, ...)
    the user didn't write as ``[abs N]``: here bare numbers are module offsets, and
    such an operand would use the number as a console address (QA BUG-01). fs/gs
    operands are allowed. Data lines (``db``, ``times``, ...) aren't decoded."""
    for row in rows:
        code_text = _code_text(row.text)  # an "abs" in a comment or string doesn't count
        if _DATA_WORDS.match(code_text) or re.search(r"\babs\b", code_text, re.I):
            continue
        at = row.offset
        if not 0 <= at < len(code):
            continue
        insn = ix.Decoder(64, code[at:], ip=org + at).decode()
        if insn.code == ix.Code.INVALID or not _absolute(insn):
            continue
        return (row.line or _first_bracket_line(source), ABSOLUTE_ADDRESS)
    return None


def _absolute(insn: ix.Instruction) -> bool:
    if not any(insn.op_kind(i) == ix.OpKind.MEMORY for i in range(insn.op_count)):
        return False
    if insn.memory_base != ix.Register.NONE or insn.memory_displacement == 0:
        return False
    return insn.segment_prefix not in (ix.Register.FS, ix.Register.GS)


def default_assembler(configured: str = "") -> NasmAssembler:
    """The NASM the app finds (setting, bundled, PATH); AssemblerNotFound if none."""
    from cheatmaker import paths

    exe = paths.nasm_exe(configured)
    if exe is None:
        raise AssemblerNotFound("Assembler not found")
    return NasmAssembler(exe)


# -- a stand-in for tests --------------------------------------------------------
_FAKE_LABEL = re.compile(r"^([A-Za-z_.][A-Za-z0-9_.]*):\s*(.*)$")
_FAKE_FIXED = {"nop": b"\x90", "ret": b"\xc3", "int3": b"\xcc"}
_JMP_SIZE = 5


def _relocate(code: bytes, src: int, dst: int) -> bytes:
    enc = ix.BlockEncoder(64)
    enc.add_many(list(ix.Decoder(64, code, ip=src)))
    return bytes(enc.encode(dst))


class FakeAssembler:
    """Assembles a tiny language with no NASM: ``label:``, ``db 0x.., ..``, ``nop``,
    ``ret``, ``int3``, ``jmp <label|symbol|number>`` (always E9 rel32), ``original``
    and ``; comments``. Variables follow as in the real wrapper. Every call is kept in
    ``calls``; ``fail`` makes every assemble report that error."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, CaveContext, tuple[Variable, ...]]] = []
        self.fail: tuple[int, str] | None = None

    def assemble(
        self, source: str, ctx: CaveContext, variables: Sequence[Variable] = ()
    ) -> AsmResult:
        self.calls.append((source, ctx, tuple(variables)))
        if self.fail is not None:
            return AsmResult(b"", {}, self.fail)
        bad = check_source(source, ctx.module_size)
        if bad is not None:
            return AsmResult(b"", {}, bad)
        names = dict(ctx.symbols)
        if ctx.hook is not None:
            names.update(hook=ctx.hook, **{"return": ctx.hook + len(ctx.original)})
        lines = list(enumerate(shim(source, ctx.org).split("\n"), 1))
        try:
            labels = self._layout(lines, ctx, variables)  # pass 1: sizes and labels
            code = self._emit(lines, ctx, {**names, **labels}, variables)
        except _FakeError as e:
            return AsmResult(b"", {}, (e.line, e.msg))
        return AsmResult(code, labels)

    def _statements(self, lines: Iterable[tuple[int, str]]):
        for n, raw in lines:
            text = raw.split(";", 1)[0].strip()
            m = _FAKE_LABEL.match(text)
            if m:
                yield n, m[1], m[2].strip()
            elif text:
                yield n, None, text

    def _size(self, n: int, stmt: str, ctx: CaveContext) -> int:
        word = stmt.split(None, 1)[0].lower()
        if word in _FAKE_FIXED:
            return 1
        if word == "jmp":
            return _JMP_SIZE
        if word == "db":
            return len(_db(n, stmt))
        if word == "original":
            if ctx.hook is None:
                raise _FakeError(n, "no original here")
            return len(_relocate(ctx.original, ctx.hook, ctx.org))
        raise _FakeError(n, f"fake assembler can't do {stmt!r}")

    def _layout(self, lines, ctx: CaveContext, variables) -> dict[str, int]:
        labels: dict[str, int] = {}
        at = ctx.org
        for n, label, stmt in self._statements(lines):
            if label is not None:
                labels[label] = at
            if stmt:
                at += self._size(n, stmt, ctx)
        if variables:
            at += -(at - ctx.org) % VARIABLE_ALIGN
            for v in variables:
                labels[v.name] = at
                at += v.vtype.size
        return labels

    def _emit(self, lines, ctx: CaveContext, names: dict[str, int], variables) -> bytes:
        out = bytearray()
        for n, _label, stmt in self._statements(lines):
            if not stmt:
                continue
            word, _, rest = stmt.partition(" ")
            word, here = word.lower(), ctx.org + len(out)
            if word in _FAKE_FIXED:
                out += _FAKE_FIXED[word]
            elif word == "db":
                out += _db(n, stmt)
            elif word == "original":
                assert ctx.hook is not None  # checked in _size
                out += _relocate(ctx.original, ctx.hook, here)
            else:  # jmp
                target = rest.strip()
                if target in names:
                    dest = names[target]
                else:
                    try:
                        dest = _number(target)
                    except ValueError:
                        raise _FakeError(n, f"symbol `{target}' not defined") from None
                out += b"\xe9" + struct.pack("<i", dest - (here + _JMP_SIZE))
        if variables:
            out += bytes(-len(out) % VARIABLE_ALIGN)
            for v in variables:
                out += v.encoded()
        return bytes(out)


class _FakeError(Exception):
    def __init__(self, line: int, msg: str) -> None:
        super().__init__(msg)
        self.line, self.msg = line, msg


def _db(n: int, stmt: str) -> bytes:
    try:
        return bytes(_number(x.strip()) for x in stmt.split(None, 1)[1].split(","))
    except (IndexError, ValueError):
        raise _FakeError(n, f"bad db: {stmt!r}") from None


__all__ = [
    "TIMEOUT",
    "AsmResult",
    "Assembler",
    "AssemblerNotFound",
    "CaveContext",
    "FakeAssembler",
    "NasmAssembler",
    "check_numbers",
    "check_source",
    "default_assembler",
    "insn_text",
    "nasm_formatter",
    "original_text",
    "parse_error",
    "parse_map",
    "preprocessed_error",
    "shim",
    "shim_line",
    "wrap",
]

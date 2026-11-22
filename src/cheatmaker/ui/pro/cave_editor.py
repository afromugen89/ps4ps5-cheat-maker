"""The cave editor (spec PR-8): source, bytes side by side, first error, cave budget.

Typing changes a draft, never the cheat: the draft becomes the entry's source only when
Build succeeds (so nothing, e.g. a variable edit, ever uses unbuilt text). Typing
restarts a ``DEBOUNCE_MS`` timer; then the draft is assembled on the worker (AC-8.1:
bytes or the first error within 0.5 s). A result for older text is dropped.
The budget (free room for the cave) reads the console, so it is worked out when an
entry is opened and after a build, not on every key.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRegularExpression, QTimer
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cheatmaker.core.pro import compile as pc_compile
from cheatmaker.core.pro import sites
from cheatmaker.core.pro.asm import AsmResult, Assembler
from cheatmaker.core.pro.model import ProCheat, ProEntry
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.tab import ProMessages
from cheatmaker.ui.tasks import TaskRunner

DEBOUNCE_MS = 300
_WORDS = r"\b(?:original|return|hook)\b"


def hex_lines(code: bytes, width: int = 8) -> str:
    """Bytes as hex, ``width`` a line (shown next to the source)."""
    return "\n".join(code[i : i + width].hex(" ").upper() for i in range(0, len(code), width))


def mono() -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


class AsmHighlighter(QSyntaxHighlighter):
    """Comments, labels, numbers and the app's own names (``hook``, ``return``,
    ``original``)."""

    def __init__(self, doc) -> None:
        super().__init__(doc)

        def fmt(color: str, bold: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            return f

        self.rules = [
            (QRegularExpression(r"\b(?:0x[0-9a-fA-F]+|[0-9][0-9a-fA-F]*h?)\b"), fmt("#a0522d")),
            (QRegularExpression(_WORDS), fmt("#00008b", True)),
            (QRegularExpression(r"^\s*[A-Za-z_.][\w.]*:"), fmt("#006400", True)),
            (QRegularExpression(r";.*$"), fmt("#808080")),
        ]

    def highlightBlock(self, text: str) -> None:
        for rx, f in self.rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), f)


class CaveEditor(QWidget):
    def __init__(self, ctl: Controller, tasks: TaskRunner, msgs: ProMessages) -> None:
        super().__init__()
        self.ctl = ctl
        self.tasks = tasks
        self.msgs = msgs
        self.assembler: Assembler | None = None
        self.pc: ProCheat | None = None
        self.entry: ProEntry | None = None
        self.others: Callable[[], list[sites.Range]] = list  # rooms other caves keep
        # Build: (entry, the draft text) -> the tab builds it; the draft becomes the
        # entry's source only when the build succeeds (``built``).
        self.on_build: Callable[[ProEntry, str], None] = lambda e, text: None
        self._drafts: dict[int, tuple[ProEntry, str]] = {}  # id(entry) -> unbuilt text
        self.room: int | None = None
        self.size: int | None = None
        self._gen = 0

        self.title = QLabel("")
        self.source = QPlainTextEdit()
        self.source.setFont(mono())
        self.source.setTabStopDistance(32)
        self.highlighter = AsmHighlighter(self.source.document())
        self.bytes_view = QPlainTextEdit()
        self.bytes_view.setReadOnly(True)
        self.bytes_view.setFont(mono())
        self.bytes_view.setMaximumWidth(260)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b00000;")
        self.budget = QLabel("")
        self.build_btn = QPushButton("Build")
        self.build_btn.setToolTip("Assemble, place the cave and keep the new bytes.")

        side = QHBoxLayout()
        side.addWidget(self.source, 3)
        side.addWidget(self.bytes_view, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.budget, 1)
        bottom.addWidget(self.build_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.title)
        lay.addLayout(side, 1)
        lay.addWidget(self.error)
        lay.addLayout(bottom)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(DEBOUNCE_MS)
        self.timer.timeout.connect(self.assemble_now)
        self.source.textChanged.connect(self._typed)
        self.build_btn.clicked.connect(self._build_clicked)
        self.set_entry(None, None)

    # -- entry ---------------------------------------------------------------------
    def set_entry(self, pc: ProCheat | None, entry: ProEntry | None, read_only=False) -> None:
        self.pc, self.entry = pc, entry if entry is not None and entry.kind == "cave" else None
        self.timer.stop()
        self._gen += 1
        self.room = self.size = None
        cave = self.entry
        self.source.blockSignals(True)
        self.source.setPlainText(self.draft_of(cave) if cave else "")
        self.source.blockSignals(False)
        self.source.setReadOnly(cave is None or read_only)
        self.bytes_view.setPlainText(hex_lines(cave.cave_bytes) if cave else "")
        self.error.setText("")
        self.budget.setText("")
        self.title.setText(
            f"Cave hooked at +{cave.offset:#x}"
            + ("" if cave.cave is None else f", at +{cave.cave:#x}")
            if cave
            else "Pick a cave entry to see its assembly."
        )
        self._update_build(read_only)
        if cave is not None:
            self.refresh_budget()

    def _update_build(self, read_only: bool = False) -> None:
        ok = self.entry is not None and self.assembler is not None and not read_only
        self.build_btn.setEnabled(ok)
        if self.entry is not None and self.assembler is None:
            self.error.setText(hints.ASSEMBLER_MISSING)

    def draft_of(self, entry: ProEntry) -> str:
        """The text being edited for ``entry``: an unbuilt draft, else its source."""
        kept = self._drafts.get(id(entry))
        return kept[1] if kept is not None and kept[0] is entry else entry.source

    def has_draft(self, entry: ProEntry) -> bool:
        return self.draft_of(entry) != entry.source

    def built(self, entry: ProEntry) -> None:
        """``entry`` was built from its draft (now its source)."""
        self._drafts.pop(id(entry), None)

    def _typed(self) -> None:
        """Typing changes only the draft; the cheat keeps its built source until Build."""
        if self.entry is None or self.source.isReadOnly():
            return
        self._drafts[id(self.entry)] = (self.entry, self.source.toPlainText())
        self._gen += 1
        self.timer.start()

    def _build_clicked(self) -> None:
        if self.entry is not None:
            self.on_build(self.entry, self.draft_of(self.entry))

    # -- assemble on the worker (AC-8.1) ---------------------------------------------------
    def assemble_now(self) -> None:
        pc, entry, asm = self.pc, self.entry, self.assembler
        if pc is None or entry is None or asm is None:
            return
        gen = self._gen
        text = self.draft_of(entry)
        org = entry.cave if entry.cave is not None else entry.offset + pc_compile.FAR
        try:  # what the draft is assembled with, worked out here (UI thread)
            ctx = pc_compile.context(pc, entry, org, pc_compile.module_size(self.ctl.session))
        except Exception as exc:
            self.msgs.fail(exc, "pro_assemble")
            return
        variables = tuple(pc.variables) if entry is pc_compile.home(pc) else ()

        def work() -> AsmResult:
            return asm.assemble(text, ctx, variables)

        self.tasks.run(work, lambda r: self._assembled(gen, r), lambda e: self._failed(gen, e))

    def _assembled(self, gen: int, result: AsmResult) -> None:
        if gen != self._gen:
            return  # the source changed meanwhile
        if not result.ok:
            line, message = result.error  # type: ignore[misc]
            self.error.setText(hints.ASM_ERROR.format(line=line, message=message))
            return
        self.size = len(result.code)
        self.bytes_view.setPlainText(hex_lines(result.code))
        self.error.setText("")
        self._show_budget()

    def _failed(self, gen: int, exc: BaseException) -> None:
        if gen == self._gen:
            self.msgs.fail(exc, "pro_assemble")

    # -- cave budget (AC-8.2) ---------------------------------------------------------------
    def refresh_budget(self) -> None:
        entry, session = self.entry, self.ctl.session
        if entry is None or session.game is None:
            return
        reserved = [r for r in self.others() if entry.cave is None or r[0] != entry.cave]

        def work() -> int:
            if entry.cave is None:
                return sites.free_room(session, reserved)
            return sites.room_at(session, entry.cave, len(entry.cave_bytes), reserved)

        def done(room: int) -> None:
            if entry is self.entry:
                self.room = room
                if self.size is None:
                    self.size = len(entry.cave_bytes)
                self._show_budget()

        self.tasks.run(work, done, lambda e: self.msgs.fail(e, "pro_budget"))

    def _show_budget(self) -> None:
        if self.size is None:
            return
        if self.room is None:
            self.budget.setText(hints.CAVE_SIZE_UNKNOWN.format(size=self.size))
            return
        self.budget.setText(hints.CAVE_SIZE.format(size=self.size, room=self.room))
        if self.size > self.room:
            self.error.setText(hints.CAVE_TOO_BIG.format(size=self.size, room=self.room))

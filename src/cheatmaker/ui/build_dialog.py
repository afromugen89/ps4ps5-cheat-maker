""" "Make it work..." (spec-cheat-builder): pick an effect, press Start, play.

The dialog drives a core.builder.Builder one step at a time: each step (Check which
is real, watch, collect, one fix's test, keeping the fix) is one background job, and
questions for the player are asked here between jobs, so no worker waits on the UI.
Whatever happens (Cancel, closing, Disconnect), the Builder puts the game back:
Cancel and closing only stop the running job, whose worker puts back (no 2 s wait on
the UI thread); Disconnect waits for it, as the link must still work. The player is
never told "unchanged" after a put back the PS5 didn't take.

With a pool (the value's Find group, spec-linked-values) a passed test is judged for
members that fell behind; then the next place is built the same way while the passed
places stay in, and a place that can't be fixed in code ends in the fallback (L11).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from cheatmaker.core import builder as bd
from cheatmaker.core import cave as cv
from cheatmaker.core import linked as lk
from cheatmaker.core import real_check as rc
from cheatmaker.core.cave import Effect
from cheatmaker.core.cheats import Cheat, LocationKind
from cheatmaker.core.values import ValueType, parse_number
from cheatmaker.errors import BuildFailed, ScanCancelled
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.messages import report

log = logging.getLogger(__name__)

RealCheckDone = Callable[[dict[rc.Key, rc.Result]], None]


class _Signals(QObject):
    progress = Signal(float, str)  # worker thread -> dialog


class MakeItWorkDialog(QDialog):
    """``source``: the My cheats row being replaced (None from Find: the result is
    added). ``group``: the row's group, checked first when it has no verdicts yet;
    ``on_real_check`` gets those verdicts for the Find list. ``pool``: the row's Find
    group with factors and marks, watched for linked places. ``sleep``/``clock`` are
    for tests."""

    def __init__(
        self,
        ctl: Controller,
        *,
        address: int,
        vtype: ValueType,
        name: str,
        source: Cheat | None = None,
        group: Sequence[rc.Target] = (),
        on_real_check: RealCheckDone | None = None,
        pool: lk.LinkedGroup | None = None,
        parent=None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Make it work: {name}")
        self.ctl, self.address, self.vtype, self.name = ctl, address, vtype, name
        self.source, self.group, self.on_real_check = source, list(group), on_real_check
        self.pool = pool
        self.sleep, self.clock = sleep, clock
        self.builder: bd.Builder | None = None
        self.result_cheat: Cheat | None = None
        self._cancel: threading.Event | None = None
        self._sure_hint = ""  # "Not sure this is the real one." when Check found none
        self._current: bd.Candidate | None = None
        self._step_running = False  # a job of ours is on a worker thread
        self._rejected = False  # the dialog was closed while a job ran
        self._full_edited = False  # Full typed by the player; else read at Start
        self._released = False  # the source cheat was switched off for the watch
        self._skipped_shown = 0
        self._linked_said = False  # the L1 hint was shown
        self._pool_items: list[QListWidgetItem] = []  # the Advanced pool lines (L15)
        # Questions and the one-time notice; tests replace these.
        self.ask: Callable[[bd.Question], bd.Answer] = self._ask
        self.notice: Callable[[], bool] = self._notice
        self._signals = _Signals()
        self._signals.progress.connect(self._on_progress)

        self.hint = QLabel(hints.BUILD_READY)
        self.hint.setWordWrap(True)
        self.effects = QButtonGroup(self)
        self.effect_btns: dict[Effect, QRadioButton] = {}
        for e in Effect:
            btn = QRadioButton(e.value)
            self.effects.addButton(btn)
            self.effect_btns[e] = btn
        self.effect_btns[Effect.NEVER_DOWN].setChecked(True)
        self.only_me = QCheckBox(hints.BUILD_ONLY_ME)
        self.only_me.setToolTip(hints.BUILD_ONLY_ME_TIP)
        self.full_label = QLabel(hints.BUILD_FULL_LABEL)
        self.full = QLineEdit()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.start_btn = QPushButton("Start")
        self.cancel_btn = QPushButton("Cancel")
        self.show_advanced = QCheckBox(hints.BUILD_ADVANCED)
        self.advanced = QListWidget()
        self.advanced.setVisible(False)

        choices = QVBoxLayout()
        for btn in self.effect_btns.values():
            choices.addWidget(btn)
        full_row = QHBoxLayout()
        full_row.addWidget(self.full_label)
        full_row.addWidget(self.full, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(self.show_advanced)
        buttons.addStretch(1)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.cancel_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addLayout(choices)
        lay.addWidget(self.only_me)
        lay.addLayout(full_row)
        lay.addWidget(self.progress)
        lay.addWidget(self.advanced, 1)
        lay.addLayout(buttons)

        self.effects.buttonToggled.connect(lambda _b, _on: self._effect_changed())
        self.show_advanced.toggled.connect(self.advanced.setVisible)
        self.full.textEdited.connect(self._full_typed)
        self.start_btn.clicked.connect(self.do_start)
        self.cancel_btn.clicked.connect(self.reject)
        self._prefill_full()
        if vtype not in cv.EFFECT_TYPES:
            self.hint.setText(cv.UNSUPPORTED_TYPE)
            self.start_btn.setEnabled(False)
        self._effect_changed()

    # -- state -----------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._cancel is not None

    def effect(self) -> Effect:
        return next(e for e, btn in self.effect_btns.items() if btn.isChecked())

    def _effect_changed(self) -> None:
        full = self.effect() is Effect.ALWAYS_FULL
        self.full.setVisible(full)
        self.full_label.setVisible(full)
        if not self.busy and self.start_btn.isEnabled():
            self.hint.setText(hints.BUILD_FILL_UP if full else hints.BUILD_READY)

    def _prefill_full(self) -> None:
        """Show the value now. Only a guide: unless the player types in Full, the
        Builder reads it again at Start (after Check which is real)."""
        try:
            g = self.ctl.session.require_game()
            raw = self.ctl.session.require_client().read(g.pid, self.address, self.vtype.size)
        except Exception as exc:  # shown empty; the player can type it
            log.info("action=build_read_full reason=%s", exc)
            return
        self.full.setText(self.vtype.text(self.vtype.decode(raw)))

    def _full_typed(self, _text: str) -> None:
        self._full_edited = True

    def _set_busy(self, busy: bool) -> None:
        self._cancel = threading.Event() if busy else None
        self.start_btn.setEnabled(not busy and self.result_cheat is None)
        for w in (*self.effect_btns.values(), self.only_me, self.full):
            w.setEnabled(not busy and self.result_cheat is None)

    def _on_progress(self, fraction: float, hint: str) -> None:
        self.progress.setValue(int(fraction * 100))
        self.hint.setText(hint)

    def _add(self, text: str) -> None:
        self.advanced.addItem(text)

    def _show_pool(self) -> None:
        """L15: one Advanced line per pool member, replaced after every judge."""
        b = self.builder
        if b is None or not b.pool_rows:
            return
        for item in self._pool_items:
            self.advanced.takeItem(self.advanced.row(item))
        self._pool_items = []
        for row in b.pool_rows.values():
            m = row.member
            done = hints.BUILD_POOL_DONE[row.done.value].format(n=row.after_place)
            text = hints.BUILD_ADV_POOL.format(
                addr=f"{m.address:#x}",
                type=m.vtype.name,
                k=f"{m.factor:.6g}",
                mark=hints.BUILD_POOL_MARK[m.mark.name if m.mark else None],
                status=row.status.value,
                done=f" - {done}" if done else "",
            )
            item = QListWidgetItem(text)
            self.advanced.addItem(item)
            self._pool_items.append(item)

    # -- the one-time notice and the questions ------------------------------------------
    def _notice(self) -> bool:
        """Show "Save your game first". Returns True when the player ticked "Don't show
        this again"."""
        box = QMessageBox(
            QMessageBox.Icon.Information, hints.BUILD_NOTICE_TITLE, hints.BUILD_NOTICE
        )
        hide = QCheckBox(hints.BUILD_NOTICE_HIDE)
        box.setCheckBox(hide)
        box.exec()
        return hide.isChecked()

    def _ask(self, question: bd.Question) -> bd.Answer:
        box = QMessageBox(
            QMessageBox.Icon.Question, hints.BUILD_Q_TITLE, question.value, parent=self
        )
        yes = box.addButton(
            "Yes, I got hit" if question is bd.Question.GOT_HIT else "Yes",
            QMessageBox.ButtonRole.YesRole,
        )
        box.addButton("No", QMessageBox.ButtonRole.NoRole)
        if question is bd.Question.ENEMIES:
            box.setInformativeText(hints.BUILD_Q_ENEMIES_HINT)
            not_sure = box.addButton("Not sure", QMessageBox.ButtonRole.RejectRole)
        else:
            not_sure = None
        box.exec()
        clicked = box.clickedButton()
        if clicked is yes:
            return bd.Answer.YES
        return bd.Answer.NOT_SURE if clicked is not_sure and not_sure else bd.Answer.NO

    def _maybe_notice(self) -> None:
        s = self.ctl.settings
        if s.build_notice_hidden or self.ctl.build_notice_shown:
            return
        self.ctl.build_notice_shown = True
        if self.notice():
            s.build_notice_hidden = True
            s.save()

    # -- the steps --------------------------------------------------------------------
    def _open_rooms(self) -> list[tuple[int, int]]:
        """Code lines of the cheats in the list: a build never reuses those rooms."""
        cf = self.ctl.cheat_file
        if cf is None:
            return []
        return [
            (loc.offset, loc.offset + len(loc.on))
            for c in cf.cheats
            for loc in c.locations
            if loc.kind is LocationKind.CODE_PATCH
        ]

    def do_start(self) -> None:
        full = None
        if self.effect() is Effect.ALWAYS_FULL and self._full_edited:
            try:
                full = self.vtype.clamp(parse_number(self.full.text()))
            except ValueError:
                self.hint.setText(hints.BUILD_FULL_BAD)
                return
        self._maybe_notice()
        try:
            self.builder = bd.Builder(
                self.ctl.session,
                self.address,
                self.vtype,
                self.effect(),
                only_me=self.only_me.isChecked(),
                name=self.name,
                full=full,
                group=self.group,
                pool=self.pool,
                live=self.ctl.live,
                extra_reserved=self._open_rooms(),
                sleep=self.sleep,
                clock=self.clock,
            )
        except BuildFailed as exc:
            self.hint.setText(str(exc))
            return
        self.ctl.before_disconnect.append(self._close_builder)
        self.advanced.clear()
        self._pool_items = []
        self._linked_said = False
        self._skipped_shown = 0
        self._set_busy(True)
        if len(self.group) > 1:
            self.hint.setText(hints.BUILD_CHECKING)
            self._run(lambda b, cancel: b.real_check(cancel), self._checked)
        else:
            self._watch()

    def _run(self, step, done: Callable) -> None:
        b, cancel = self.builder, self._cancel
        assert b is not None and cancel is not None
        self._step_running = True

        def ok(value) -> None:
            self._step_running = False
            if self._rejected and isinstance(value, bd.BuildResult):
                # Kept before Cancel landed: the fix is in the game and marked on, so
                # it must be listed (it can be switched off there), never dropped.
                done(value)
            elif self._rejected:
                self._closed_meanwhile()
            else:
                done(value)

        def bad(exc: BaseException) -> None:
            self._step_running = False
            if self._rejected:
                self._closed_meanwhile()
            else:
                self._failed(exc)

        self.ctl.tasks.run(lambda: step(b, cancel), ok, bad)

    def _checked(self, hint: str | None) -> None:
        b = self.builder
        if b is not None and self.on_real_check is not None and b.real_results:
            self.on_real_check(b.real_results)
        self._sure_hint = hint or ""
        if b is not None:
            self.address = b.address
        self._watch()

    def _release(self) -> None:
        """Stop the app's own freeze on the value, or the watch would see our writes."""
        if self.source is not None and self.source.enabled:
            self.ctl.live.turn_off(self.source, restore=False)
            self._released = True
            self.ctl.cheats_changed.emit()
        addrs = [self.address]
        if self.pool is not None:  # L2: the pool's own Find freezes too
            addrs += [m.address for m in self.pool.members if m.address != self.address]
        released = 0
        for addr in addrs:
            if addr in self.ctl.frozen_addresses:
                self.ctl.freezer.unfreeze(f"find:{addr}")
                self.ctl.frozen_addresses.discard(addr)
                released += 1
        if self.builder is not None:
            self.builder.pool_released = released

    def _watch(self) -> None:
        if self._stopping():
            return
        self._release()
        self.hint.setText(" ".join(t for t in (self._sure_hint, hints.BUILD_WATCHING) if t))
        self.progress.setValue(0)
        self._run(lambda b, cancel: b.watch(cancel), self._watched)

    def _watched(self, _cands) -> None:
        b = self.builder
        assert b is not None
        if b.place == 1 and b.full is not None and not self._full_edited:
            self.full.setText(self.vtype.text(b.full))  # what Start read
        for w in b.writers:
            self._add(
                hints.BUILD_ADV_WRITER.format(addr=f"{w.address:#x}", text=w.text, hits=w.hits)
            )
        self._show_skipped()
        if self.only_me.isChecked() and not (b.place > 1 and b.reuse_filter()):
            self._collect()  # L7: once per place, unless it writes through place 1's object
        else:
            self._next()

    def _collect(self) -> None:
        if self._stopping():
            return
        self.hint.setText(hints.BUILD_COLLECTING)
        self._run(lambda b, cancel: b.collect(cancel), self._collected)

    def _show_skipped(self) -> None:
        b = self.builder
        assert b is not None
        for s in b.skipped[self._skipped_shown :]:
            self._add(
                hints.BUILD_ADV_SKIPPED.format(
                    addr=f"{s.writer.address:#x}", text=s.writer.text, reason=s.reason
                )
            )
        self._skipped_shown = len(b.skipped)

    def _collected(self, hint: str | None) -> None:
        self._show_skipped()
        if hint:  # only the player was seen: once more (the Builder stops the second time)
            self.hint.setText(hint)
            self._collect()
            return
        self._next()

    def _next(self) -> None:
        if self._stopping():
            return
        b = self.builder
        assert b is not None
        c = b.next_candidate()
        if c is None and b.place > 1:
            self._fallback()  # L11: no fix worked for this place, or a limit
            return
        if c is None:
            self._end(hints.BUILD_NONE_WORKED)
            return
        self._current = c
        progress = self._signals.progress.emit
        self._run(lambda b, cancel: b.try_candidate(c, cancel, progress), self._tried)

    def _tried(self, outcome: bd.TestOutcome) -> None:
        b = self.builder
        assert b is not None
        c = self._current
        assert c is not None
        n = len({id(t) for t, _o in b.place_tried})
        R = bd.TryResult
        if outcome.result is not R.NEEDS_ANSWER:
            result = outcome.result.value + (f": {outcome.hint}" if outcome.hint else "")
            self._add(hints.BUILD_ADV_TRIED.format(n=n, label=c.label, result=result))
        if outcome.rollback is bd.Rollback.NOT_RESTORED or b.not_restored:
            self._end(bd.RESTART)  # the fix may still be in: try nothing more
        elif outcome.result is R.PASS:
            self._passed()
        elif outcome.result is R.NEEDS_ANSWER:
            assert outcome.question is not None
            q = outcome.question
            answer = self.ask(q)
            self._run(lambda b, _cancel: b.answer(q, answer), self._tried)
        elif outcome.result is R.CANCELLED:
            self._end(hints.BUILD_CANCELLED)
        elif outcome.result is R.LOST and outcome.rollback is not bd.Rollback.RESTORED:
            self._end(outcome.hint)  # the game is gone or the PS5 isn't answering
        elif outcome.result in (R.NO_HIT, R.LOST):
            self.hint.setText(outcome.hint)
            self._next()
        else:
            self.hint.setText(hints.BUILD_FIX_FAILED.format(n=n, reason=outcome.hint))
            self._next()

    def _passed(self) -> None:
        """B8, or with a pool L1: judge the pool, then done or the next place."""
        b = self.builder
        assert b is not None
        plan = b.after_pass() if b.pool or b.has_kept else bd.LinkedPlan(True)
        self._show_pool()
        if plan.done:
            self.hint.setText(hints.BUILD_FINISHING)
            self._run(lambda b, cancel: b.finalize(cancel), self._finished)
            return
        if not self._linked_said:
            self._linked_said = True
            self.hint.setText(hints.BUILD_LINKED_BEHIND.format(name=self.name))
        self._run(lambda b, cancel: b.next_place(cancel), self._placed)

    def _placed(self, np: bd.NextPlace) -> None:
        self._show_pool()
        if np.finish:
            self.hint.setText(hints.BUILD_FINISHING)
            self._run(lambda b, cancel: b.finalize(cancel), self._finished)
        elif np.fallback_reason is not None:
            self._fallback(np.fallback_reason)
        else:
            self._watch_place(np)

    def _watch_place(self, np: bd.NextPlace) -> None:
        if self._stopping():
            return
        assert np.place is not None
        self._skipped_shown = 0
        self.hint.setText(hints.BUILD_PLACE_WATCHING.format(n=np.place, total=np.total))
        self.progress.setValue(0)
        self._run(lambda b, cancel: b.watch(cancel), self._watched)

    def _fallback(self, reason: str = "") -> None:
        """L11: keep the passed places and hold this one by a freeze (Half damage:
        keep the passed places only)."""
        if self._stopping():
            return
        b = self.builder
        assert b is not None
        if reason:
            self._add(hints.BUILD_ADV_PLACE_FAILED.format(n=b.place, reason=reason))
        self.hint.setText(hints.BUILD_FINISHING)
        self._run(lambda b, cancel: b.fallback(cancel, reason), self._finished)

    def _done_text(self, res: bd.BuildResult) -> str:
        if res.linked == "fixed":
            return hints.BUILD_DONE_PLACES.format(name=self.name, n=res.places)
        if res.linked == "partial" and self.effect() is Effect.HALF:
            return hints.BUILD_LINKED_HALF.format(name=self.name)
        if res.linked in ("freeze", "partial"):
            return hints.BUILD_LINKED_FREEZE
        return hints.BUILD_DONE.format(name=res.cheat.name, after=res.after_restart)

    def _finished(self, res: bd.BuildResult) -> None:
        self._show_pool()
        cheat = res.cheat
        cheats = self.ctl.ensure_file().cheats
        if self.source is not None and self.source in cheats:
            at = cheats.index(self.source)
            cheats[at] = cheat
        else:
            cheats.append(cheat)
        self.result_cheat = cheat
        text = self._done_text(res)
        if res.linked in ("none", "fixed") and not self.only_me.isChecked():
            text += " " + hints.BUILD_TRY_ONLY_ME
        self._end(text)
        self.progress.setValue(100)
        self.cancel_btn.setText("Close")
        self.ctl.cheats_changed.emit()
        self.ctl.message.emit(text)

    def _failed(self, exc: BaseException) -> None:
        if isinstance(exc, bd.PlaceFailed):  # a linked place: the passed ones stay (L11)
            self._fallback(str(exc))
            return
        if self.builder is not None:
            self.builder.close()  # nothing left in after a step fails; said by _end
        if isinstance(exc, ScanCancelled):
            self._end(hints.BUILD_CANCELLED)
        elif isinstance(exc, BuildFailed):
            report(exc, "build")
            self._end(str(exc))
        else:
            self._end("")
            self.ctl.fail(exc, "build")

    def _stopping(self) -> bool:
        """Cancel was pressed between two steps: stop here."""
        if self._cancel is not None and self._cancel.is_set():
            self._end(hints.BUILD_CANCELLED)
            return True
        return False

    def _end(self, text: str) -> None:
        b = self.builder
        if b is not None and self.result_cheat is None:
            if b.has_kept:  # between two steps of a linked place: put every place back
                self._close_builder()
            if b.not_restored:
                text = bd.RESTART
            b.log_end(text)
            self._give_back_source()
        if text:
            self.hint.setText(text)
        self._unregister()
        self._set_busy(False)

    def _give_back_source(self) -> None:
        """No new cheat: switch the My cheats row back on as it was before Start."""
        src = self.source
        if not self._released or src is None or not self.ctl.session.connected:
            return
        self._released = False
        if self.builder is not None and self.builder.not_restored:
            return  # don't write more over a fix that may still be in
        try:
            self.ctl.live.turn_on(src)
        except Exception as exc:  # the row stays off; the reason is logged
            report(exc, "build")
        self.ctl.cheats_changed.emit()

    # -- closing ----------------------------------------------------------------------
    def _close_builder(self) -> bd.Rollback:
        """Put back now (Disconnect, or nothing is running): waits for the job's writes."""
        if self.builder is None:
            return bd.Rollback.NOTHING
        rb = self.builder.close()
        if rb is bd.Rollback.NOT_RESTORED:
            self.ctl.message.emit(bd.RESTART)
        return rb

    def _closed_meanwhile(self) -> None:
        """The job that ran when the dialog closed has ended (and put back)."""
        try:
            self._close_builder()
        except Exception as exc:  # closing must go on; the reason is logged
            report(exc, "build")
        self._end(hints.BUILD_CANCELLED)

    def _unregister(self) -> None:
        if self._close_builder in self.ctl.before_disconnect:
            self.ctl.before_disconnect.remove(self._close_builder)

    def reject(self) -> None:
        """Cancel or close: stop the running step and put the game back. A job on a
        worker puts back itself; Disconnect still waits for it until it has ended."""
        if self._cancel is not None:
            self._cancel.set()
        if self._step_running:
            self._rejected = True
        else:
            try:
                self._close_builder()
            except Exception as exc:  # closing must go on; the reason is logged
                report(exc, "build")
            self._unregister()
        super().reject()


__all__ = ["MakeItWorkDialog"]

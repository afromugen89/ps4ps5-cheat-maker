"""Entry point: logging, crash recorder, first-run warning, main window."""

from __future__ import annotations

import logging
import sys

from cheatmaker import __version__

SELF_CHECK = "--self-check"


def self_check(out=None) -> int:
    """``CheatMaker.exe --self-check`` (the Windows build's smoke test): every screen's
    module loads (so the bundled Qt, iced-x86 and app code are all there), and the
    assembler is found. No window opens. 0 when all is well; each line printed says
    what was checked."""
    out = out or sys.stdout
    try:
        from cheatmaker.paths import nasm_exe
        from cheatmaker.settings import Settings
        from cheatmaker.ui.main_window import MainWindow  # noqa: F401 (loads every screen)
        from cheatmaker.ui.pro.pro_window import ProWindow  # noqa: F401
    except Exception as exc:  # report it: this is the smoke test's whole point
        print(f"modules: FAILED ({type(exc).__name__}: {exc})", file=out)
        return 1
    print(f"modules: ok (version {__version__})", file=out)
    nasm = nasm_exe(Settings.load().nasm_path)
    print(f"assembler: {nasm or 'MISSING'}", file=out)
    return 0 if nasm is not None else 1


def main() -> int:
    if SELF_CHECK in sys.argv[1:]:
        return self_check()
    from cheatmaker.logs import (
        check_previous_run,
        install_crash_hook,
        install_native_crash_log,
        install_qt_message_handler,
        mark_clean_exit,
        setup_logging,
    )

    path = setup_logging()
    install_crash_hook()
    unexpected = check_previous_run()  # before this run's start line, which it looks for
    install_native_crash_log(ended_unexpectedly=unexpected)
    log = logging.getLogger(__name__)
    log.info("action=start version=%s log=%s", __version__, path)

    from PySide6.QtWidgets import QApplication

    from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient
    from cheatmaker.settings import Settings
    from cheatmaker.ui.controller import Controller
    from cheatmaker.ui.main_window import MainWindow
    from cheatmaker.ui.watchdog import UiWatchdog

    install_qt_message_handler()
    app = QApplication(sys.argv)
    app.setApplicationName("Cheat Maker")
    settings = Settings.load()
    ctl = Controller(
        lambda ip, port: Ps5DebugNgClient.connect(
            ip,
            port,
            scan_connections=settings.scan_connections,
            fast_scan=settings.fast_scan,
        ),
        settings,
    )
    win = MainWindow(ctl)
    win.show()
    win.show_first_run_warning()
    watchdog = UiWatchdog(parent=app)
    watchdog.start()
    app.aboutToQuit.connect(watchdog.stop)
    code = app.exec()
    # A start without this line in the log means a hard crash or a kill.
    # crash.log stays armed until the process ends: Qt teardown can crash too.
    log.info("action=exit code=%s", code)
    mark_clean_exit()  # dumps before this line in crash.log were handled, not crashes
    return code


if __name__ == "__main__":
    sys.exit(main())

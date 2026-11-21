"""Entry point: logging, crash recorder, first-run warning, main window."""

from __future__ import annotations

import logging
import sys

from cheatmaker import __version__


def main() -> int:
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

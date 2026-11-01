"""Entry point: logging, crash hook, first-run warning, main window."""

from __future__ import annotations

import logging
import sys

from cheatmaker import __version__


def main() -> int:
    from cheatmaker.logs import install_crash_hook, setup_logging

    path = setup_logging()
    install_crash_hook()
    logging.getLogger(__name__).info("action=start version=%s log=%s", __version__, path)

    from PySide6.QtWidgets import QApplication

    from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient
    from cheatmaker.settings import Settings
    from cheatmaker.ui.controller import Controller
    from cheatmaker.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Cheat Maker")
    ctl = Controller(lambda ip, port: Ps5DebugNgClient.connect(ip, port), Settings.load())
    win = MainWindow(ctl)
    win.show()
    win.show_first_run_warning()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

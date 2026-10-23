from __future__ import annotations

import logging
import logging.handlers
import os
import sys


def _notify(text: str) -> None:
    """Visible even in a windowed exe (no console): message box on Windows, print elsewhere."""
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "Bionic Overlay", 0x40)
            return
    except Exception:
        pass
    print(text)


def main() -> int:
    # Physical-pixel mode: every coordinate (mss, Win32, Qt, OCR) is then in the same space,
    # which removes all DPI conversions on mixed-DPI multi-monitor setups.  Must be set before Qt loads.
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
    os.environ.setdefault("QT_QPA_PLATFORM", "windows:dpiawareness=2")

    from . import config, winapi

    handler = logging.handlers.RotatingFileHandler(config.config_dir() / "bionic.log", maxBytes=500_000, backupCount=2,
                                                   encoding="utf-8")
    handlers: list = [handler]
    if sys.stderr is not None:                       # a windowed exe has no stderr
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("bionic")
    sys.excepthook = lambda *a: log.critical("uncaught", exc_info=a)

    if not winapi.IS_WINDOWS:
        _notify("Bionic Overlay only runs on Windows 10/11.")
        return 1
    if not winapi.acquire_single_instance():
        _notify("Bionic Overlay is already running (check the tray icon).")
        return 0

    from PySide6.QtWidgets import QApplication

    from .controller import OverlayController
    from .settings import Settings
    from .ui import SettingsWindow, Tray

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    controller = OverlayController(Settings.load())
    window = SettingsWindow(controller)
    tray = Tray(controller, window, app)
    tray.show()
    controller.start()
    window.show()
    app.aboutToQuit.connect(controller.shutdown)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
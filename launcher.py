#!/usr/bin/env python3
"""
    python launcher.py              start the app (hides its own console when double-clicked)
    python launcher.py --check      print diagnostics (python, missing packages, OCR engines) and exit
    python launcher.py --install    install missing packages from requirements.txt, then start

From source, if packages are missing it offers to install them with pip and then starts the app.
Frozen with PyInstaller (see build_exe.bat) everything is bundled: no Python or pip needed on the user's PC.
Startup errors are written to %APPDATA%\\BionicOverlay\\launcher-error.log and shown in a message box,
because a windowed .exe has no console to print to.
"""
from __future__ import annotations

import ctypes
import importlib.util
import os
import subprocess
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, "frozen", False) else sys._MEIPASS  # type: ignore[attr-defined]
FROZEN = bool(getattr(sys, "frozen", False))
MIN_PYTHON = (3, 10)
TITLE = "Bionic Overlay"

# import name -> pip name.  (module spec checked without importing it)
REQUIRED = {"PySide6": "PySide6", "numpy": "numpy", "PIL": "Pillow", "mss": "mss"}
WINDOWS_REQUIRED = {"winrt.windows.media.ocr": "winrt-Windows.Media.Ocr", "win32com": "pywin32"}


def _msgbox(text: str, flags: int = 0x40, title: str = TITLE) -> int:
    """Win32 message box (0x10 error, 0x40 info, 0x24 yes/no question). Falls back to print()."""
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))  # type: ignore[attr-defined]
    except Exception:
        print(f"{title}: {text}")
        return 0


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def missing_packages() -> list[str]:
    need = dict(REQUIRED)
    if sys.platform == "win32":
        need.update(WINDOWS_REQUIRED)
    return sorted({pip for mod, pip in need.items() if not _has(mod)})


def _hide_own_console() -> None:
    """Hide the console only if this process created it (double-click); never hide the user's terminal."""
    try:
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        hwnd = k32.GetConsoleWindow()
        procs = (ctypes.c_uint * 4)()
        if hwnd and k32.GetConsoleProcessList(procs, 4) == 1:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # type: ignore[attr-defined]
    except Exception:
        pass


def _config_dir() -> str:
    d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "BionicOverlay")
    os.makedirs(d, exist_ok=True)
    return d


def install_missing(missing: list[str]) -> bool:
    req = os.path.join(ROOT, "requirements.txt")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    cmd += ["-r", req] if os.path.exists(req) else missing
    print("Installing:", " ".join(cmd[3:]))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
        _msgbox(f"pip failed (exit {r.returncode}):\n\n{tail}", 0x10)
        return False
    return True


def check() -> int:
    print(f"Python      : {sys.version.split()[0]} ({'frozen exe' if FROZEN else sys.executable})")
    print(f"Platform    : {sys.platform}")
    miss = missing_packages()
    print(f"Missing pkgs: {', '.join(miss) if miss else 'none'}")
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from bionic_overlay.ocr import available_engines
        print(f"OCR engines : {', '.join(available_engines()) or 'none available'}")
    except Exception as exc:
        print(f"OCR engines : could not be checked ({type(exc).__name__}: {exc})")
    return 0 if not miss else 2


def run() -> int:
    args = set(sys.argv[1:])
    if "--check" in args:
        return check()
    if sys.platform != "win32":
        _msgbox("Bionic Overlay runs on Windows 10/11 only.", 0x10)
        return 1
    if sys.version_info < MIN_PYTHON:
        _msgbox(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required (found {sys.version.split()[0]}).", 0x10)
        return 1

    if not FROZEN:
        miss = missing_packages()
        if miss or "--install" in args:
            if os.environ.get("BIONIC_LAUNCHER_RETRY"):
                _msgbox("Packages are still missing after installing:\n" + ", ".join(miss), 0x10)
                return 1
            if miss and "--install" not in args:
                ans = _msgbox("Missing packages:\n  " + "\n  ".join(miss) + "\n\nInstall them now with pip?", 0x24)
                if ans != 6:        # IDYES
                    return 1
            if not install_missing(miss):
                return 1
            os.environ["BIONIC_LAUNCHER_RETRY"] = "1"      # restart cleanly so new packages are importable
            return subprocess.call([sys.executable, os.path.abspath(__file__)] + [a for a in sys.argv[1:] if a != "--install"])
        _hide_own_console()

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from bionic_overlay.main import main
    return int(main() or 0)


if __name__ == "__main__":
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except BaseException:
        log = os.path.join(_config_dir(), "launcher-error.log")
        with open(log, "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        _msgbox(f"Bionic Overlay failed to start.\n\nDetails were written to:\n{log}", 0x10)
        sys.exit(1)
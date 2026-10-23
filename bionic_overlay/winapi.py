"""Thin ctypes wrappers for the Win32 calls the overlay needs. Safe to import anywhere."""
from __future__ import annotations

import logging
import sys
from typing import Optional

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x11
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

_mutex = None

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    _set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    _get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_long.restype = ctypes.c_ssize_t
    _set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _set_long.restype = ctypes.c_ssize_t
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]


def _h(hwnd: int):
    return wintypes.HWND(int(hwnd))


def make_click_through(hwnd: int) -> None:
    """Layered + transparent (mouse hits pass through) + no-activate + hidden from Alt-Tab."""
    if not IS_WINDOWS:
        return
    style = _get_long(_h(hwnd), GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    _set_long(_h(hwnd), GWL_EXSTYLE, style)


def set_topmost(hwnd: int) -> None:
    if IS_WINDOWS:
        user32.SetWindowPos(_h(hwnd), wintypes.HWND(-1), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def exclude_from_capture(hwnd: int) -> bool:
    """Hide our own window from screen capture (Windows 10 2004+) so OCR never sees the overlay."""
    if not IS_WINDOWS:
        return False
    ok = bool(user32.SetWindowDisplayAffinity(_h(hwnd), WDA_EXCLUDEFROMCAPTURE))
    if not ok:
        log.warning("SetWindowDisplayAffinity failed (error %s)", ctypes.get_last_error())
    return ok


def get_foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0) if IS_WINDOWS else 0


def is_window(hwnd: int) -> bool:
    return bool(IS_WINDOWS and hwnd and user32.IsWindow(_h(hwnd)))


def is_iconic(hwnd: int) -> bool:
    return bool(IS_WINDOWS and user32.IsIconic(_h(hwnd)))


def window_pid(hwnd: int) -> int:
    if not IS_WINDOWS:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(_h(hwnd), ctypes.byref(pid))
    return int(pid.value)


def window_title(hwnd: int) -> str:
    if not IS_WINDOWS:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(_h(hwnd), buf, 256)
    return buf.value


def client_rect_screen(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """Client area of a window as (x, y, w, h) in physical screen pixels."""
    if not IS_WINDOWS:
        return None
    rc, pt = wintypes.RECT(), wintypes.POINT(0, 0)
    if not user32.GetClientRect(_h(hwnd), ctypes.byref(rc)) or not user32.ClientToScreen(_h(hwnd), ctypes.byref(pt)):
        return None
    w, h = rc.right - rc.left, rc.bottom - rc.top
    return (pt.x, pt.y, w, h) if w > 0 and h > 0 else None


def acquire_single_instance(name: str = "Local\\BionicOverlaySingleton") -> bool:
    """False if another instance already holds the mutex."""
    global _mutex
    if not IS_WINDOWS:
        return True
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS

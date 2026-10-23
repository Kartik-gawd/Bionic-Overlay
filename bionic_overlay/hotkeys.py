"""Global hotkeys via Win32 RegisterHotKey on a dedicated message-loop thread."""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT, WM_APP, WM_USER = 0x0312, 0x0012, 0x8000, 0x0400

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
         "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN}
_KEYS = {
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "-": 0xBD, "minus": 0xBD, "=": 0xBB, "equals": 0xBB, "plus": 0xBB, "+": 0xBB,
    ",": 0xBC, "comma": 0xBC, ".": 0xBE, "period": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, "`": 0xC0,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+-' -> (modifier_flags, virtual_key). Raises ValueError if invalid."""
    s = spec.strip().lower().replace(" ", "")
    if not s:
        raise ValueError("empty hotkey")
    if s.endswith("+"):                    
        parts = [p for p in s[:-1].split("+") if p] + ["+"]
    else:
        parts = s.split("+")
    mods, key = 0, None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        elif key is None:
            key = p
        else:
            raise ValueError(f"more than one key in {spec!r}")
    if key is None:
        raise ValueError(f"no key in {spec!r}")
    if key in _KEYS:
        vk = _KEYS[key]
    elif len(key) == 1 and key.isalnum() and key.isascii():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"unknown key {key!r}")
    return mods, vk


def normalize_hotkey(spec: str) -> str:
    """Canonical form used to detect duplicate bindings."""
    mods, vk = parse_hotkey(spec)
    return f"{mods}:{vk}"


class HotkeyManager(threading.Thread):
    """Registers hotkeys on its own thread; `callback(name)` is invoked from that thread."""

    def __init__(self, callback: Callable[[str], None],
                 on_error: Optional[Callable[[str, str, str], None]] = None) -> None:
        super().__init__(daemon=True, name="hotkeys")
        self._callback = callback
        self._on_error = on_error or (lambda n, s, m: log.warning("hotkey %s (%s): %s", n, s, m))
        self._bindings: dict[str, str] = {}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._tid = 0
        self._ids: dict[int, str] = {}

    def set_bindings(self, bindings: dict[str, str]) -> None:
        with self._lock:
            self._bindings = dict(bindings)
        self._post(WM_APP)

    def stop(self) -> None:
        self._post(WM_QUIT)

    def _post(self, msg: int) -> None:
        if sys.platform == "win32" and self._ready.wait(2.0):
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._tid, msg, 0, 0)

    def run(self) -> None:  # pragma: no cover - Windows only
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, 0)  # create message queue
        self._tid = kernel32.GetCurrentThreadId()
        self._ready.set()

        def apply() -> None:
            for hid in list(self._ids):
                user32.UnregisterHotKey(None, hid)
            self._ids.clear()
            with self._lock:
                items = list(self._bindings.items())
            for i, (name, spec) in enumerate(items, start=1):
                if not spec:
                    continue
                try:
                    mods, vk = parse_hotkey(spec)
                except ValueError as e:
                    self._on_error(name, spec, str(e))
                    continue
                if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                    self._ids[i] = name
                else:
                    self._on_error(name, spec, f"already in use by another program (error {ctypes.get_last_error()})")

        apply()
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0 or msg.message == WM_QUIT:
                break
            if msg.message == WM_HOTKEY:
                name = self._ids.get(int(msg.wParam))
                if name:
                    try:
                        self._callback(name)
                    except Exception:
                        log.exception("hotkey callback")
            elif msg.message == WM_APP:
                apply()
        for hid in list(self._ids):
            user32.UnregisterHotKey(None, hid)

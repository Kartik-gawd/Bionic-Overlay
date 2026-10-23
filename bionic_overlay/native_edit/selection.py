"""Selection abstractions shared by all host-application providers.

A SelectionProvider talks to ONE application through its native object model
(never via keystrokes or the clipboard) and returns a SelectionContext made of
TextTargets. A TextTarget is a contiguous piece of text whose character-level
bold property can be read and written by (UTF-16) offsets.
"""
from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, ClassVar, ContextManager, Optional

log = logging.getLogger(__name__)


class SelectionUncertain(Exception):
    """Selection/editability can't be established with certainty -> do nothing."""


@dataclass(frozen=True)
class ForegroundInfo:
    hwnd: int
    pid: int
    exe: str      # lower-cased base name, e.g. "winword.exe"
    title: str


class TextTarget(ABC):
    """A run of text with read/write access to its bold property.

    All offsets are UTF-16 code units relative to the start of text()
    (the unit Office object models use), while text() itself is a normal
    Python string.
    """

    label: str = ""

    @abstractmethod
    def text(self) -> str: ...

    @abstractmethod
    def get_bold(self, start: int, end: int) -> Optional[bool]:
        """True/False if uniform over [start, end), None if mixed."""

    @abstractmethod
    def set_bold(self, start: int, end: int, value: bool) -> None: ...

    @abstractmethod
    def verify(self, start: int, end: int, expected: str) -> bool:
        """Does the host's text at [start, end) equal `expected`? (offset-mapping check)"""

    def editable_problem(self) -> Optional[str]:
        """Return a reason string if this target must not be modified, else None."""
        return None


def _null_scope() -> ContextManager[None]:
    return contextlib.nullcontext()


@dataclass
class SelectionContext:
    app: str
    key: str                                   # stable identity of the selection
    targets: list[TextTarget]
    undo_scope: Callable[[], ContextManager[None]] = _null_scope
    notes: list[str] = field(default_factory=list)


class SelectionProvider(ABC):
    name: ClassVar[str]
    exe_names: ClassVar[frozenset[str]]

    @abstractmethod
    def acquire(self, info: ForegroundInfo) -> Optional[SelectionContext]:
        """Return the live selection, None if there is none, or raise SelectionUncertain."""


class SelectionDispatcher:
    def __init__(self, providers: list[SelectionProvider]) -> None:
        self._providers = providers

    def provider_for(self, info: ForegroundInfo) -> Optional[SelectionProvider]:
        for p in self._providers:
            if info.exe in p.exe_names:
                return p
        return None


# ----------------------------------------------------------- Win32 helpers (ctypes)
def _win() -> bool:
    return sys.platform == "win32"


def pid_of_hwnd(hwnd: int) -> int:
    from ctypes import wintypes
    pid = wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))  # type: ignore[attr-defined]
    return int(pid.value)


def get_foreground_info() -> Optional[ForegroundInfo]:
    """Describe the foreground window. Returns None off-Windows or on failure."""
    if not _win():
        return None
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32.GetForegroundWindow.restype = wintypes.HWND
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = pid_of_hwnd(hwnd)
    exe = ""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                exe = buf.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        finally:
            kernel32.CloseHandle(h)
    tbuf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(wintypes.HWND(hwnd), tbuf, 512)
    return ForegroundInfo(hwnd=int(hwnd), pid=pid, exe=exe, title=tbuf.value)

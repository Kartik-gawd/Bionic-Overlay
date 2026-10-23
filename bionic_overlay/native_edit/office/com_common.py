"""Helpers shared by the Office COM providers. win32com is imported lazily so the
package (and its tests) import fine on non-Windows machines."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional, TypeVar

from ..selection import ForegroundInfo, SelectionUncertain, pid_of_hwnd

log = logging.getLogger(__name__)
T = TypeVar("T")

# RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER, VBA_E_IGNORE (Excel: cell edit mode / modal state)
BUSY_HRESULTS = {-2147418111, -2147417846, -2146777998}


def norm_bold(value: Any) -> Optional[bool]:
    """Normalise Word (-1/0/9999999), PowerPoint (-1/0/-2) and Excel (True/False/None) tri-state."""
    if value is None or isinstance(value, str):
        return None
    if value is True or value == -1 or value == 1:
        return True
    if value is False or value == 0:
        return False
    return None


def get_running_app(prog_id: str) -> Any:
    """Attach to an ALREADY RUNNING instance. Never launches the application."""
    import pywintypes
    import win32com.client
    try:
        return win32com.client.GetActiveObject(prog_id)
    except pywintypes.com_error:
        return None


def is_busy(exc: BaseException) -> bool:
    return getattr(exc, "hresult", None) in BUSY_HRESULTS or bool(
        getattr(exc, "args", None) and exc.args and exc.args[0] in BUSY_HRESULTS)


def com_call(fn: Callable[[], T], tries: int = 3, delay: float = 0.15) -> T:
    """Retry transient 'application busy' rejections; convert persistent ones to Uncertain."""
    import pywintypes
    for attempt in range(tries):
        try:
            return fn()
        except pywintypes.com_error as exc:
            if is_busy(exc):
                if attempt < tries - 1:
                    time.sleep(delay)
                    continue
                raise SelectionUncertain("application is busy or in a modal/edit state") from exc
            raise
    raise AssertionError("unreachable")


def assert_same_process(info: ForegroundInfo, app_hwnd: int, app_label: str) -> None:
    """The COM object we attached to must live in the FOREGROUND process, otherwise
    (multiple Office instances) we might format a document in the wrong window."""
    if pid_of_hwnd(int(app_hwnd)) != info.pid:
        raise SelectionUncertain(f"{app_label}: COM instance is not the foreground process "
                                 f"(multiple Office instances?)")

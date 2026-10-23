"""Runs native Office edits on ONE dedicated COM thread (latest-request-wins, ignores key mashing).

Every COM object is created, used and released on this thread, because GetActiveObject happens inside
provider.acquire().  The Qt GUI thread (which drives the overlay) is never blocked by slow COM calls.
`outcome` is emitted with an Outcome value and is delivered to the GUI thread via a queued connection."""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class NativeEditService(QObject):
    outcome = Signal(str)

    def __init__(self, cfg_provider: Callable[[], object], parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._cfg_provider = cfg_provider
        self._q: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=1)
        self._thread: Optional[threading.Thread] = None

    def request(self, mode: str = "toggle") -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="native-edit", daemon=True)
            self._thread.start()
        try:
            self._q.put_nowait(mode)
        except queue.Full:
            pass                                    # a request is already pending: drop the repeat

    def stop(self) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        from .controller import NativeEditController, Outcome
        from .office import build_dispatcher
        from .spans import BionicEngine

        try:
            import pythoncom
        except ImportError:
            log.error("pywin32 is not installed: native Office editing is unavailable (pip install pywin32)")
            while True:
                if self._q.get() is None:
                    return
                self.outcome.emit(Outcome.ERROR.value)
        pythoncom.CoInitialize()
        try:
            cfg = self._cfg_provider()
            ctl = NativeEditController(cfg, build_dispatcher(cfg))
            while True:
                mode = self._q.get()
                if mode is None:
                    break
                try:
                    cfg = self._cfg_provider()          # pick up bold ratio / rule changes
                    ctl.cfg, ctl.engine, ctl.dispatcher = cfg, BionicEngine(cfg.bionic), build_dispatcher(cfg)
                    self.outcome.emit(ctl.handle(mode).value)
                except Exception:
                    log.exception("native edit request failed")
                    self.outcome.emit(Outcome.ERROR.value)
        finally:
            pythoncom.CoUninitialize()

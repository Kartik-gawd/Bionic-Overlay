"""The transparent, always-on-top, click-through window that shows the rendered Bionic layer."""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QWidget

from . import winapi
from .geometry import Rect

log = logging.getLogger(__name__)


class OverlayWindow(QWidget):
    """Draws a pre-rendered QImage 1:1 (the window lives in physical pixels, see main.py).
    It never repaints on its own, so idle CPU use is ~0."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._image: Optional[QImage] = None
        self.capture_excluded = False
        hwnd = int(self.winId())                       # force native window creation
        winapi.make_click_through(hwnd)
        self.capture_excluded = winapi.exclude_from_capture(hwnd)
        if not self.capture_excluded:
            log.warning("Overlay cannot be excluded from capture; falling back to hide-while-capturing")

    @property
    def hwnd(self) -> int:
        return int(self.winId())

    def set_region(self, rect: Rect) -> None:
        x, y, w, h = rect.as_ints()
        self.setGeometry(x, y, w, h)
        winapi.set_topmost(self.hwnd)

    def show_overlay(self) -> None:
        self.show()
        winapi.make_click_through(self.hwnd)
        winapi.set_topmost(self.hwnd)

    def set_image(self, image: Optional[QImage]) -> None:
        self._image = image
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        if self._image is not None:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.drawImage(0, 0, self._image)
        p.end()

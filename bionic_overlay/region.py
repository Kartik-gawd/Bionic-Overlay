"""Full-desktop drag-to-select region picker (move / resize / clear / confirm)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .geometry import Rect

HANDLE = 9
MIN_SIZE = 24


class RegionSelector(QWidget):
    selected = Signal(object)    # geometry.Rect in physical screen pixels
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self._origin = QPoint(0, 0)
        self._rect: Optional[QRect] = None
        self._mode: Optional[str] = None
        self._anchor = QPoint()
        self._start_rect = QRect()
        self._handle = ""

    def start(self, virtual: Rect, initial: Optional[Rect] = None) -> None:
        x, y, w, h = virtual.as_ints()
        self._origin = QPoint(x, y)
        self.setGeometry(x, y, w, h)
        self._rect = None
        if initial:
            ix, iy, iw, ih = initial.as_ints()
            self._rect = QRect(ix - x, iy - y, iw, ih)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    # ---- helpers
    def _handles(self) -> dict[str, QRect]:
        r = self._rect
        if r is None:
            return {}
        pts = {"nw": r.topLeft(), "ne": r.topRight(), "sw": r.bottomLeft(), "se": r.bottomRight(),
               "n": QPoint(r.center().x(), r.top()), "s": QPoint(r.center().x(), r.bottom()),
               "w": QPoint(r.left(), r.center().y()), "e": QPoint(r.right(), r.center().y())}
        return {k: QRect(p.x() - HANDLE, p.y() - HANDLE, 2 * HANDLE, 2 * HANDLE) for k, p in pts.items()}

    def cancel(self) -> None:
        self.hide()
        self.cancelled.emit()

    def confirm(self) -> None:
        r = self._rect
        if r is None or r.width() < MIN_SIZE or r.height() < MIN_SIZE:
            return
        g = r.translated(self._origin)
        self.hide()
        self.selected.emit(Rect(g.x(), g.y(), g.width(), g.height()))

    # ---- events
    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        p.setPen(QColor(255, 255, 255, 230))
        p.drawText(24, 36, "Drag to select the reading area  -  drag inside to move, handles to resize  -  "
                           "Enter / double-click to confirm  -  right-click to clear  -  Esc to cancel")
        if self._rect is not None:
            r = self._rect.normalized()
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(r, QColor(0, 0, 0, 1))        # alpha 1: stays mouse-opaque but looks clear
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(QPen(QColor(0, 190, 255), 2))
            p.drawRect(r)
            p.setBrush(QColor(0, 190, 255))
            for hr in self._handles().values():
                c = hr.center()
                p.drawRect(QRect(c.x() - 4, c.y() - 4, 8, 8))
            p.setPen(QColor(255, 255, 255))
            p.drawText(r.left() + 4, max(14, r.top() - 6), f"{r.width()} x {r.height()}")
        p.end()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        pos = e.position().toPoint()
        if e.button() == Qt.MouseButton.RightButton:
            self._rect = None
            self.update()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._anchor, self._start_rect = pos, QRect(self._rect) if self._rect else QRect()
        for name, hr in self._handles().items():
            if hr.contains(pos):
                self._mode, self._handle = "resize", name
                return
        if self._rect is not None and self._rect.normalized().contains(pos):
            self._mode = "move"
        else:
            self._mode = "new"
            self._rect = QRect(pos, pos)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        pos = e.position().toPoint()
        if not self._mode or self._rect is None:
            return
        d = pos - self._anchor
        if self._mode == "new":
            self._rect = QRect(self._anchor, pos)
        elif self._mode == "move":
            self._rect = self._start_rect.translated(d)
        else:
            r = QRect(self._start_rect.normalized())
            if "n" in self._handle: r.setTop(r.top() + d.y())
            if "s" in self._handle: r.setBottom(r.bottom() + d.y())
            if "w" in self._handle: r.setLeft(r.left() + d.x())
            if "e" in self._handle: r.setRight(r.right() + d.x())
            self._rect = r
        self.update()

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        if self._rect is not None:
            self._rect = self._rect.normalized()
        self._mode = None
        self.update()

    def mouseDoubleClickEvent(self, _e) -> None:  # noqa: N802
        self.confirm()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self.cancel()
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.confirm()
        elif e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._rect = None
            self.update()

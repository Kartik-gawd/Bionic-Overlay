"""Tiny geometry helper shared by capture, OCR and rendering (no Qt dependency)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle. Screen rectangles use int-valued physical pixels;
    OCR boxes use floats in capture-image pixels."""

    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def is_valid(self) -> bool:
        return self.w > 0 and self.h > 0

    def union(self, o: "Rect") -> "Rect":
        x0, y0 = min(self.x, o.x), min(self.y, o.y)
        return Rect(x0, y0, max(self.right, o.right) - x0, max(self.bottom, o.bottom) - y0)

    def intersection(self, o: "Rect") -> Optional["Rect"]:
        x0, y0 = max(self.x, o.x), max(self.y, o.y)
        x1, y1 = min(self.right, o.right), min(self.bottom, o.bottom)
        if x1 <= x0 or y1 <= y0:
            return None
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def vertical_overlap(self, o: "Rect") -> float:
        """Overlap height divided by the smaller of the two heights (0..1)."""
        ov = min(self.bottom, o.bottom) - max(self.y, o.y)
        m = min(self.h, o.h)
        return max(0.0, ov) / m if m > 0 else 0.0

    def horizontal_overlap(self, o: "Rect") -> float:
        ov = min(self.right, o.right) - max(self.x, o.x)
        m = min(self.w, o.w)
        return max(0.0, ov) / m if m > 0 else 0.0

    def scaled(self, sx: float, sy: Optional[float] = None) -> "Rect":
        sy = sx if sy is None else sy
        return Rect(self.x * sx, self.y * sy, self.w * sx, self.h * sy)

    def translated(self, dx: float, dy: float) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def expanded(self, px: float, py: Optional[float] = None) -> "Rect":
        py = px if py is None else py
        return Rect(self.x - px, self.y - py, self.w + 2 * px, self.h + 2 * py)

    def as_ints(self) -> tuple[int, int, int, int]:
        return int(round(self.x)), int(round(self.y)), int(round(self.w)), int(round(self.h))

    def to_list(self) -> list[int]:
        return list(self.as_ints())

    @classmethod
    def from_list(cls, v: Optional[Sequence[float]]) -> Optional["Rect"]:
        if not v or len(v) != 4:
            return None
        r = cls(*[float(n) for n in v])
        return r if r.is_valid() else None

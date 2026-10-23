"""Screen capture (mss) and change detection (numpy only)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .geometry import Rect


class ScreenCapture:
    """GDI BitBlt capture via `mss`.  Create and use on one thread (the Qt main thread)."""

    def __init__(self) -> None:
        self._sct = None

    def _get(self):
        if self._sct is None:
            import mss

            self._sct = mss.mss()
        return self._sct

    def monitors(self) -> list[Rect]:
        """[virtual desktop, monitor 1, monitor 2, ...] in physical pixels."""
        return [Rect(m["left"], m["top"], m["width"], m["height"]) for m in self._get().monitors]

    def virtual_screen(self) -> Rect:
        return self.monitors()[0]

    def grab(self, rect: Rect) -> np.ndarray:
        x, y, w, h = rect.as_ints()
        shot = self._get().grab({"left": x, "top": y, "width": w, "height": h})
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        return np.ascontiguousarray(arr[:, :, :3])  # BGR

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None


@dataclass
class ChangeResult:
    stale: bool     # on-screen content differs from what the overlay was built for
    trigger: bool   # run OCR now


class ChangeDetector:
    """Decides *when* OCR should run.

    - compares a cheap sub-sampled green channel against the frame the last OCR was run on
    - while content is still moving (scrolling, video) it only reports `stale`
    - once the content has been still for `debounce_s` it fires one OCR trigger
    - never fires more often than `min_interval_s`
    """

    def __init__(self, stride: int = 2, pixel_threshold: int = 24, change_fraction: float = 0.01,
                 debounce_s: float = 0.25, min_interval_s: float = 0.3) -> None:
        self.stride = stride
        self.pixel_threshold = pixel_threshold
        self.change_fraction = change_fraction
        self.debounce_s = debounce_s
        self.min_interval_s = min_interval_s
        self.reset()

    def configure(self, *, change_fraction: Optional[float] = None, debounce_s: Optional[float] = None,
                  min_interval_s: Optional[float] = None) -> None:
        if change_fraction is not None:
            self.change_fraction = change_fraction
        if debounce_s is not None:
            self.debounce_s = debounce_s
        if min_interval_s is not None:
            self.min_interval_s = min_interval_s

    def reset(self) -> None:
        self._ref: Optional[np.ndarray] = None
        self._prev: Optional[np.ndarray] = None
        self._pending = False
        self._last_motion = 0.0
        self._last_trigger = -1e9
        self._force = False

    def force(self) -> None:
        """Make the next update() trigger OCR regardless of change state."""
        self._force = True

    def _small(self, frame: np.ndarray) -> np.ndarray:
        return frame[:: self.stride, :: self.stride, 1].astype(np.int16)

    def _fraction(self, a: np.ndarray, b: np.ndarray) -> float:
        if a.shape != b.shape:
            return 1.0
        return float(np.mean(np.abs(a - b) > self.pixel_threshold))

    def update(self, frame: np.ndarray, now: float) -> ChangeResult:
        small = self._small(frame)
        if self._ref is None or self._force:
            self._ref = self._prev = small
            self._pending, self._force = False, False
            self._last_trigger = now
            return ChangeResult(stale=False, trigger=True)

        moving = self._fraction(small, self._prev) > self.change_fraction
        self._prev = small
        if self._fraction(small, self._ref) <= self.change_fraction:
            self._pending = False
            return ChangeResult(False, False)

        if not self._pending or moving:
            self._pending = True
            self._last_motion = now
        settled = now - self._last_motion >= self.debounce_s
        due = now - self._last_trigger >= self.min_interval_s
        if settled and due:
            self._ref = small
            self._pending = False
            self._last_trigger = now
            return ChangeResult(stale=True, trigger=True)
        return ChangeResult(stale=True, trigger=False)

"""OCR engine interface and the data types every engine must return."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np

from ..geometry import Rect

RGB = tuple[int, int, int]


@dataclass
class OcrWord:
    text: str
    box: Rect                       # capture-image pixel coordinates
    confidence: float = 1.0         # 0..1 (only meaningful if result.has_confidence)
    line_id: Optional[int] = None   # engine-provided line grouping, if any
    fg: Optional[RGB] = None        # sampled text colour (filled in by layout stage)
    bg: Optional[RGB] = None        # sampled background colour
    bg_ok: bool = True              # background uniform enough to patch (False: gradient/image/selection)
    stroke: Optional[float] = None  # stroke width / cap height; None if not measurable
    underline: bool = False
    skip: str = ""                  # non-empty: leave this word untouched ("bold", "bold-line", ...)


@dataclass
class OCRResult:
    words: list[OcrWord]
    width: int
    height: int
    engine: str = ""
    elapsed_ms: float = 0.0
    has_confidence: bool = False
    has_line_ids: bool = False


class OcrEngine(ABC):
    """Swap-in OCR backend.  `recognize` receives a BGR uint8 array (H, W, 3) and must
    return word boxes in that array's pixel coordinates.  It is always called from the
    single OCR worker thread, so engines may keep thread-bound state (event loops, etc.)."""

    name = "base"
    has_confidence = False
    has_line_ids = False
    max_dimension: Optional[int] = None   # largest image side the engine accepts

    @classmethod
    def is_available(cls) -> bool:
        return True

    @abstractmethod
    def recognize(self, image: np.ndarray) -> OCRResult: ...

    def close(self) -> None:  # pragma: no cover - optional
        pass


def split_line_into_words(text: str, box: Rect, line_id: Optional[int] = None,
                          confidence: float = 1.0) -> list[OcrWord]:
    """For engines that only return *line* boxes (e.g. PaddleOCR): estimate per-word boxes
    by sharing the line width in proportion to estimated glyph widths.  Approximate by design;
    the renderer's per-word width fitting hides most of the error."""
    tokens = text.split()
    if not tokens or not box.is_valid():
        return []

    narrow, wide = set("iljtfI.,;:'|!1()[]"), set("mwMW@")

    def weight(tok: str) -> float:
        return sum(0.45 if c in narrow else 1.3 if c in wide else 1.0 for c in tok)

    space = 0.35
    total = sum(weight(t) for t in tokens) + space * (len(tokens) - 1)
    x, out = box.x, []
    for t in tokens:
        w = box.w * weight(t) / total
        out.append(OcrWord(t, Rect(x, box.y, w, box.h), confidence, line_id))
        x += w + box.w * space / total
    return out

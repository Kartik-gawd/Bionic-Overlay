"""PaddleOCR engine (optional, heavy; best accuracy on stylised / low-contrast text).

PaddleOCR returns *line* boxes, so words are estimated with split_line_into_words().
Supports both the 3.x (`predict`) and 2.x (`ocr`) APIs.  Written against the public docs
and not verified on every release - treat as experimental."""
from __future__ import annotations

import importlib.util
import time

import numpy as np

from ..geometry import Rect
from .base import OCRResult, OcrEngine, split_line_into_words


class PaddleEngine(OcrEngine):
    name = "paddle"
    has_confidence = True
    has_line_ids = True

    def __init__(self, lang: str = "en") -> None:
        self.lang = lang
        self._ocr = None

    @classmethod
    def is_available(cls) -> bool:
        return importlib.util.find_spec("paddleocr") is not None

    def _ensure(self) -> None:
        if self._ocr is None:
            from paddleocr import PaddleOCR

            try:  # 3.x
                self._ocr = PaddleOCR(lang=self.lang, use_doc_orientation_classify=False,
                                      use_doc_unwarping=False, use_textline_orientation=False)
            except TypeError:  # 2.x
                self._ocr = PaddleOCR(lang=self.lang, use_angle_cls=False, show_log=False)

    def _lines(self, image: np.ndarray):
        """Yield (text, score, (x0, y0, x1, y1))."""
        if hasattr(self._ocr, "predict"):
            for res in self._ocr.predict(image):
                data = res["res"] if "res" in res else res
                polys = data.get("rec_polys") if data.get("rec_polys") is not None else data.get("dt_polys", [])
                for t, s, p in zip(data["rec_texts"], data["rec_scores"], polys):
                    p = np.asarray(p)
                    yield t, float(s), (p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max())
        else:
            for page in self._ocr.ocr(image, cls=False) or []:
                for poly, (t, s) in page or []:
                    p = np.asarray(poly)
                    yield t, float(s), (p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max())

    def recognize(self, image: np.ndarray) -> OCRResult:
        self._ensure()
        t0 = time.perf_counter()
        h, w = image.shape[:2]
        words = []
        for li, (text, score, (x0, y0, x1, y1)) in enumerate(self._lines(image)):
            words.extend(split_line_into_words(text, Rect(x0, y0, x1 - x0, y1 - y0), li, score))
        return OCRResult(words, w, h, self.name, (time.perf_counter() - t0) * 1000, True, True)

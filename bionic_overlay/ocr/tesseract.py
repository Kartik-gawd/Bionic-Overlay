"""Tesseract engine via pytesseract (needs the Tesseract binary installed)."""
from __future__ import annotations

import importlib.util
import os
import shutil
import time

import numpy as np

from ..geometry import Rect
from .base import OCRResult, OcrEngine, OcrWord

DEFAULT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


class TesseractEngine(OcrEngine):
    name = "tesseract"
    has_confidence = True
    has_line_ids = True

    def __init__(self, cmd: str = "", lang: str = "eng", psm: int = 3) -> None:
        self.cmd = cmd or (DEFAULT_EXE if os.path.exists(DEFAULT_EXE) else "")
        self.lang, self.psm = lang or "eng", psm

    @classmethod
    def is_available(cls) -> bool:
        return importlib.util.find_spec("pytesseract") is not None and (
            shutil.which("tesseract") is not None or os.path.exists(DEFAULT_EXE))

    def recognize(self, image: np.ndarray) -> OCRResult:
        import pytesseract
        from PIL import Image

        if self.cmd:
            pytesseract.pytesseract.tesseract_cmd = self.cmd
        t0 = time.perf_counter()
        h, w = image.shape[:2]
        d = pytesseract.image_to_data(Image.fromarray(image[:, :, ::-1]), lang=self.lang,
                                      config=f"--psm {self.psm}", output_type=pytesseract.Output.DICT)
        line_ids: dict[tuple, int] = {}
        words: list[OcrWord] = []
        for i, text in enumerate(d["text"]):
            conf = float(d["conf"][i])
            if not text.strip() or conf < 0:
                continue
            key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            lid = line_ids.setdefault(key, len(line_ids))
            words.append(OcrWord(text, Rect(d["left"][i], d["top"][i], d["width"][i], d["height"][i]),
                                 conf / 100.0, lid))
        return OCRResult(words, w, h, self.name, (time.perf_counter() - t0) * 1000, True, True)

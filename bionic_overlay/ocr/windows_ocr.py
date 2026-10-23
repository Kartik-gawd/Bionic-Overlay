"""Windows.Media.Ocr engine (offline, built into Windows 10/11) via the pywinrt packages."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time

import numpy as np

from ..geometry import Rect
from .base import OCRResult, OcrEngine, OcrWord


class WindowsOcrEngine(OcrEngine):
    name = "windows"
    has_confidence = False      # the WinRT API exposes no confidence values
    has_line_ids = True

    def __init__(self, language: str = "") -> None:
        self._language = language
        self._engine = None
        self._loop = None
        self.max_dimension = None

    @classmethod
    def is_available(cls) -> bool:
        if sys.platform != "win32":
            return False
        try:
            return importlib.util.find_spec("winrt.windows.media.ocr") is not None
        except (ImportError, ValueError):
            return False

    def _ensure(self) -> None:
        if self._engine is not None:
            return
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine as WinOcr

        eng = None
        if self._language:
            lang = Language(self._language)
            if WinOcr.is_language_supported(lang):
                eng = WinOcr.try_create_from_language(lang)
        if eng is None:
            eng = WinOcr.try_create_from_user_profile_languages()
        if eng is None:
            raise RuntimeError(
                "No Windows OCR language available. Install one: Settings > Time & language > "
                "Language & region > Add a language (with 'Optical character recognition')."
            )
        self._engine = eng
        self.max_dimension = int(WinOcr.max_image_dimension)
        self._loop = asyncio.new_event_loop()

    def recognize(self, image: np.ndarray) -> OCRResult:
        self._ensure()
        t0 = time.perf_counter()
        h, w = image.shape[:2]
        bgra = np.dstack([image[:, :, :3], np.full((h, w), 255, np.uint8)])
        words = self._loop.run_until_complete(self._run(bgra, w, h))
        return OCRResult(words, w, h, self.name, (time.perf_counter() - t0) * 1000,
                         has_confidence=False, has_line_ids=True)

    async def _run(self, bgra: np.ndarray, w: int, h: int) -> list[OcrWord]:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter

        writer = DataWriter()
        writer.write_bytes(bgra.tobytes())
        bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h)
        result = await self._engine.recognize_async(bitmap)
        out: list[OcrWord] = []
        for li, line in enumerate(result.lines):
            for word in line.words:
                r = word.bounding_rect
                out.append(OcrWord(word.text, Rect(r.x, r.y, r.width, r.height), 1.0, li))
        return out

    def close(self) -> None:
        if self._loop is not None:
            self._loop.close()
        self._loop, self._engine = None, None

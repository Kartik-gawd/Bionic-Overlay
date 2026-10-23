from __future__ import annotations

from .base import OcrEngine


def create_engine(name: str, language: str = "", tesseract_cmd: str = "", tesseract_lang: str = "eng") -> OcrEngine:
    if name == "windows":
        from .windows_ocr import WindowsOcrEngine
        if not WindowsOcrEngine.is_available():
            raise RuntimeError("Windows OCR unavailable: pip install the winrt-* packages (see requirements.txt)")
        return WindowsOcrEngine(language)
    if name == "tesseract":
        from .tesseract import TesseractEngine
        if not TesseractEngine.is_available():
            raise RuntimeError("Tesseract unavailable: pip install pytesseract and install the Tesseract binary")
        return TesseractEngine(tesseract_cmd, tesseract_lang)
    if name == "paddle":
        from .paddle import PaddleEngine
        if not PaddleEngine.is_available():
            raise RuntimeError("PaddleOCR unavailable: pip install paddleocr paddlepaddle")
        return PaddleEngine((language or "en").split("-")[0])
    raise ValueError(f"unknown OCR engine {name!r}")


def available_engines() -> list[str]:
    from .paddle import PaddleEngine
    from .tesseract import TesseractEngine
    from .windows_ocr import WindowsOcrEngine

    pairs = (("windows", WindowsOcrEngine), ("tesseract", TesseractEngine), ("paddle", PaddleEngine))
    return [n for n, c in pairs if c.is_available()]

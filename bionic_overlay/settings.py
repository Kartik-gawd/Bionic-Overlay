"""Persistent user settings with validation/clamping."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger(__name__)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


@dataclass
class Settings:
    # capture
    capture_mode: str = "region"
    region: Optional[list] = None            # [x, y, w, h] physical pixels
    monitor_index: int = 1                   # for capture_mode == "screen" (1 = primary)
    window_hide_in_background: bool = True   # window mode: hide overlay when target isn't foreground
    # OCR
    ocr_engine: str = "windows"
    ocr_language: str = ""                   # BCP-47 tag, "" = user profile languages
    tesseract_cmd: str = ""
    tesseract_lang: str = "eng"
    ocr_scale: float = 1.0                   # upscale before OCR (small text benefits from 1.5-2)
    ocr_interval_ms: int = 400               # screen polling interval == minimum gap between OCR runs
    debounce_ms: int = 250                   # content must be still this long before OCR
    change_fraction: float = 0.01            # fraction of sampled pixels that must change
    min_confidence: float = 0.5
    hide_on_change: bool = True              # blank the overlay while content is changing
    skip_bold_words: bool = True             # leave words that are already bold (stroke-width test)
    bold_stroke_abs: float = 0.17            # stroke/cap-height above this = bold
    bold_stroke_rel: float = 1.3             # ... or this multiple of the paragraph median
    skip_colored_words: bool = False         # links / syntax highlighting
    skip_underlined_words: bool = False
    # bionic
    bold_ratio: float = 0.45
    min_word_length: int = 1
    short_word_max: int = 3
    short_word_mode: str = "ratio"
    bold_numbers: bool = False
    bold_urls: bool = False
    bold_emails: bool = False
    bold_symbols: bool = False
    bold_cjk: bool = False
    # rendering / calibration
    font_family: str = "Segoe UI"
    font_scale: float = 1.0
    offset_x: int = 0
    offset_y: int = 0
    opacity: float = 1.0
    line_spacing: float = 1.0
    word_spacing: float = 1.0
    fit_width: bool = True
    text_align: str = "left"
    background_mode: str = "embolden"
    embolden_strength: float = 1.0
    embolden_min_xheight: float = 10.0
    mask_opacity: float = 0.85
    text_color: str = "auto"                 # colour of the bold letters: "auto" (match the text) or "#rrggbb"
    debug_boxes: bool = False
    # native Office editing (Word / PowerPoint / Excel object model)
    native_edit_enabled: bool = True
    selection_fallback_to_overlay: bool = True
    max_selection_chars: int = 50000
    require_restorable: bool = True
    ppt_include_selected_shapes: bool = False
    show_toasts: bool = True
    # hotkeys
    hotkeys: dict = field(default_factory=lambda: dict(config.DEFAULT_HOTKEYS))

    def validate(self) -> "Settings":
        def choice(name, allowed):
            if getattr(self, name) not in allowed:
                setattr(self, name, allowed[0])

        choice("capture_mode", config.CAPTURE_MODES)
        choice("ocr_engine", config.OCR_ENGINES)
        choice("background_mode", config.BACKGROUND_MODES)
        choice("short_word_mode", config.SHORT_WORD_MODES)
        choice("text_align", config.TEXT_ALIGNS)
        self.monitor_index = int(_clamp(self.monitor_index, 1, 16))
        self.ocr_scale = float(_clamp(self.ocr_scale, 0.5, 4.0))
        self.ocr_interval_ms = int(_clamp(self.ocr_interval_ms, 100, 10000))
        self.debounce_ms = int(_clamp(self.debounce_ms, 0, 5000))
        self.change_fraction = float(_clamp(self.change_fraction, 0.0005, 0.5))
        self.min_confidence = float(_clamp(self.min_confidence, 0.0, 1.0))
        self.bold_ratio = float(_clamp(self.bold_ratio, 0.0, 1.0))
        self.min_word_length = int(_clamp(self.min_word_length, 1, 10))
        self.short_word_max = int(_clamp(self.short_word_max, 0, 6))
        self.font_scale = float(_clamp(self.font_scale, 0.3, 3.0))
        self.offset_x = int(_clamp(self.offset_x, -200, 200))
        self.offset_y = int(_clamp(self.offset_y, -200, 200))
        self.opacity = float(_clamp(self.opacity, 0.05, 1.0))
        self.line_spacing = float(_clamp(self.line_spacing, 0.5, 2.0))
        self.word_spacing = float(_clamp(self.word_spacing, 0.5, 2.0))
        self.mask_opacity = float(_clamp(self.mask_opacity, 0.0, 1.0))
        self.embolden_strength = float(_clamp(self.embolden_strength, 0.2, 4.0))
        tc = str(self.text_color or "auto").strip()
        self.text_color = tc.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", tc) else "auto"   # "auto" or "#rrggbb"
        self.embolden_min_xheight = float(_clamp(self.embolden_min_xheight, 4.0, 40.0))
        self.bold_stroke_abs = float(_clamp(self.bold_stroke_abs, 0.05, 0.6))
        self.bold_stroke_rel = float(_clamp(self.bold_stroke_rel, 1.05, 3.0))
        self.max_selection_chars = int(_clamp(self.max_selection_chars, 100, 500000))
        if self.region is not None and (not isinstance(self.region, list) or len(self.region) != 4
                                        or self.region[2] <= 0 or self.region[3] <= 0):
            self.region = None
        merged = dict(config.DEFAULT_HOTKEYS)
        merged.update({k: v for k, v in (self.hotkeys or {}).items() if k in merged and isinstance(v, str)})
        self.hotkeys = merged
        return self

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names}).validate()

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        path = path or config.settings_path()
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except FileNotFoundError:
            return cls().validate()
        except Exception:
            log.exception("Could not read settings; using defaults")
            return cls().validate()

    def save(self, path: Optional[Path] = None) -> None:
        path = Path(path or config.settings_path())
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)
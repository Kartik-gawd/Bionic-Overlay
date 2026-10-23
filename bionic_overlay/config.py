"""Constants, paths and defaults."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "BionicOverlay"

OCR_ENGINES = ("windows", "tesseract", "paddle")
CAPTURE_MODES = ("region", "window", "screen")
BACKGROUND_MODES = ("embolden", "mask", "contrast", "replace", "transparent")
SHORT_WORD_MODES = ("ratio", "first", "none", "all")
TEXT_ALIGNS = ("left", "center", "right")

DEFAULT_HOTKEYS = {
    "toggle": "ctrl+alt+o",
    "refresh": "ctrl+alt+r",
    "pause": "ctrl+alt+p",
    "bold_down": "ctrl+alt+-",
    "bold_up": "ctrl+alt+=",
    "select_region": "ctrl+alt+s",
    "select_window": "ctrl+alt+w",
    "toggle_debug": "ctrl+alt+d",
    "bionic_selection": "ctrl+alt+b",   # native bold in Word/PowerPoint/Excel (falls back to the overlay)
    "restore_selection": "",            # optional: restore-only for native edits
    "disable": "esc",
}

HOTKEY_LABELS = {
    "toggle": "Toggle overlay",
    "refresh": "Re-OCR / refresh",
    "pause": "Pause / resume",
    "bold_down": "Decrease bold ratio",
    "bold_up": "Increase bold ratio",
    "select_region": "Select region",
    "select_window": "Use foreground window as target",
    "toggle_debug": "Toggle alignment debug boxes",
    "bionic_selection": "Bionic-format selection (Office) / overlay fallback",
    "restore_selection": "Restore selection formatting (Office)",
    "disable": "Disable overlay (only grabbed while active)",
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_path() -> Path:
    return config_dir() / "settings.json"

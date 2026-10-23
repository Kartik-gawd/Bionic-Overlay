"""Configuration: flat JSON file -> typed dataclasses, with strict validation."""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


class ConfigError(ValueError):
    """Raised for invalid or unknown configuration values."""


@dataclass
class BionicConfig:
    """Controls the emphasis algorithm (pure text -> spans)."""

    bold_ratio: float = 0.45            # fraction of meaningful characters to embolden
    minimum_word_length: int = 3        # words with fewer meaningful chars are "short"
    format_short_words: bool = False    # if True, short words get >=1 emphasized char
    format_numbers: bool = False        # if False, purely numeric tokens are untouched
    ignore_urls: bool = True
    ignore_emails: bool = True
    skip_unspaced_scripts: bool = True  # CJK/Thai/... have no word spaces: leave alone

    def validate(self) -> None:
        if not (0.0 < self.bold_ratio < 1.0):
            raise ConfigError(f"bold_ratio must be in (0, 1), got {self.bold_ratio}")
        if self.minimum_word_length < 1:
            raise ConfigError("minimum_word_length must be >= 1")


@dataclass
class HotkeyConfig:
    hotkey: str = "Ctrl+Alt+B"
    toggle: bool = True                 # pressing again on the same selection restores
    restore_hotkey: Optional[str] = None  # optional dedicated "restore only" hotkey
    beep: bool = False                  # audible feedback on success


@dataclass
class OfficeConfig:
    max_selection_chars: int = 50_000   # refuse bigger selections (COM is slow)
    require_restorable: bool = True     # refuse to modify if original state can't be captured
    max_snapshot_calls: int = 4_000     # COM-call budget for capturing original formatting
    verify_sample_every: int = 25       # spot-check every Nth span's text before writing
    ppt_include_selected_shapes: bool = False  # PowerPoint: also handle whole selected shapes
    excel_max_cells: int = 2_000


@dataclass
class AppConfig:
    bionic: BionicConfig = field(default_factory=BionicConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    office: OfficeConfig = field(default_factory=OfficeConfig)

    def validate(self) -> None:
        self.bionic.validate()


def from_dict(data: dict[str, Any]) -> AppConfig:
    cfg = AppConfig()
    sections = {"bionic": cfg.bionic, "hotkey": cfg.hotkey, "office": cfg.office}
    owner: dict[str, Any] = {}
    for obj in sections.values():
        for f in dataclasses.fields(obj):
            owner[f.name] = obj
    for key, value in data.items():
        if key not in owner:
            raise ConfigError(f"Unknown configuration key: {key!r}")
        setattr(owner[key], key, value)
    cfg.validate()
    return cfg


def from_settings(s) -> AppConfig:
    """Build the native-edit config from the overlay's single Settings object, so the bold ratio and
    word rules are shared between the overlay and Word/PowerPoint/Excel editing."""
    cfg = AppConfig()
    cfg.bionic = BionicConfig(
        bold_ratio=min(0.99, max(0.01, s.bold_ratio)),
        minimum_word_length=s.min_word_length,      # words shorter than this are left alone ...
        format_short_words=False,                   # ... (min_word_length=1 means nothing is 'short')
        format_numbers=s.bold_numbers,
        ignore_urls=not s.bold_urls,
        ignore_emails=not s.bold_emails,
        skip_unspaced_scripts=not s.bold_cjk,
    )
    cfg.office = OfficeConfig(max_selection_chars=s.max_selection_chars,
                              require_restorable=s.require_restorable,
                              ppt_include_selected_shapes=s.ppt_include_selected_shapes)
    return cfg

from __future__ import annotations

from ..config import AppConfig
from ..selection import SelectionDispatcher


def build_dispatcher(cfg: AppConfig) -> SelectionDispatcher:
    from .excel import ExcelProvider
    from .powerpoint import PowerPointProvider
    from .word import WordProvider
    return SelectionDispatcher([WordProvider(cfg.office), PowerPointProvider(cfg.office), ExcelProvider(cfg.office)])

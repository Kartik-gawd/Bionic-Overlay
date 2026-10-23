#PowerPoint provider: formats the selected TextRange (or, opt-in, whole selected shapes)
from __future__ import annotations

import logging
from typing import Any, Optional

from ..spans import utf16_len
from ..config import OfficeConfig
from ..selection import (ForegroundInfo, SelectionContext, SelectionProvider,
                         SelectionUncertain, TextTarget)
from .com_common import assert_same_process, com_call, get_running_app, norm_bold

log = logging.getLogger(__name__)

PP_SELECTION_SHAPES, PP_SELECTION_TEXT = 2, 3
MSO_TRUE, MSO_FALSE = -1, 0


class PptTarget(TextTarget):
    #TextRange.Characters(Start, Length) is 1-based and relative to the range.

    def __init__(self, tr: Any, text: str, label: str) -> None:
        self._tr, self._text, self.label = tr, text, label

    def _chars(self, a: int, b: int) -> Any:
        return self._tr.Characters(a + 1, b - a)

    def text(self) -> str:
        return self._text

    def get_bold(self, start: int, end: int) -> Optional[bool]:
        return norm_bold(self._chars(start, end).Font.Bold)   # -2 (mixed) -> None

    def set_bold(self, start: int, end: int, value: bool) -> None:
        self._chars(start, end).Font.Bold = MSO_TRUE if value else MSO_FALSE

    def verify(self, start: int, end: int, expected: str) -> bool:
        return self._chars(start, end).Text == expected


class PowerPointProvider(SelectionProvider):
    name = "PowerPoint"
    exe_names = frozenset({"powerpnt.exe"})

    def __init__(self, cfg: OfficeConfig) -> None:
        self.cfg = cfg

    def acquire(self, info: ForegroundInfo) -> Optional[SelectionContext]:
        app = get_running_app("PowerPoint.Application")
        if app is None:
            raise SelectionUncertain("PowerPoint is foreground but no COM instance is registered")
        try:
            assert_same_process(info, com_call(lambda: app.HWND), "PowerPoint")
            if com_call(lambda: app.Windows.Count) == 0:
                return None
            win = com_call(lambda: app.ActiveWindow)
            sel = com_call(lambda: win.Selection)
            stype = com_call(lambda: sel.Type)
        except SelectionUncertain:
            raise
        except Exception as exc:  # e.g. Protected View windows expose no ActiveWindow
            raise SelectionUncertain(f"cannot read PowerPoint selection ({exc})") from exc

        targets: list[PptTarget] = []
        if stype == PP_SELECTION_TEXT:
            tr = sel.TextRange
            if int(tr.Length) == 0:
                return None
            t = self._target(tr, "ppt:text")
            if t:
                targets.append(t)
        elif stype == PP_SELECTION_SHAPES and self.cfg.ppt_include_selected_shapes:
            shapes = sel.ShapeRange
            for i in range(1, int(shapes.Count) + 1):
                sh = shapes.Item(i)
                if getattr(sh, "HasTextFrame", 0) and sh.TextFrame.HasText:
                    t = self._target(sh.TextFrame.TextRange, f"ppt:shape{i}")
                    if t:
                        targets.append(t)
        else:
            return None
        if not targets:
            raise SelectionUncertain("selected PowerPoint text could not be mapped safely")
        pres = win.Presentation
        key = f"ppt:{pres.FullName}:{[t.label for t in targets]}:{[t.text()[:60] for t in targets]}"
        return SelectionContext("PowerPoint", key, list(targets))

    @staticmethod
    def _target(tr: Any, label: str) -> Optional[PptTarget]:
        text = tr.Text
        if utf16_len(text) != int(tr.Length):
            log.warning("%s skipped: Text length != TextRange.Length", label)
            return None
        return PptTarget(tr, text, label)

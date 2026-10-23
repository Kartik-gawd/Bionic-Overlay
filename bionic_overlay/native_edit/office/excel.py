"""Excel provider.

Two DIFFERENT things exist in Excel and must not be confused:
  * cell-level formatting  (Range.Font)            -> NOT touched by this tool;
  * text-within-cell rich formatting (Range.Characters(start, len).Font) -> what we write.

Hard limits of the Excel object model (documented, not worked around):
  * While a cell is in EDIT MODE Excel rejects COM calls. We detect that and do
    nothing. Words selected inside the cell editor therefore cannot be formatted;
    the user must select the CELLS (Esc/Enter first) and every text constant in
    them is emphasized.
  * Formula cells are skipped (their display text cannot hold rich formatting).
  * Programmatic edits clear Excel's Undo stack. The tool's own snapshot restore
    (press the hotkey again) is the only way back.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator, Optional

from ..spans import utf16_len
from ..config import OfficeConfig
from ..selection import (ForegroundInfo, SelectionContext, SelectionProvider,
                         SelectionUncertain, TextTarget)
from .com_common import assert_same_process, com_call, get_running_app, norm_bold

log = logging.getLogger(__name__)


class ExcelCellTarget(TextTarget):
    def __init__(self, cell: Any, text: str, address: str) -> None:
        self._cell, self._text, self.label = cell, text, f"excel:{address}"

    def _chars(self, a: int, b: int) -> Any:
        return self._cell.Characters(a + 1, b - a)

    def text(self) -> str:
        return self._text

    def get_bold(self, start: int, end: int) -> Optional[bool]:
        return norm_bold(self._chars(start, end).Font.Bold)   # None when mixed

    def set_bold(self, start: int, end: int, value: bool) -> None:
        self._chars(start, end).Font.Bold = bool(value)

    def verify(self, start: int, end: int, expected: str) -> bool:
        return self._chars(start, end).Text == expected

    def editable_problem(self) -> Optional[str]:
        if utf16_len(self._text) > 32767:
            return "cell text too long"
        return None


class ExcelProvider(SelectionProvider):
    name = "Excel"
    exe_names = frozenset({"excel.exe"})

    def __init__(self, cfg: OfficeConfig) -> None:
        self.cfg = cfg

    def acquire(self, info: ForegroundInfo) -> Optional[SelectionContext]:
        app = get_running_app("Excel.Application")
        if app is None:
            raise SelectionUncertain("Excel is foreground but no COM instance is registered")
        try:
            assert_same_process(info, com_call(lambda: app.Hwnd), "Excel")
            if com_call(lambda: app.Workbooks.Count) == 0:
                return None
            sel = com_call(lambda: app.Selection)
            try:
                address = com_call(lambda: sel.Address)
            except SelectionUncertain:
                raise
            except Exception:
                return None                      # chart / shape / nothing selected
            ws = com_call(lambda: app.ActiveSheet)
            if com_call(lambda: ws.ProtectContents):
                raise SelectionUncertain("worksheet is protected")
            targets = self._collect(app, ws, sel)
        except SelectionUncertain:
            raise
        except Exception as exc:
            raise SelectionUncertain(f"Excel selection could not be read safely ({exc})") from exc
        if not targets:
            return None
        wb = app.ActiveWorkbook
        return SelectionContext("Excel", f"excel:{wb.FullName}:{ws.Name}:{address}", targets,
                                lambda: self._scope(app),
                                notes=["Excel's Undo stack is cleared by programmatic edits; "
                                       "press the hotkey again to restore."])

    def _collect(self, app: Any, ws: Any, sel: Any) -> list[TextTarget]:
        out: list[TextTarget] = []
        used = ws.UsedRange
        areas = sel.Areas
        for ai in range(1, int(areas.Count) + 1):
            inter = app.Intersect(areas.Item(ai), used)
            if inter is None:
                continue
            values = inter.Value2
            rows, cols = int(inter.Rows.Count), int(inter.Columns.Count)
            for r in range(1, rows + 1):
                for c in range(1, cols + 1):
                    v = values if rows == cols == 1 else values[r - 1][c - 1]
                    if not isinstance(v, str) or not v.strip():
                        continue
                    cell = inter.Cells(r, c)
                    if cell.HasFormula:
                        continue
                    out.append(ExcelCellTarget(cell, v, str(cell.Address(False, False))))
                    if len(out) > self.cfg.excel_max_cells:
                        raise SelectionUncertain(f"more than {self.cfg.excel_max_cells} text cells selected")
        return out

    @staticmethod
    @contextlib.contextmanager
    def _scope(app: Any) -> Iterator[None]:
        prev = None
        try:
            prev = app.ScreenUpdating
            app.ScreenUpdating = False
        except Exception:
            pass
        try:
            yield
        finally:
            if prev is not None:
                try:
                    app.ScreenUpdating = prev
                except Exception:
                    pass

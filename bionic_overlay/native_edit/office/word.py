"""Microsoft Word provider: formats the live Selection through the Word object model.

Mapping rules (fail-safe):
  * Work paragraph-by-paragraph (Range.Paragraphs) so the Range stays inside the
    SAME story (body, header, footnote, text box...). Document.Range(s, e) would
    silently address the main story only.
  * A paragraph is only touched if len(Range.Text) == End - Start (UTF-16 units),
    i.e. there are no hidden fields/hidden text that would shift offsets.
    Mismatching paragraphs are skipped and logged, never guessed.
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

WD_SELECTION_NORMAL, WD_SELECTION_ROW = 2, 5
WD_NO_PROTECTION = -1


def normalize_word_text(raw: str, length: int) -> Optional[str]:
    """Return text whose UTF-16 length equals the Range length, or None if offsets can't be trusted."""
    if utf16_len(raw) == length:
        return raw
    if raw.endswith("\r\x07") and utf16_len(raw) - 1 == length:  # end-of-cell/row marker = 1 position
        return raw[:-2] + "\r"
    return None


class WordTarget(TextTarget):
    def __init__(self, base: Any, start: int, end: int, text: str, label: str) -> None:
        self._base, self._start, self._end, self._text, self.label = base, start, end, text, label

    def _sub(self, a: int, b: int) -> Any:
        r = self._base.Duplicate
        r.SetRange(self._start + a, self._start + b)
        return r

    def text(self) -> str:
        return self._text

    def get_bold(self, start: int, end: int) -> Optional[bool]:
        return norm_bold(self._sub(start, end).Font.Bold)

    def set_bold(self, start: int, end: int, value: bool) -> None:
        self._sub(start, end).Font.Bold = -1 if value else 0

    def verify(self, start: int, end: int, expected: str) -> bool:
        return normalize_word_text(self._sub(start, end).Text, end - start) == expected


class WordProvider(SelectionProvider):
    name = "Word"
    exe_names = frozenset({"winword.exe"})

    def __init__(self, cfg: OfficeConfig) -> None:
        self.cfg = cfg

    def acquire(self, info: ForegroundInfo) -> Optional[SelectionContext]:
        app = get_running_app("Word.Application")
        if app is None:
            raise SelectionUncertain("Word is the foreground app but no COM instance is registered")
        if com_call(lambda: app.Documents.Count) == 0:
            return None
        try:
            hwnd = com_call(lambda: app.ActiveWindow.Hwnd)
        except SelectionUncertain:
            raise
        except Exception as exc:
            raise SelectionUncertain(f"cannot verify Word window ({exc})") from exc
        assert_same_process(info, hwnd, "Word")

        sel = com_call(lambda: app.Selection)
        stype = com_call(lambda: sel.Type)
        if stype in (0, 1):                      # none / insertion point
            return None
        if stype not in (WD_SELECTION_NORMAL, WD_SELECTION_ROW):
            raise SelectionUncertain(f"unsupported Word selection type {stype}")
        doc = com_call(lambda: app.ActiveDocument)
        if com_call(lambda: doc.ProtectionType) != WD_NO_PROTECTION:
            raise SelectionUncertain("document is protected")
        rng = com_call(lambda: sel.Range)
        s, e = int(rng.Start), int(rng.End)
        if e <= s:
            return None
        if e - s > self.cfg.max_selection_chars * 2:
            raise SelectionUncertain(f"selection too large ({e - s} positions)")

        targets: list[TextTarget] = []
        paras = rng.Paragraphs
        for i in range(1, int(paras.Count) + 1):
            pr = paras.Item(i).Range
            ps, pe = max(int(pr.Start), s), min(int(pr.End), e)
            if pe <= ps:
                continue
            sub = pr.Duplicate
            sub.SetRange(ps, pe)
            text = normalize_word_text(sub.Text, pe - ps)
            if text is None:
                log.warning("Word paragraph %d [%d-%d] skipped: text length != range length "
                            "(hidden text, fields or similar)", i, ps, pe)
                continue
            targets.append(WordTarget(sub, ps, pe, text, f"word:para{i}"))
        if not targets:
            raise SelectionUncertain("no paragraph in the selection could be mapped safely")

        key = f"word:{doc.FullName}:{rng.StoryType}:{s}-{e}"
        return SelectionContext("Word", key, targets, lambda: self._scope(app))

    @staticmethod
    @contextlib.contextmanager
    def _scope(app: Any) -> Iterator[None]:
        """One Ctrl+Z undoes the whole Bionic operation; screen updates off for speed."""
        prev = None
        started = False
        try:
            prev = app.ScreenUpdating
            app.ScreenUpdating = False
        except Exception:
            pass
        try:
            app.UndoRecord.StartCustomRecord("Bionic Reading")
            started = True
        except Exception:
            log.debug("UndoRecord unavailable; Ctrl+Z will undo in several steps")
        try:
            yield
        finally:
            if started:
                try:
                    app.UndoRecord.EndCustomRecord()
                except Exception:
                    log.exception("EndCustomRecord failed")
            if prev is not None:
                try:
                    app.ScreenUpdating = prev
                except Exception:
                    pass

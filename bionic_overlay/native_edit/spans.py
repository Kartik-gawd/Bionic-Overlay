"""Unicode-aware Bionic Reading algorithm.

Pure Python, OS-independent. Input: text. Output: character spans (start, end),
in Python code-point offsets, that should be emphasized. The engine NEVER
changes text, which is what makes every adapter's "text preserved exactly"
guarantee checkable.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterator

from ..bionic_engine import bold_count
from .config import BionicConfig

Span = tuple[int, int]

_URL_RE = re.compile(r"(?:(?:https?|ftp)://|www\.)[^\s<>\"']+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+%\-]+@[\w\-]+(?:\.[\w\-]+)+")
_URL_TRAILING = ".,;:!?)]}\"'\u00bb\u201d\u2019"
_APOSTROPHES = frozenset("'\u2019\u02bc")
_JOINERS = frozenset("\u200c\u200d\u00ad")  # ZWNJ, ZWJ, soft hyphen

# Scripts written without spaces between words: "first 45% of the word" is
# meaningless there (a whole sentence would count as one word).
_UNSPACED = (
    (0x0E00, 0x0EFF), (0x1000, 0x109F), (0x1780, 0x17FF), (0x3040, 0x30FF),
    (0x31F0, 0x31FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
)


def _is_unspaced(ch: str) -> bool:
    cp = ord(ch)
    if cp < 0x0E00:
        return False
    return any(lo <= cp <= hi for lo, hi in _UNSPACED)


def _is_attached(ch: str) -> bool:
    """Combining marks / joiners: never counted, but stay glued to their base."""
    return ch in _JOINERS or unicodedata.category(ch)[0] == "M"


def _is_numeric(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "N"


class BionicEngine:
    def __init__(self, config: BionicConfig | None = None) -> None:
        self.config = config or BionicConfig()
        self.config.validate()

    # API
    def spans(self, text: str) -> list[Span]:
        """Return sorted, non-overlapping (start, end) spans to embolden."""
        if not text or text.isspace():
            return []
        cfg = self.config
        mask = self._ignored_mask(text)
        out: list[Span] = []
        for start, end, meaningful in self._iter_words(text):
            if mask is not None and mask[start]:
                continue
            n = len(meaningful)
            if cfg.skip_unspaced_scripts and any(_is_unspaced(text[i]) for i in meaningful):
                continue
            if all(_is_numeric(text[i]) for i in meaningful) and not cfg.format_numbers:
                continue
            short = n < cfg.minimum_word_length
            if short and not cfg.format_short_words:
                continue
            k = self._bold_count(n)
            stop = meaningful[k - 1] + 1
            while stop < end and _is_attached(text[stop]):
                stop += 1
            out.append((start, stop))
        return out

    def preview(self, text: str, marker: str = "**") -> str:
        """Human-readable rendering, e.g. 'Bi**onic**'. For debugging and tests."""
        parts, pos = [], 0
        for a, b in self.spans(text):
            parts += [text[pos:a], marker, text[a:b], marker]
            pos = b
        parts.append(text[pos:])
        return "".join(parts)

    # internals
    def _bold_count(self, n: int) -> int:
        return bold_count(n, self.config.bold_ratio)   # shared with the overlay engine

    def _ignored_mask(self, text: str) -> bytearray | None:
        cfg = self.config
        if not (cfg.ignore_urls or cfg.ignore_emails):
            return None
        ranges: list[Span] = []
        if cfg.ignore_urls:
            for m in _URL_RE.finditer(text):
                end = m.end()
                while end > m.start() and text[end - 1] in _URL_TRAILING:
                    end -= 1
                ranges.append((m.start(), end))
        if cfg.ignore_emails:
            ranges.extend(m.span() for m in _EMAIL_RE.finditer(text))
        if not ranges:
            return None
        mask = bytearray(len(text))
        for a, b in ranges:
            mask[a:b] = b"\x01" * (b - a)
        return mask

    @staticmethod
    def _iter_words(text: str) -> Iterator[tuple[int, int, list[int]]]:
        """Yield (start, end, indices_of_meaningful_chars).

        A word is a run of letters/digits; combining marks and joiners attach to
        it; an apostrophe between two alphanumerics is kept (contractions).
        Hyphens, underscores, slashes, dots split words, so 'well-known' gives
        two words and 'don't' stays one.
        """
        n, i = len(text), 0
        while i < n:
            if not text[i].isalnum():
                i += 1
                continue
            start, j, meaningful = i, i, []
            while j < n:
                c = text[j]
                if c.isalnum():
                    meaningful.append(j)
                    j += 1
                elif _is_attached(c):
                    j += 1
                elif c in _APOSTROPHES and j + 1 < n and text[j + 1].isalnum():
                    j += 1
                else:
                    break
            yield start, j, meaningful
            i = j


# UTF-16 helpers
def utf16_len(text: str) -> int:
    """Length in UTF-16 code units: the unit Word/PowerPoint/Excel COM positions use."""
    return len(text) + sum(1 for c in text if ord(c) > 0xFFFF)


def to_utf16_spans(text: str, spans: list[Span]) -> list[Span]:
    """Convert code-point spans into UTF-16 code-unit spans (identity for BMP text)."""
    if not spans or max(text) <= "\uffff":
        return list(spans)
    offsets, acc = [0], 0
    for ch in text:
        acc += 2 if ord(ch) > 0xFFFF else 1
        offsets.append(acc)
    return [(offsets[a], offsets[b]) for a, b in spans]

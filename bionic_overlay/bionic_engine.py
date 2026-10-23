from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

from .geometry import Rect
from .ocr.base import OcrWord


@dataclass(frozen=True)
class BionicConfig:
    bold_ratio: float = 0.45          # fraction of a word's letters that are bold
    min_word_length: int = 1          # words shorter than this are left regular
    short_word_max: int = 3           # words up to this length use short_word_mode
    short_word_mode: str = "ratio"    # "ratio" (shared rule) | "first" | "none" | "all"
    bold_numbers: bool = False
    bold_urls: bool = False
    bold_emails: bool = False
    bold_symbols: bool = False        # tokens that contain no letters/digits at all
    bold_cjk: bool = False            # CJK has no word boundaries; off by default


@dataclass(frozen=True)
class Segment:
    text: str
    bold: bool


@dataclass
class FormattedWord:
    word: OcrWord
    segments: list[Segment]


@dataclass
class FormattedLine:
    words: list[FormattedWord]
    box: Rect
    baseline: float
    cap_height: float
    paragraph: int = 0


@dataclass
class FormattedWords:
    lines: list[FormattedLine]
    width: int = 0
    height: int = 0
    image: Optional[object] = None       # BGR frame the layout was built from (for pixel emboldening)

    @property
    def words(self) -> list[FormattedWord]:
        return [w for ln in self.lines for w in ln.words]


_URL_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.\-]*://|www\.)\S+$"
    r"|^[\w\-]+(?:\.[\w\-]+)*\.(?:com|org|net|io|edu|gov|co|uk|de|fr|in|ai|dev|app)(?:/\S*)?$",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"^[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+$")
_NUMBER_RE = re.compile(r"^\d[\d.,:/\-]*$")
_JOINERS_RE = re.compile(r"([\-_/]+)")


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FFFF)


def _is_rtl(ch: str) -> bool:
    return unicodedata.bidirectional(ch) in ("R", "AL")


def bold_count(n: int, ratio: float) -> int:
    """THE shared rule (overlay and native Office editing both use it): round half up, never zero,
    never the whole word (except 1-letter words)."""
    if n <= 1:
        return max(0, n)
    return max(1, min(n - 1, int(n * ratio + 0.5)))


def split_token(token: str) -> tuple[str, str, str]:
    """Split into (leading punctuation, core, trailing punctuation); the core starts and
    ends with an alphanumeric character."""
    n = len(token)
    i = 0
    while i < n and not token[i].isalnum():
        i += 1
    j = n
    while j > i and not token[j - 1].isalnum():
        j -= 1
    return token[:i], token[i:j], token[j:]


def _merge(segs: Iterable[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for s in segs:
        if not s.text:
            continue
        if out and out[-1].bold == s.bold:
            out[-1] = Segment(out[-1].text + s.text, s.bold)
        else:
            out.append(s)
    return out


class BionicEngine:
    def __init__(self, config: Optional[BionicConfig] = None) -> None:
        self.config = config or BionicConfig()

    def configure(self, config: BionicConfig) -> None:
        self.config = config

    # text level
    def format_text(self, text: str) -> list[Segment]:
        """Format arbitrary text (whitespace preserved as regular segments)."""
        segs: list[Segment] = []
        for part in re.split(r"(\s+)", text):
            if not part:
                continue
            segs.extend([Segment(part, False)] if part.isspace() else self.format_token(part))
        return _merge(segs)

    def format_token(self, token: str) -> list[Segment]:
        """Format one whitespace-free token such as '"Hello,"' or 'well-known.'"""
        if not token:
            return []
        lead, core, trail = split_token(token)
        if not core:  # symbols only: "—", "&", "..."
            if self.config.bold_symbols and token.strip():
                k = self._bold_len(len(token))
                return _merge([Segment(token[:k], True), Segment(token[k:], False)])
            return [Segment(token, False)]
        return _merge([Segment(lead, False), *self._format_core(core), Segment(trail, False)])

    # internals
    def _format_core(self, core: str) -> list[Segment]:
        c = self.config
        if _EMAIL_RE.match(core):
            if not c.bold_emails:
                return [Segment(core, False)]
        elif _URL_RE.match(core):
            if not c.bold_urls:
                return [Segment(core, False)]
        elif _NUMBER_RE.match(core):
            if not c.bold_numbers:
                return [Segment(core, False)]
        if any(_is_rtl(ch) for ch in core):          # logical-prefix bolding is wrong for RTL
            return [Segment(core, False)]
        if not c.bold_cjk and any(_is_cjk(ch) for ch in core):
            return [Segment(core, False)]

        segs: list[Segment] = []
        for part in _JOINERS_RE.split(core):
            if not part:
                continue
            if _JOINERS_RE.fullmatch(part):
                segs.append(Segment(part, False))
            else:
                segs.extend(self._bold_word(part))
        return segs

    def _bold_word(self, word: str) -> list[Segment]:
        k = self._bold_len(len(word))
        while 0 < k < len(word) and unicodedata.category(word[k]) in ("Mn", "Mc", "Me"):
            k += 1  # never split a base character from its combining marks
        return [Segment(word[:k], True), Segment(word[k:], False)]

    def _bold_len(self, n: int) -> int:
        c = self.config
        if c.bold_ratio <= 0 or n < c.min_word_length:
            return 0
        if n <= c.short_word_max:
            if c.short_word_mode == "none":
                return 0
            if c.short_word_mode == "all":
                return n
            if c.short_word_mode == "first":
                return 1
        return bold_count(n, c.bold_ratio)

    # layout level
    def format_words(self, words: Iterable[OcrWord]) -> list[FormattedWord]:
        return [FormattedWord(w, self.format_token(w.text)) for w in words]

    def format(self, layout) -> FormattedWords:  # layout: recognition.Layout
        lines = [
            FormattedLine(self.format_words(ln.words), ln.box, ln.baseline, ln.cap_height, ln.paragraph)
            for ln in layout.lines
        ]
        return FormattedWords(lines, layout.width, layout.height, getattr(layout, "image", None))

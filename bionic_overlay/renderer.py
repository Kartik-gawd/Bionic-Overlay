"""Turns FormattedWords into a transparent QImage that matches the OCR layout.

Modes (RenderStyle.background_mode):
  embolden    (default) thicken the CAPTURED pixels of each bold prefix and paint them in the sampled ink
              colour: the original font is reused exactly and no background patch is needed.  Falls back
              to `mask` for text whose x-height is below `embolden_min_xheight`.
  mask        patch exactly the prefix box with the sampled background, draw the bold prefix as glyphs.
  contrast    glyph prefix with a halo (no / light patch).
  transparent glyph prefix only.
  replace     hide the original word and redraw the whole word as glyphs (legacy behaviour).

Glyph drawing never stretches a word more than 0.95-1.05x; the remainder is absorbed as letter-spacing.
Words flagged by the layout stage (`word.skip`: already bold, bold heading, ...) are never touched.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen

from .bionic_engine import FormattedLine, FormattedWord, FormattedWords
from .ink import InkInfo, crop_around, delta_e, embolden_alpha, snap_to_gap, word_ink

REF = 100.0
CAP_RATIO = 0.73    # cap/ascender height as a fraction of the font size
XH_OVER_CAP = 0.72  # x-height as a fraction of cap height


@dataclass
class RenderStyle:
    font_family: str = "Segoe UI"
    font_scale: float = 1.0
    offset_x: int = 0
    offset_y: int = 0
    opacity: float = 1.0
    line_spacing: float = 1.0
    word_spacing: float = 1.0
    fit_width: bool = True
    text_align: str = "left"
    background_mode: str = "embolden"   # embolden | mask | contrast | transparent | replace
    mask_opacity: float = 0.85
    text_color: str = "auto"            # colour of the bold letters only: "auto" or "#rrggbb"
    debug_boxes: bool = False
    embolden_strength: float = 1.0      # multiplier on the thickening (about 7% of x-height at 1.0)
    embolden_min_xheight: float = 10.0  # below this, 1px dilation looks chunky -> glyph fallback


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


@dataclass
class _Crop:
    info: InkInfo
    x0: int
    y0: int


class OverlayRenderer:
    def __init__(self, style: Optional[RenderStyle] = None) -> None:
        self.style = style or RenderStyle()
        self._bg_memory: list[tuple[float, float, tuple]] = []

    def set_style(self, style: RenderStyle) -> None:
        self.style = style

    def reset(self) -> None:
        """Forget colour memory (call when the capture region changes)."""
        self._bg_memory = []

    # ------------------------------------------------------------------ public
    def render(self, formatted: FormattedWords, size: QSize) -> QImage:
        out = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(Qt.GlobalColor.transparent)
        if not formatted.lines:
            return out
        st = self.style
        p = QPainter(out)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
                         | QPainter.RenderHint.SmoothPixmapTransform)
        reg, bold = self._fonts()
        self._mr, self._mb = QFontMetricsF(reg), QFontMetricsF(bold)
        self._fonts_pair = (reg, bold)
        img = formatted.image
        mode = st.background_mode

        b0 = min(ln.baseline for ln in formatted.lines)
        glyph_jobs = []      # (line, px, base, word, ranges)
        new_memory: list[tuple[float, float, tuple]] = []
        for ln in formatted.lines:
            px = self._line_size(ln)
            base = b0 + (ln.baseline - b0) * st.line_spacing + st.offset_y
            xh = XH_OVER_CAP * ln.cap_height
            pixels_ok = mode == "embolden" and img is not None and xh >= st.embolden_min_xheight
            for fw in ln.words:
                w = fw.word
                if w.skip:
                    continue
                crop = self._crop(img, w)
                ranges = (self._bold_ranges(fw, crop) if mode != "replace"
                          else [(w.box.x, w.box.right)])
                if not ranges:
                    continue
                if pixels_ok and crop is not None:
                    if self._paint_embolden(p, crop, ranges, xh):
                        continue
                    if not w.bg_ok:
                        continue                      # low contrast / messy background: leave original
                if w.bg is not None:
                    w.bg = self._stable_bg(w, new_memory)
                glyph_jobs.append((ln, px, base, fw, ranges))
        self._bg_memory = new_memory

        gmode = "mask" if mode == "embolden" else mode
        if gmode != "transparent":                    # pass 1: backgrounds
            for ln, _, _, fw, ranges in glyph_jobs:
                self._draw_patch(p, gmode, ln, fw, ranges)
        for ln, px, base, fw, ranges in glyph_jobs:   # pass 2: glyph text
            self._draw_glyphs(p, gmode, fw, ranges, px, base, ln.words[0].word.box.x)
        if st.debug_boxes:
            self._draw_debug(p, formatted)
        p.end()
        return out

    # ------------------------------------------------------------------ fonts / metrics
    def _fonts(self):
        reg = QFont(self.style.font_family)
        reg.setPixelSize(int(REF))
        reg.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        bold = QFont(reg)
        bold.setWeight(QFont.Weight.Bold)
        return reg, bold

    def _natural(self, fw: FormattedWord) -> float:
        return sum((self._mb if s.bold else self._mr).horizontalAdvance(s.text) for s in fw.segments)

    def _line_size(self, ln: FormattedLine) -> float:
        """One font size per line: least-squares fit of rendered width to OCR width over the words."""
        num = den = 0.0
        for fw in ln.words:
            nat = self._natural(fw)
            if nat > 0 and len(fw.word.text) >= 3:
                num += fw.word.box.w * nat
                den += nat * nat
        prior = ln.cap_height / CAP_RATIO if ln.cap_height > 0 else None
        size = REF * num / den if den > 0 else (prior or ln.box.h * 0.8)
        if prior:
            size = _clamp(size, prior * 0.75, prior * 1.3)
        return max(4.0, size * self.style.font_scale)

    # ------------------------------------------------------------------ pixel analysis helpers
    def _crop(self, img: Optional[np.ndarray], w) -> Optional[_Crop]:
        if img is None:
            return None
        crop, x0, y0 = crop_around(img, w.box)
        if crop is None:
            return None
        return _Crop(word_ink(crop), x0, y0)

    def _snap(self, box, crop: Optional[_Crop], f: float, tol: float) -> float:
        target = box.x + f * box.w
        if crop is None:
            return target
        prof = crop.info.alpha.max(axis=0)
        return crop.x0 + snap_to_gap(prof, target - crop.x0, tol)

    def _bold_ranges(self, fw: FormattedWord, crop: Optional[_Crop]) -> list[tuple[float, float]]:
        """Pixel x-ranges of the bold segments. Boundaries start from font-metric proportions of the
        OCR box and are snapped to the nearest inter-letter gap in the captured pixels."""
        box = fw.word.box
        advs = [(self._mb if s.bold else self._mr).horizontalAdvance(s.text) for s in fw.segments]
        total = sum(advs)
        if total <= 0:
            return []
        tol = 0.6 * box.w / max(1, len(fw.word.text))
        out, cum = [], 0.0
        for s, a in zip(fw.segments, advs):
            if s.bold and a > 0:
                f0, f1 = cum / total, (cum + a) / total
                x0 = box.x if f0 <= 1e-6 else self._snap(box, crop, f0, tol)
                x1 = box.right if f1 >= 1 - 1e-6 else self._snap(box, crop, f1, tol)
                if x1 - x0 >= 1:
                    out.append((x0, x1))
            cum += a
        return out

    def _stable_bg(self, w, memory: list) -> tuple:
        """Hysteresis: reuse last render's colour for the same word position unless it really changed."""
        cx, cy, new = w.box.cx, w.box.cy, w.bg
        for pcx, pcy, old in self._bg_memory:
            if abs(pcx - cx) < w.box.w * 0.6 and abs(pcy - cy) < w.box.h * 0.6:
                if delta_e(old, new) < 6.0:
                    new = old
                break
        memory.append((cx, cy, new))
        return new

    # ------------------------------------------------------------------ embolden (pixels)
    def _paint_embolden(self, p: QPainter, crop: _Crop, ranges, xh: float) -> bool:
        info = crop.info
        if info.dmax < 40.0 or info.uniformity < 0.5:
            return False
        st = self.style
        px = 0.07 * xh * st.embolden_strength
        custom = self._custom_color()                 # user-chosen colour for the bold letters, else sampled ink
        fg = (custom.blue(), custom.green(), custom.red()) if custom else info.fg
        pad = 3
        for rx0, rx1 in ranges:
            a, b = int(np.floor(rx0 - crop.x0)), int(np.ceil(rx1 - crop.x0))
            a, b = max(0, a), min(info.alpha.shape[1], b)
            if b - a < 1:
                continue
            sub = np.pad(info.alpha[:, a:b], ((pad, pad), (pad, pad)))
            emb = embolden_alpha(sub, px)
            h, w = emb.shape
            rgba = np.empty((h, w, 4), np.uint8)
            rgba[..., 0], rgba[..., 1], rgba[..., 2] = int(fg[2]), int(fg[1]), int(fg[0])
            rgba[..., 3] = (np.clip(emb * st.opacity, 0, 1) * 255).astype(np.uint8)
            q = QImage(rgba.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
            p.drawImage(crop.x0 + a - pad, crop.y0 - pad, q)
        return True

    # ------------------------------------------------------------------ glyph drawing
    def _bg_color(self, w) -> QColor:
        return QColor(*(w.bg or (255, 255, 255)))

    def _custom_color(self) -> Optional[QColor]:
        if self.style.text_color == "auto":
            return None
        c = QColor(self.style.text_color)
        return c if c.isValid() else None

    def _fg_color(self, w) -> QColor:
        return self._custom_color() or QColor(*(w.fg or (0, 0, 0)))

    def _draw_patch(self, p: QPainter, gmode: str, ln: FormattedLine, fw: FormattedWord, ranges) -> None:
        w, st = fw.word, self.style
        if not w.bg_ok or w.bg is None:
            return                                   # gradients / images / selections: no patch (halo instead)
        alpha = {"replace": 1.0, "contrast": min(0.6, st.mask_opacity)}.get(gmode, st.mask_opacity)
        c = self._bg_color(w)
        c.setAlphaF(alpha)
        p.setOpacity(1.0)
        pad_y = max(2.0, 0.25 * ln.box.h) if gmode == "replace" else 1.0
        for x0, x1 in ranges:                        # exactly the prefix box, no horizontal feathering
            pad_x = max(2.0, 0.25 * ln.box.h) if gmode == "replace" else 0.0
            p.fillRect(QRectF(x0 - pad_x, ln.box.y - pad_y, (x1 - x0) + 2 * pad_x, ln.box.h + 2 * pad_y), c)

    def _draw_glyphs(self, p: QPainter, gmode: str, fw: FormattedWord, ranges, px: float, base: float,
                     line_x0: float = 0.0) -> None:
        st, w = self.style, fw.word
        k = px / REF
        color = self._fg_color(w)
        halo_on = gmode == "contrast" or not w.bg_ok
        halo = QColor(255, 255, 255) if color.lightness() < 128 else QColor(0, 0, 0)
        reg, bold = self._fonts_pair
        dy = 0.0
        if gmode == "replace":                       # whole word: draw every segment from the word start
            ranges = [(w.box.x, w.box.right)]
        for r_i, (x0, x1) in enumerate(ranges):
            if gmode == "replace":
                segs = fw.segments
            else:
                bolds = [s for s in fw.segments if s.bold]
                if r_i >= len(bolds):
                    continue
                segs = [bolds[r_i]]
            dx = st.offset_x + (x0 - line_x0) * (st.word_spacing - 1.0)
            x0 = x0 + dx
            text_len = sum(len(s.text) for s in segs)
            nat = sum((self._mb if s.bold else self._mr).horizontalAdvance(s.text) for s in segs) * k
            if nat <= 0:
                continue
            target = x1 - (x0 - dx)
            sx = _clamp(target / nat, 0.95, 1.05) if st.fit_width else 1.0
            extra = (target - nat * sx) if st.fit_width else 0.0
            spacing = _clamp(extra / (k * sx) / (text_len - 1), -25.0, 25.0) if text_len > 1 else 0.0
            p.save()
            p.setOpacity(st.opacity)
            p.translate(x0, base + dy)
            p.scale(k * sx, k)
            cx = 0.0
            for s in segs:
                font, fm = (bold, self._mb) if s.bold else (reg, self._mr)
                f2 = QFont(font)
                f2.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
                if halo_on:
                    path = QPainterPath()
                    path.addText(QPointF(cx, 0), f2, s.text)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.setPen(QPen(halo, 2.4 / k, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                  Qt.PenJoinStyle.RoundJoin))
                    p.drawPath(path)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(color)
                    p.drawPath(path)
                else:
                    p.setFont(f2)
                    p.setPen(color)
                    p.drawText(QPointF(cx, 0), s.text)
                cx += fm.horizontalAdvance(s.text) + spacing * len(s.text)
            p.restore()

    # ------------------------------------------------------------------ debug
    def _draw_debug(self, p: QPainter, formatted: FormattedWords) -> None:
        p.setOpacity(1.0)
        small = QFont("Consolas")
        small.setPixelSize(9)
        p.setFont(small)
        for ln in formatted.lines:
            p.setPen(QPen(QColor(0, 160, 255, 220), 1))
            p.drawLine(QPointF(ln.box.x, ln.baseline), QPointF(ln.box.right, ln.baseline))
            for fw in ln.words:
                w = fw.word
                b = w.box
                p.setPen(QPen(QColor(255, 150, 0, 230) if w.skip else QColor(255, 40, 40, 200), 1))
                p.drawRect(QRectF(b.x, b.y, b.w, b.h))
                label = ("SKIP:" + w.skip if w.skip else "r") + (f" {w.stroke:.2f}" if w.stroke else "")
                p.setPen(QColor(0, 200, 255))
                p.drawText(QPointF(b.x, b.y - 2), label)
                for i, col in enumerate((w.bg, w.fg)):       # swatches: sampled background, sampled ink
                    if col:
                        p.fillRect(QRectF(b.right - 14 + i * 7, b.bottom, 6, 6), QColor(*col))
                        p.setPen(QPen(QColor(128, 128, 128), 1))
                        p.drawRect(QRectF(b.right - 14 + i * 7, b.bottom, 6, 6))
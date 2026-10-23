"""Pixel analysis on the captured frame (numpy + Pillow).

Used for: robust background/ink separation, stroke-width ("already bold?") detection, snapping the
bold-prefix boundary to the inter-letter gap, and emboldening captured ink instead of redrawing text.
All colours are returned as RGB tuples unless a function says otherwise; images are BGR uint8."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


# colour helpers
def rgb_to_lab(rgb) -> np.ndarray:
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = (m @ c) / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])


def delta_e(a, b) -> float:
    return float(np.linalg.norm(rgb_to_lab(a) - rgb_to_lab(b)))


def _rgb(bgr) -> tuple[int, int, int]:
    return int(round(bgr[2])), int(round(bgr[1])), int(round(bgr[0]))


# ink / background
@dataclass
class InkInfo:
    bg: np.ndarray          # BGR float
    fg: np.ndarray          # BGR float
    alpha: np.ndarray       # 0..1 ink coverage, same HxW as the crop
    ink: np.ndarray         # bool mask
    dmax: float             # contrast: max colour distance between ink and background
    uniformity: float       # share of ring pixels close to the background colour (0..1)


def crop_around(img: np.ndarray, box, margin: int = 4):
    h, w = img.shape[:2]
    x0, y0 = max(0, int(box.x) - margin), max(0, int(box.y) - margin)
    x1, y1 = min(w, int(np.ceil(box.right)) + margin), min(h, int(np.ceil(box.bottom)) + margin)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None, 0, 0
    return img[y0:y1, x0:x1, :3], x0, y0


def ring_background(crop: np.ndarray, ring: int = 3) -> tuple[np.ndarray, float]:
    """Background = the MODE (quantised) of the crop's border ring, never the mean: the ring may
    touch neighbouring glyphs, and means turn those into random tinted patches."""
    h, w = crop.shape[:2]
    ring = max(1, min(ring, h // 3, w // 3))
    mask = np.ones((h, w), bool)
    mask[ring:h - ring, ring:w - ring] = False
    px = crop[mask]
    q = px >> 4
    key = (q[:, 0].astype(np.int32) << 8) | (q[:, 1].astype(np.int32) << 4) | q[:, 2]
    top = np.bincount(key, minlength=4096).argmax()
    bg = px[key == top].astype(np.float32).mean(axis=0)
    uniformity = float((np.linalg.norm(px.astype(np.float32) - bg, axis=1) < 24).mean())
    return bg, uniformity


def word_ink(crop: np.ndarray) -> InkInfo:
    bg, uni = ring_background(crop)
    d = np.linalg.norm(crop.astype(np.float32) - bg, axis=2)
    dmax = float(np.percentile(d, 98))
    alpha = np.clip(d / max(dmax, 1.0), 0.0, 1.0)
    ink = d > max(40.0, 0.35 * dmax)
    sel = d >= 0.8 * dmax
    fg = crop[sel].astype(np.float32).mean(axis=0) if dmax >= 1 and sel.any() else bg
    return InkInfo(bg, fg, alpha, ink, dmax, uni)


def stroke_width(alpha: np.ndarray, ink: np.ndarray, limit: float) -> Optional[float]:
    """Median horizontal ink-run width (sub-pixel via coverage), ignoring runs longer than `limit`
    (horizontal bars). Approximates the vertical-stem thickness without distance transforms."""
    h, w = ink.shape
    if not ink.any():
        return None
    m = np.zeros((h, w + 2), np.int8)
    m[:, 1:-1] = ink
    d = np.diff(m, axis=1)
    rs, cs = np.nonzero(d == 1)
    re_, ce = np.nonzero(d == -1)
    if len(rs) == 0 or len(rs) != len(re_):
        return None
    csum = np.concatenate([np.zeros((h, 1), np.float32), np.cumsum(alpha * ink, axis=1)], axis=1)
    widths = csum[re_, ce] - csum[rs, cs]
    widths = widths[widths <= limit]
    return float(np.median(widths)) if widths.size >= 3 else None


def has_underline(ink: np.ndarray, baseline_row: float, min_cover: float = 0.8) -> bool:
    h, w = ink.shape
    for r in range(int(baseline_row) + 1, min(h, int(baseline_row) + 4)):
        if r >= 0 and ink[r].sum() >= min_cover * w * 0.7:   # crop includes margins -> allow 70% of width
            return True
    return False


# prefix boundary
def snap_to_gap(profile: np.ndarray, target: float, tol: float) -> float:
    """Pick the inter-letter gap (interior low-ink column run) nearest `target`; if letters touch
    or no gap is within `tol`, return the raw target."""
    if profile.size == 0 or profile.max() <= 0:
        return target
    gap = profile <= 0.12 * profile.max()
    centers, i, n = [], 0, len(gap)
    while i < n:
        if gap[i]:
            j = i
            while j < n and gap[j]:
                j += 1
            if i > 0 and j < n:
                centers.append((i + j) / 2.0)
            i = j
        else:
            i += 1
    if not centers:
        return target
    best = min(centers, key=lambda c: abs(c - target))
    return best if abs(best - target) <= tol else target


# emboldening
def _maxfilt(x: np.ndarray, k: int, axis: int) -> np.ndarray:
    if k <= 1:
        return x
    r = k // 2
    pad = [(0, 0), (0, 0)]
    pad[axis] = (r, k - 1 - r)
    xp = np.pad(x, pad)
    n = x.shape[axis]
    out = None
    for s in range(k):
        sl = [slice(None), slice(None)]
        sl[axis] = slice(s, s + n)
        v = xp[tuple(sl)]
        out = v if out is None else np.maximum(out, v)
    return out


def embolden_alpha(alpha: np.ndarray, px: float) -> np.ndarray:
    """Thicken strokes by ~px pixels horizontally (~px/2 vertically) with 4x sub-pixel precision."""
    from PIL import Image

    h, w = alpha.shape
    big = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).resize(
        (w * 4, h * 4), Image.Resampling.BICUBIC), dtype=np.float32) / 255.0
    big = _maxfilt(_maxfilt(big, 1 + int(round(px * 4)), 1), 1 + int(round(px * 2)), 0)
    out = Image.fromarray((np.clip(big, 0, 1) * 255).astype(np.uint8)).resize((w, h), Image.Resampling.BOX)
    return np.asarray(out, dtype=np.float32) / 255.0


# layout analysis
@dataclass
class InkConfig:
    skip_bold: bool = True
    bold_rel: float = 1.3          # word is bold if stroke ratio > rel * paragraph median ...
    bold_abs: float = 0.17         # ... or above this absolute stroke/cap-height ratio
    heading_frac: float = 0.6      # >60% bold words -> skip the whole line
    skip_colored: bool = False     # links / syntax highlighting
    skip_underlined: bool = False
    uniform_min: float = 0.6       # ring uniformity below this -> no background patch for the word
    line_de: float = 12.0          # Lab dE above which a word's bg snaps to its line's bg
    min_contrast: float = 40.0


def analyze_layout(layout, cfg: Optional[InkConfig] = None) -> None:
    """Fill word.bg / fg / bg_ok / stroke / underline / skip for every word (in place)."""
    cfg = cfg or InkConfig()
    img = layout.image
    if img is None:
        return
    for ln in layout.lines:
        okbg = []
        for w in ln.words:
            crop, x0, y0 = crop_around(img, w.box)
            if crop is None:
                w.bg_ok = False
                continue
            info = word_ink(crop)
            w.fg = _rgb(info.fg)
            w.bg = _rgb(info.bg)
            w.bg_ok = info.uniformity >= cfg.uniform_min
            if w.bg_ok:
                okbg.append(w.bg)
            letters = sum(c.isalnum() for c in w.text)
            if info.dmax >= cfg.min_contrast and letters >= 3:
                sw = stroke_width(info.alpha, info.ink, 0.45 * ln.cap_height)
                if sw:
                    w.stroke = sw / max(ln.cap_height, 1.0)
                w.underline = has_underline(info.ink, ln.baseline - y0)
        if okbg:                                         # smooth backgrounds across the line
            line_bg = tuple(int(v) for v in np.median(np.array(okbg), axis=0))
            for w in ln.words:
                if w.bg_ok and w.bg and delta_e(w.bg, line_bg) > cfg.line_de:
                    w.bg = line_bg
    _mark_skips(layout, cfg)


def _mark_skips(layout, cfg: InkConfig) -> None:
    all_ratios = [w.stroke for ln in layout.lines for w in ln.words if w.stroke]
    global_med = float(np.median(all_ratios)) if all_ratios else None
    paras: dict[int, list] = {}
    for ln in layout.lines:
        paras.setdefault(ln.paragraph, []).append(ln)
    for lines in paras.values():
        words = [w for ln in lines for w in ln.words]
        ratios = [w.stroke for w in words if w.stroke]
        body = float(np.median(ratios)) if len(ratios) >= 3 else global_med
        body_fg = None
        fgs = [w.fg for w in words if w.fg]
        if fgs:
            body_fg = tuple(int(v) for v in np.median(np.array(fgs), axis=0))
        for ln in lines:
            bold_words = 0
            for w in ln.words:
                if cfg.skip_bold and w.stroke and (w.stroke > cfg.bold_abs or (body and w.stroke > cfg.bold_rel * body)):
                    w.skip = "bold"
                    bold_words += 1
                elif cfg.skip_underlined and w.underline:
                    w.skip = "underline"
                elif cfg.skip_colored and w.fg and body_fg and delta_e(w.fg, body_fg) > 25:
                    w.skip = "colour"
            if cfg.skip_bold and ln.words and bold_words > cfg.heading_frac * len(ln.words):
                for w in ln.words:
                    w.skip = w.skip or "bold-line"

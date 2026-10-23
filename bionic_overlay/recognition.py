"""OCR post-processing: filtering, word -> line -> paragraph layout, colour sampling,
result cache and the background OCR worker."""
from __future__ import annotations

import hashlib
import logging
import math
import statistics
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

import numpy as np

from .geometry import Rect
from .ink import InkConfig, analyze_layout
from .ocr.base import OcrEngine, OCRResult, OcrWord

log = logging.getLogger(__name__)

DESCENDERS = set("gjpqyQ,;()[]{}|/\\")
ASCENDERS = set("bdfhklt") | set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


# data model
@dataclass
class LayoutConfig:
    min_confidence: float = 0.5        # words below this are dropped (original stays visible)
    min_word_height: float = 5.0       # px; smaller boxes are almost always noise
    column_gap_factor: float = 3.0     # split clustered rows at gaps wider than N x line height
    paragraph_gap_factor: float = 0.9  # vertical gap (x line height) that still joins a paragraph
    sample_colors: bool = True
    ink: InkConfig = field(default_factory=InkConfig)


@dataclass
class Line:
    words: list[OcrWord]
    box: Rect
    baseline: float
    cap_height: float
    paragraph: int = 0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Layout:
    lines: list[Line]
    width: int
    height: int
    image: Optional[np.ndarray] = field(default=None, repr=False, compare=False)  # BGR frame it was built from

    @property
    def words(self) -> list[OcrWord]:
        return [w for ln in self.lines for w in ln.words]

    @property
    def paragraph_count(self) -> int:
        return len({ln.paragraph for ln in self.lines})


# filtering
def filter_words(words: list[OcrWord], cfg: LayoutConfig, has_confidence: bool) -> list[OcrWord]:
    """Drop empty, tiny, implausibly-shaped and (if the engine reports it) low-confidence words."""
    out: list[OcrWord] = []
    for w in words:
        t = w.text.strip()
        b = w.box
        if not t or b.w <= 0 or b.h < cfg.min_word_height:
            continue
        if has_confidence and w.confidence < cfg.min_confidence:
            continue
        char_w = b.w / len(t)
        if not (0.08 * b.h <= char_w <= 3.0 * b.h):
            continue
        out.append(w if t == w.text else replace(w, text=t))
    return out


# line geometry
def _alnum_count(s: str) -> int:
    return sum(ch.isalnum() for ch in s)


def estimate_baseline(words: list[OcrWord]) -> tuple[float, float]:
    """Return (baseline_y, cap_height).  Words without descenders sit exactly on the baseline,
    so their bottoms give a robust estimate; otherwise fall back to bottom - 0.2*height."""
    med_h = statistics.median(w.box.h for w in words)
    flat = [w.box.bottom for w in words if _alnum_count(w.text) >= 2 and not (set(w.text) & DESCENDERS)]
    if flat:
        baseline = statistics.median(flat)
    else:
        baseline = statistics.median(w.box.bottom for w in words) - 0.2 * med_h
    tops = [w.box.y for w in words if set(w.text) & ASCENDERS]
    cap = baseline - statistics.median(tops) if tops else 0.72 * med_h
    return baseline, max(cap, 1.0)


def _make_line(words: list[OcrWord]) -> Line:
    box = words[0].box
    for w in words[1:]:
        box = box.union(w.box)
    baseline, cap = estimate_baseline(words)
    return Line(words, box, baseline, cap)


def _cluster_rows(words: list[OcrWord]) -> list[list[OcrWord]]:
    rows: list[tuple[Rect, list[OcrWord]]] = []
    for w in sorted(words, key=lambda w: w.box.cy):
        for i in range(len(rows) - 1, max(-1, len(rows) - 4), -1):
            rbox, rwords = rows[i]
            if rbox.vertical_overlap(w.box) >= 0.5:
                rows[i] = (rbox.union(w.box), rwords + [w])
                break
        else:
            rows.append((w.box, [w]))
    return [r[1] for r in rows]


def _split_columns(row: list[OcrWord], factor: float) -> list[list[OcrWord]]:
    row = sorted(row, key=lambda w: w.box.x)
    line_h = statistics.median(w.box.h for w in row)
    groups, cur = [], [row[0]]
    for prev, w in zip(row, row[1:]):
        if w.box.x - prev.box.right > factor * line_h:
            groups.append(cur)
            cur = []
        cur.append(w)
    groups.append(cur)
    return groups


def group_lines(words: list[OcrWord], cfg: LayoutConfig, use_line_ids: bool = False) -> list[Line]:
    if not words:
        return []
    if use_line_ids and all(w.line_id is not None for w in words):
        buckets: "OrderedDict[int, list[OcrWord]]" = OrderedDict()
        for w in words:
            buckets.setdefault(w.line_id, []).append(w)  # type: ignore[arg-type]
        groups = [sorted(g, key=lambda w: w.box.x) for g in buckets.values()]
    else:
        groups = [g for row in _cluster_rows(words) for g in _split_columns(row, cfg.column_gap_factor)]
    lines = [_make_line(g) for g in groups]
    lines.sort(key=lambda ln: (ln.box.y, ln.box.x))
    return lines


def assign_paragraphs(lines: list[Line], cfg: LayoutConfig) -> list[Line]:
    """Attach each line to the paragraph whose last line sits directly above it."""
    paras: list[list[Line]] = []
    for ln in sorted(lines, key=lambda l: (l.box.y, l.box.x)):
        for p in reversed(paras):
            last = p[-1]
            gap = ln.box.y - last.box.bottom
            if (-0.5 * ln.box.h <= gap <= cfg.paragraph_gap_factor * ln.box.h
                    and last.box.horizontal_overlap(ln.box) >= 0.3):
                p.append(ln)
                break
        else:
            paras.append([ln])
    ordered: list[Line] = []
    for i, p in enumerate(paras):
        for ln in p:
            ln.paragraph = i
            ordered.append(ln)
    return ordered


# colour sampling
def sample_colors(img: np.ndarray, box: Rect):
    """Estimate (fg, bg) as RGB tuples for a word box from a BGR image."""
    h, w = img.shape[:2]
    x0, y0 = max(0, int(box.x)), max(0, int(box.y))
    x1, y1 = min(w, int(math.ceil(box.right))), min(h, int(math.ceil(box.bottom)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None, None
    crop = img[y0:y1, x0:x1, :3].reshape(-1, 3).astype(np.float32)
    lum = crop @ np.array([0.114, 0.587, 0.299], dtype=np.float32)
    bg = np.median(crop, axis=0)
    bg_l = float(np.median(lum))
    d = np.abs(lum - bg_l)
    dmax = float(d.max())
    if dmax < 30:                                  # nearly flat: pick a readable colour
        fg = np.array([0, 0, 0] if bg_l > 128 else [255, 255, 255], dtype=np.float32)
    else:
        fg = crop[d >= 0.85 * dmax].mean(axis=0)
    to_rgb = lambda c: (int(c[2]), int(c[1]), int(c[0]))
    return to_rgb(fg), to_rgb(bg)


# pipeline
def build_layout(result: OCRResult, image: Optional[np.ndarray], cfg: LayoutConfig,
                 scale: float = 1.0) -> Layout:
    """OCR result (in `scale`-d image coordinates) -> Layout in original image coordinates."""
    words = result.words
    if abs(scale - 1.0) > 1e-6:
        words = [replace(w, box=w.box.scaled(1.0 / scale)) for w in words]
    words = filter_words(words, cfg, result.has_confidence)
    lines = assign_paragraphs(group_lines(words, cfg, result.has_line_ids), cfg)
    if image is not None:
        height, width = image.shape[:2]
    else:
        width, height = round(result.width / scale), round(result.height / scale)
    layout = Layout(lines, int(width), int(height), image)
    if image is not None and cfg.sample_colors:
        analyze_layout(layout, cfg.ink)
    return layout


# cache
def frame_key(frame: np.ndarray, extra: str = "") -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(repr(frame.shape).encode())
    h.update(extra.encode())
    h.update(np.ascontiguousarray(frame[::2, ::2]).tobytes())
    return h.hexdigest()


class OcrCache:
    """Small thread-safe LRU keyed by frame hash (+ engine/settings tag)."""

    def __init__(self, capacity: int = 6) -> None:
        self.capacity = capacity
        self._d: "OrderedDict[str, Layout]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Layout]:
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key: str, value: Layout) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.capacity:
                self._d.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


# worker
@dataclass
class OcrOutcome:
    job_id: int
    epoch: int
    layout: Optional[Layout] = None
    elapsed_ms: float = 0.0
    from_cache: bool = False
    error: Optional[str] = None
    engine: str = ""


@dataclass
class _Job:
    id: int
    epoch: int
    frame: np.ndarray
    cfg: LayoutConfig
    scale: float
    tag: str


def _resize(img: np.ndarray, scale: float) -> np.ndarray:
    from PIL import Image

    h, w = img.shape[:2]
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    resample = Image.Resampling.LANCZOS if scale > 1 else Image.Resampling.AREA
    return np.asarray(Image.fromarray(img).resize(size, resample))


class OcrWorker(threading.Thread):
    """Single background OCR thread with a one-slot 'latest job wins' queue.

    * A newly submitted job replaces any job still waiting (stale-job cancellation).
    * `cancel_pending()` also invalidates a job that is already running: its result is
      discarded when it finishes (OCR calls themselves cannot be interrupted).
    * Engine failures never kill the thread: the engine is re-created on the next job and,
      after MAX_FAILURES in a row, OCR backs off exponentially (max 60 s) before retrying.
    """

    MAX_FAILURES = 3

    def __init__(self, engine_factory: Callable[[], OcrEngine],
                 on_outcome: Callable[[OcrOutcome], None], cache: Optional[OcrCache] = None) -> None:
        super().__init__(daemon=True, name="ocr-worker")
        self._factory = engine_factory
        self._on_outcome = on_outcome
        self.cache = cache or OcrCache()
        self._cv = threading.Condition()
        self._job: Optional[_Job] = None
        self._next_id = 1
        self._min_valid_id = 1
        self._stopping = False
        self._engine: Optional[OcrEngine] = None
        self._engine_dirty = False
        self._failures = 0
        self._retry_at = 0.0

    # ---- public API (any thread)
    def submit(self, frame: np.ndarray, *, epoch: int = 0, cfg: Optional[LayoutConfig] = None,
               scale: float = 1.0, tag: str = "") -> int:
        with self._cv:
            jid = self._next_id
            self._next_id += 1
            self._job = _Job(jid, epoch, frame, cfg or LayoutConfig(), scale, tag)
            self._cv.notify()
            return jid

    def cancel_pending(self) -> None:
        with self._cv:
            self._job = None
            self._min_valid_id = self._next_id

    def set_engine_factory(self, factory: Callable[[], OcrEngine]) -> None:
        with self._cv:
            self._factory = factory
            self._engine_dirty = True
            self._failures = 0
            self._retry_at = 0.0

    def stop(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify()

    # ---- thread body
    def run(self) -> None:
        while True:
            with self._cv:
                while self._job is None and not self._stopping:
                    self._cv.wait()
                if self._stopping:
                    break
                job, self._job = self._job, None
                factory, dirty = self._factory, self._engine_dirty
                self._engine_dirty = False
            if dirty:
                self._close_engine()
            outcome = self._process(job, factory)
            with self._cv:
                stale = job.id < self._min_valid_id
            if not stale:
                try:
                    self._on_outcome(outcome)
                except Exception:  # pragma: no cover - callback bugs must not kill OCR
                    log.exception("OCR outcome callback failed")
        self._close_engine()

    def _close_engine(self) -> None:
        if self._engine is not None:
            try:
                self._engine.close()
            except Exception:
                log.exception("closing OCR engine")
            self._engine = None

    def _process(self, job: _Job, factory: Callable[[], OcrEngine]) -> OcrOutcome:
        t0 = time.perf_counter()
        key = frame_key(job.frame, job.tag)
        cached = self.cache.get(key)
        if cached is not None:
            return OcrOutcome(job.id, job.epoch, cached, 0.0, True, None, "cache")
        wait = self._retry_at - time.monotonic()
        if wait > 0:
            return OcrOutcome(job.id, job.epoch, error=f"OCR backing off after repeated failures (retry in {wait:.0f}s)")
        try:
            if self._engine is None:
                self._engine = factory()
            eng = self._engine
            scale = job.scale
            if eng.max_dimension:
                scale = min(scale, eng.max_dimension / max(job.frame.shape[:2]))
            img = _resize(job.frame, scale) if abs(scale - 1.0) > 1e-3 else job.frame
            result = eng.recognize(img)
            layout = build_layout(result, job.frame, job.cfg, scale)
            self._failures = 0
            self.cache.put(key, layout)
            return OcrOutcome(job.id, job.epoch, layout, (time.perf_counter() - t0) * 1000, False, None, eng.name)
        except Exception as exc:
            log.exception("OCR failed")
            self._failures += 1
            self._close_engine()  # re-create on next job
            if self._failures >= self.MAX_FAILURES:
                self._retry_at = time.monotonic() + min(60.0, 5.0 * 2 ** (self._failures - self.MAX_FAILURES))
            return OcrOutcome(job.id, job.epoch, error=f"{type(exc).__name__}: {exc}")

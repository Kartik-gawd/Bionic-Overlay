"""Application controller: owns all components and the capture -> detect -> OCR -> render loop."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import fields
from typing import Optional

from PySide6.QtCore import QObject, QSize, QTimer, Signal

from . import winapi
from .bionic_engine import BionicConfig, BionicEngine
from .capture import ChangeDetector, ScreenCapture
from .geometry import Rect
from .hotkeys import HotkeyManager
from .ink import InkConfig
from .native_edit import config as native_config
from .native_edit.controller import Outcome
from .native_edit.service import NativeEditService
from .ocr import available_engines, create_engine
from .overlay import OverlayWindow
from .recognition import Layout, LayoutConfig, OcrCache, OcrOutcome, OcrWorker
from .region import RegionSelector
from .renderer import OverlayRenderer, RenderStyle
from .settings import Settings

log = logging.getLogger(__name__)


class OverlayController(QObject):
    state_changed = Signal()
    settings_changed = Signal()      # settings modified from outside the settings window (hotkeys, ...)
    status = Signal(str)
    notify = Signal(str, str)        # (title, message) -> tray toast
    hotkey_failed = Signal(str, str)  # (hotkey name, reason); name '*' clears all marks
    _hotkey_fired = Signal(str)
    _ocr_done = Signal(object)

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.enabled = False
        self.paused = False
        self.selecting = False
        self.capture = ScreenCapture()
        self.overlay = OverlayWindow()
        self.bionic = BionicEngine(self._bionic_config())
        self.renderer = OverlayRenderer(self._render_style())
        self.detector = ChangeDetector()
        self.cache = OcrCache()
        self.worker = OcrWorker(self._engine_factory(), self._ocr_done.emit, self.cache)
        self.hotkeys = HotkeyManager(self._hotkey_fired.emit, self._hotkey_error)
        self.native = NativeEditService(lambda: native_config.from_settings(self.settings), self)
        self.native.outcome.connect(self._on_native_outcome)
        self._native_mode = "toggle"
        self._hold_until = 0.0
        self.selector: Optional[RegionSelector] = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save)
        self._hotkey_fired.connect(self._on_hotkey)
        self._ocr_done.connect(self._on_ocr)

        self._region: Optional[Rect] = None
        self._epoch = 0
        self._expected_job: Optional[int] = None   # only this job's result may be displayed
        self._layout: Optional[Layout] = None
        self._showing = False
        self._target_hwnd = 0
        self._ocr_sig = self._cap_sig = self._hk_sig = None
        self._fix_engine_choice()
        self.apply_settings()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.worker.start()
        self.hotkeys.start()
        self._register_hotkeys()
        if self.settings.capture_mode == "region" and not self.settings.region:
            self.status.emit("Press Ctrl+Alt+S to choose a reading region, then Ctrl+Alt+O to enable.")
        else:
            self.status.emit("Ready. Press Ctrl+Alt+O to enable the overlay.")

    def shutdown(self) -> None:
        self.disable()
        self._save()
        self.hotkeys.stop()
        self.worker.stop()
        self.native.stop()
        self.capture.close()
        self.overlay.close()

    # ------------------------------------------------------------------ settings plumbing
    def _bionic_config(self) -> BionicConfig:
        return BionicConfig(**{f.name: getattr(self.settings, f.name) for f in fields(BionicConfig)})

    def _render_style(self) -> RenderStyle:
        return RenderStyle(**{f.name: getattr(self.settings, f.name) for f in fields(RenderStyle)})

    def _fix_engine_choice(self) -> None:
        avail = available_engines()
        if avail and self.settings.ocr_engine not in avail:
            log.warning("OCR engine %s unavailable, using %s", self.settings.ocr_engine, avail[0])
            self.settings.ocr_engine = avail[0]

    def _ink_config(self) -> InkConfig:
        s = self.settings
        return InkConfig(skip_bold=s.skip_bold_words, bold_abs=s.bold_stroke_abs, bold_rel=s.bold_stroke_rel,
                         skip_colored=s.skip_colored_words, skip_underlined=s.skip_underlined_words)

    def _engine_factory(self):
        s = self.settings
        name, lang, cmd, tlang = s.ocr_engine, s.ocr_language, s.tesseract_cmd, s.tesseract_lang
        return lambda: create_engine(name, lang, cmd, tlang)

    def _ocr_tag(self) -> str:
        return repr(self._ocr_sig)

    def apply_settings(self) -> None:
        s = self.settings
        self.bionic.configure(self._bionic_config())
        self.renderer.set_style(self._render_style())
        self.detector.configure(change_fraction=s.change_fraction, debounce_s=s.debounce_ms / 1000,
                                min_interval_s=s.ocr_interval_ms / 1000)
        self.timer.setInterval(max(50, min(s.ocr_interval_ms, 250)))   # poll often, OCR rarely
        refresh = False
        ocr_sig = (s.ocr_engine, s.ocr_language, s.tesseract_cmd, s.tesseract_lang, s.ocr_scale, s.min_confidence,
                   s.skip_bold_words, s.bold_stroke_abs, s.bold_stroke_rel, s.skip_colored_words, s.skip_underlined_words)
        if ocr_sig != self._ocr_sig:
            first = self._ocr_sig is None
            self._ocr_sig = ocr_sig
            if not first:
                self.worker.set_engine_factory(self._engine_factory())
                refresh = True
        cap_sig = (s.capture_mode, s.monitor_index)
        if cap_sig != self._cap_sig:
            first = self._cap_sig is None
            self._cap_sig = cap_sig
            if not first:
                self._region = None
                refresh = True
        hk_sig = tuple(sorted(s.hotkeys.items()))
        if hk_sig != self._hk_sig:
            self._hk_sig = hk_sig
            if self.hotkeys.is_alive():
                self._register_hotkeys()
        if refresh and self.enabled:
            self.detector.force()
        else:
            self._render_current()

    def set_option(self, name: str, value, notify: bool = True) -> None:
        setattr(self.settings, name, value)
        self.settings.validate()
        self.apply_settings()
        self._save_timer.start(600)
        if notify:
            self.settings_changed.emit()

    def set_hotkey(self, name: str, spec: str) -> None:
        hk = dict(self.settings.hotkeys)
        hk[name] = spec
        self.set_option("hotkeys", hk, notify=False)

    def _save(self) -> None:
        try:
            self.settings.save()
        except Exception:
            log.exception("saving settings")

    # ------------------------------------------------------------------ hotkeys
    def _register_hotkeys(self) -> None:
        hk = dict(self.settings.hotkeys)
        if not (self.enabled or self.selecting):
            hk["disable"] = ""        # Esc is only grabbed globally while the overlay is active
        self.hotkey_failed.emit("*", "")          # clear marks; the hotkey thread re-reports failures
        self.hotkeys.set_bindings(hk)

    def _hotkey_error(self, name: str, spec: str, msg: str) -> None:
        self.status.emit(f"Hotkey '{name}' ({spec}) not registered: {msg}")
        self.hotkey_failed.emit(name, msg)

    def _on_hotkey(self, name: str) -> None:
        s = self.settings
        if name == "toggle": self.toggle()
        elif name == "refresh": self.refresh()
        elif name == "pause": self.toggle_pause()
        elif name == "bold_down": self.set_option("bold_ratio", round(max(0.05, s.bold_ratio - 0.05), 2))
        elif name == "bold_up": self.set_option("bold_ratio", round(min(0.95, s.bold_ratio + 0.05), 2))
        elif name == "select_region": self.select_region()
        elif name == "select_window": self.select_window_now()
        elif name == "toggle_debug": self.set_option("debug_boxes", not s.debug_boxes)
        elif name == "bionic_selection": self.native_hotkey("toggle")
        elif name == "restore_selection": self.native_hotkey("restore")
        elif name == "disable":
            if self.selecting and self.selector:
                self.selector.cancel()
            else:
                self.disable()

    # ------------------------------------------------------------------ commands
    def toggle(self) -> None:
        self.disable() if self.enabled else self.enable()

    def enable(self) -> None:
        if self.enabled:
            return
        s = self.settings
        if s.capture_mode == "region" and not s.region:
            self.select_region(enable_after=True)
            return
        if s.capture_mode == "window" and not self._target_hwnd and not self._pick_foreground_window():
            self.status.emit("Window mode: bring the target window to the front and press the window hotkey.")
            return
        self.enabled = True
        self.paused = False
        self._reset_region_state()
        self.timer.start()
        self._register_hotkeys()
        self.status.emit("Overlay ON")
        self.state_changed.emit()
        self._tick()

    def disable(self) -> None:
        was = self.enabled
        self.enabled = False
        self.timer.stop()
        self.worker.cancel_pending()
        self.overlay.set_image(None)
        self.overlay.hide()
        self._layout, self._showing, self._region, self._expected_job = None, False, None, None
        if was:
            self._register_hotkeys()
            self.status.emit("Overlay OFF")
        self.state_changed.emit()

    def toggle_pause(self) -> None:
        if not self.enabled:
            return
        self.paused = not self.paused
        if self.paused:
            self.worker.cancel_pending()
            self.overlay.set_image(None)
            self.overlay.hide()
            self._showing, self._expected_job = False, None
            self.status.emit("Paused (original screen visible)")
        else:
            self._reset_region_state()
            self.status.emit("Resumed")
            self._tick()
        self.state_changed.emit()

    def refresh(self) -> None:
        """Manual re-OCR / recalibration: bypass the cache and the change detector."""
        if not self.enabled or self.paused:
            return
        self.cache.clear()
        self.detector.force()
        self.status.emit("Refreshing...")
        self._tick()

    def _reset_region_state(self) -> None:
        self._region = None
        self._expected_job = None
        self._layout = None
        self.detector.reset()

    # ---- region / window selection
    def select_region(self, enable_after: bool = False) -> None:
        if self.selecting:
            return
        self.selecting = True
        self._register_hotkeys()
        self.overlay.hide()
        self.selector = RegionSelector()
        self.selector.selected.connect(lambda r: self._region_chosen(r, enable_after))
        self.selector.cancelled.connect(self._region_cancelled)
        init = Rect.from_list(self.settings.region)
        self.selector.start(self.capture.virtual_screen(), init)

    def _end_selection(self) -> None:
        self.selecting = False
        if self.selector:
            self.selector.deleteLater()
            self.selector = None
        self._register_hotkeys()

    def _region_chosen(self, rect: Rect, enable_after: bool) -> None:
        self._end_selection()
        self.settings.region = rect.to_list()
        self.settings.capture_mode = "region"
        self.apply_settings()
        self._save()
        self.settings_changed.emit()
        self._reset_region_state()
        self.status.emit(f"Region saved: {rect.as_ints()[2]} x {rect.as_ints()[3]}")
        if enable_after and not self.enabled:
            self.enable()

    def _region_cancelled(self) -> None:
        self._end_selection()
        self.status.emit("Region selection cancelled")

    def _pick_foreground_window(self) -> bool:
        hwnd = winapi.get_foreground_hwnd()
        if not hwnd or winapi.window_pid(hwnd) == os.getpid():
            return False
        self._target_hwnd = hwnd
        self.settings.capture_mode = "window"
        self.apply_settings()
        self.status.emit(f"Target window: {winapi.window_title(hwnd) or hwnd}")
        self.settings_changed.emit()
        return True

    def select_window_now(self) -> None:
        if self._pick_foreground_window():
            self._reset_region_state()
            if not self.enabled:
                self.enable()
        else:
            self.status.emit("No suitable foreground window to target")

    def select_window_delayed(self, ms: int = 3000) -> None:
        self.status.emit(f"Switch to the target window - picking in {ms // 1000} s...")
        QTimer.singleShot(ms, self.select_window_now)

    # ------------------------------------------------------------------ main loop
    def _resolve_region(self) -> Optional[Rect]:
        s = self.settings
        if s.capture_mode == "region":
            return Rect.from_list(s.region)
        if s.capture_mode == "screen":
            mons = self.capture.monitors()
            return mons[min(max(1, s.monitor_index), len(mons) - 1)] if len(mons) > 1 else mons[0]
        hwnd = self._target_hwnd
        if not hwnd or not winapi.is_window(hwnd):
            if hwnd:
                self._target_hwnd = 0
                self.status.emit("Target window closed")
            return None
        if winapi.is_iconic(hwnd) or (s.window_hide_in_background and winapi.get_foreground_hwnd() != hwnd):
            return None
        r = winapi.client_rect_screen(hwnd)
        if not r:
            return None
        return Rect(*r).intersection(self.capture.virtual_screen())

    def _grab(self, rect: Rect):
        ov = self.overlay
        hide = ov.isVisible() and not ov.capture_excluded
        if hide:                       # fallback for Windows builds without WDA_EXCLUDEFROMCAPTURE
            ov.hide()
            time.sleep(0.04)
        try:
            return self.capture.grab(rect)
        finally:
            if hide:
                ov.show_overlay()

    def _tick(self) -> None:
        if not self.enabled or self.paused or self.selecting or time.monotonic() < self._hold_until:
            return
        region = self._resolve_region()
        if region is None:
            if self.overlay.isVisible():
                self.overlay.hide()
            self._region, self._expected_job = None, None
            return
        if region != self._region:
            self._region = region
            self._epoch += 1
            self.worker.cancel_pending()
            self.detector.reset()
            self.renderer.reset()
            self._layout, self._showing, self._expected_job = None, False, None
            self.overlay.set_image(None)
            self.overlay.set_region(region)
        if not self.overlay.isVisible():
            self.overlay.show_overlay()
        try:
            frame = self._grab(region)
        except Exception as exc:
            log.exception("capture failed")
            self.status.emit(f"Capture failed: {exc}")
            return
        res = self.detector.update(frame, time.monotonic())
        if res.stale:
            self._expected_job = None                       # any in-flight result is now outdated
            if self.settings.hide_on_change and self._showing:
                self.overlay.set_image(None)                # fallback: show the untouched original
                self._showing = False
        if res.trigger:
            s = self.settings
            self._expected_job = self.worker.submit(
                frame, epoch=self._epoch, cfg=LayoutConfig(min_confidence=s.min_confidence, ink=self._ink_config()),
                scale=s.ocr_scale, tag=self._ocr_tag())

    def _on_ocr(self, out: OcrOutcome) -> None:
        if out.job_id != self._expected_job or out.epoch != self._epoch or not self.enabled:
            return
        if out.error or out.layout is None:
            self.status.emit(f"OCR problem: {out.error} - original stays visible")
            return
        self._layout = out.layout
        self._render_current()
        n = len(out.layout.words)
        self.status.emit(f"{n} words - {'cached' if out.from_cache else f'{out.elapsed_ms:.0f} ms'}")

    def _render_current(self) -> None:
        if self._layout is None or not self.enabled or self.paused:
            return
        lay = self._layout
        img = self.renderer.render(self.bionic.format(lay), QSize(lay.width, lay.height))
        self.overlay.set_image(img)
        self._showing = True

    # ------------------------------------------------------------------ native Office editing
    def native_hotkey(self, mode: str = "toggle") -> None:
        """Smart hotkey: edit the real selection through the app's object model; fall back to the overlay."""
        self._native_mode = mode
        if self.settings.native_edit_enabled:
            self.native.request(mode)
        elif mode == "toggle" and self.settings.selection_fallback_to_overlay:
            self.toggle()

    def _on_native_outcome(self, value: str) -> None:
        out, s = Outcome(value), self.settings
        if out in (Outcome.APPLIED, Outcome.RESTORED):
            self.status.emit("Bionic formatting applied" if out is Outcome.APPLIED else "Original formatting restored")
            if s.show_toasts:
                self.notify.emit("Bionic Overlay", "Bionic formatting applied" if out is Outcome.APPLIED
                                 else "Original formatting restored")
            self._hold_overlay(1000)
        elif out in (Outcome.UNSUPPORTED_APP, Outcome.NO_SELECTION):
            if self._native_mode == "toggle" and s.selection_fallback_to_overlay:
                self.toggle()                                   # not Office / nothing selected -> overlay
            else:
                self.status.emit("Nothing to do: no supported selection in the foreground app")
        elif out is Outcome.UNCERTAIN_NOT_MODIFIED:
            self.status.emit("Selection not modified (unsafe or unverifiable) - see bionic.log")
            if s.show_toasts:
                self.notify.emit("Bionic Overlay", "Selection left untouched (see log for the reason)")
        elif out is Outcome.ERROR:
            self.status.emit("Native edit failed - see bionic.log")
            if s.show_toasts:
                self.notify.emit("Bionic Overlay", "Native edit failed - see log")
        else:
            self.status.emit(out.value.replace("_", " ").capitalize())

    def _hold_overlay(self, ms: int) -> None:
        """After a native edit, don't OCR a half-updated frame: blank + pause the overlay briefly, then refresh."""
        if not self.enabled:
            return
        self._hold_until = time.monotonic() + ms / 1000.0
        self.worker.cancel_pending()
        self.overlay.set_image(None)
        self._showing, self._expected_job = False, None
        QTimer.singleShot(ms + 50, self.refresh)

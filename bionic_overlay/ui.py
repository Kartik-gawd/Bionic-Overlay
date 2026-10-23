"""Settings window and tray icon."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFontComboBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMenu, QPushButton, QSlider, QSpinBox, QSystemTrayIcon,
                               QTabWidget, QVBoxLayout, QWidget)

from . import config
from .hotkeys import normalize_hotkey, parse_hotkey
from .ocr import available_engines


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(30, 64, 120))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 12, 12)
    p.setPen(QColor(255, 255, 255))
    f = QFont("Segoe UI")
    f.setPixelSize(34)
    f.setBold(True)
    p.setFont(f)
    p.drawText(8, 46, "B")
    f.setBold(False)
    p.setFont(f)
    p.drawText(33, 46, "r")
    p.end()
    return QIcon(pm)


class SettingsWindow(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.c, self.s = controller, controller.settings
        self._syncers = []
        self.setWindowTitle("Bionic Overlay")
        self.setWindowIcon(make_icon())
        self.setMinimumWidth(480)
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.btn_toggle = QPushButton()
        self.btn_toggle.clicked.connect(self.c.toggle)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self.c.toggle_pause)
        btn_refresh = QPushButton("Refresh (re-OCR)")
        btn_refresh.clicked.connect(self.c.refresh)
        for b in (self.btn_toggle, self.btn_pause, btn_refresh):
            top.addWidget(b)
        root.addLayout(top)

        tabs = QTabWidget()
        tabs.addTab(self._capture_tab(), "Capture")
        tabs.addTab(self._ocr_tab(), "OCR")
        tabs.addTab(self._bionic_tab(), "Bionic")
        tabs.addTab(self._look_tab(), "Appearance")
        tabs.addTab(self._native_tab(), "Office")
        tabs.addTab(self._hotkey_tab(), "Hotkeys")
        root.addWidget(tabs)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.c.state_changed.connect(self.refresh_state)
        self.c.settings_changed.connect(self.sync_from_settings)
        self.c.status.connect(self.status.setText)
        self.c.hotkey_failed.connect(self._hk_failed)
        self.refresh_state()

    # widget factories (each one registers a syncer so external changes show up)
    def _set(self, attr, value):
        self.c.set_option(attr, value, notify=False)

    def _reg(self, widget, setter, attr):
        def sync():
            widget.blockSignals(True)
            setter(getattr(self.s, attr))
            widget.blockSignals(False)
        self._syncers.append(sync)
        sync()
        return widget

    def _spin(self, attr, lo, hi, step=1, suffix=""):
        w = QSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        w.setSuffix(suffix)
        w.valueChanged.connect(lambda v: self._set(attr, v))
        return self._reg(w, w.setValue, attr)

    def _dspin(self, attr, lo, hi, step=0.05):
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        w.setDecimals(2)
        w.valueChanged.connect(lambda v: self._set(attr, v))
        return self._reg(w, w.setValue, attr)

    def _pct(self, attr, lo=0, hi=100):
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        sl, lbl = QSlider(Qt.Orientation.Horizontal), QLabel()
        sl.setRange(lo, hi)
        lbl.setMinimumWidth(38)
        lay.addWidget(sl)
        lay.addWidget(lbl)

        def on(v):
            lbl.setText(f"{v}%")
            self._set(attr, v / 100.0)

        sl.valueChanged.connect(on)

        def setter(v):
            sl.setValue(round(v * 100))
            lbl.setText(f"{round(v * 100)}%")

        self._reg(sl, setter, attr)
        return box

    def _check(self, attr, text):
        w = QCheckBox(text)
        w.toggled.connect(lambda v: self._set(attr, v))
        return self._reg(w, w.setChecked, attr)

    def _color_row(self, attr):
        """Colour button + 'Auto' reset. Value is 'auto' or '#rrggbb'."""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        btn, auto = QPushButton(), QPushButton("Auto")
        lay.addWidget(btn)
        lay.addWidget(auto)

        def paint(v):
            if v == "auto":
                btn.setText("Auto (same as the text)")
                btn.setStyleSheet("")
            else:
                dark = QColor(v).lightness() < 128
                btn.setText(v.upper())
                btn.setStyleSheet(f"background:{v}; color:{'#fff' if dark else '#000'}")

        def pick():
            cur = QColor(self.s.text_color) if self.s.text_color != "auto" else QColor(30, 64, 180)
            c = QColorDialog.getColor(cur, self, "Colour of the bold letters")
            if c.isValid():
                self._set(attr, c.name())
                paint(c.name())

        def reset():
            self._set(attr, "auto")
            paint("auto")

        btn.clicked.connect(pick)
        auto.clicked.connect(reset)
        self._reg(btn, paint, attr)
        return box

    def _combo(self, attr, items):
        w = QComboBox()
        w.addItems(list(items))
        w.currentTextChanged.connect(lambda v: self._set(attr, v))
        return self._reg(w, w.setCurrentText, attr)

    # tabs
    def _capture_tab(self):
        w, f = QWidget(), QFormLayout()
        w.setLayout(f)
        f.addRow("Capture mode", self._combo("capture_mode", config.CAPTURE_MODES))
        f.addRow("Monitor (screen mode)", self._spin("monitor_index", 1, 16))
        self.region_label = QLabel()
        f.addRow("Region", self.region_label)
        b1 = QPushButton("Select region...")
        b1.clicked.connect(lambda: self.c.select_region())
        b2 = QPushButton("Pick target window (3 s)")
        b2.clicked.connect(lambda: self.c.select_window_delayed())
        row = QHBoxLayout()
        row.addWidget(b1)
        row.addWidget(b2)
        f.addRow(row)
        f.addRow(self._check("window_hide_in_background", "Window mode: hide when target is not in front"))
        return w

    def _ocr_tab(self):
        w, f = QWidget(), QFormLayout()
        w.setLayout(f)
        engines = available_engines() or list(config.OCR_ENGINES)
        f.addRow("OCR engine", self._combo("ocr_engine", engines))
        lang = QLineEdit()
        lang.setPlaceholderText("e.g. en-US (blank = Windows profile languages)")
        lang.editingFinished.connect(lambda: self._set("ocr_language", lang.text().strip()))
        f.addRow("Language", self._reg(lang, lang.setText, "ocr_language"))
        f.addRow("Refresh interval", self._spin("ocr_interval_ms", 100, 10000, 100, " ms"))
        f.addRow("Settle time (debounce)", self._spin("debounce_ms", 0, 5000, 50, " ms"))
        f.addRow("Upscale before OCR", self._dspin("ocr_scale", 0.5, 4.0, 0.25))
        f.addRow("Min. confidence", self._pct("min_confidence"))
        f.addRow(self._check("hide_on_change", "Blank overlay while content is changing"))
        f.addRow(self._check("skip_bold_words", "Skip words that are already bold (stroke-width test)"))
        f.addRow("  bold if stroke > (x cap height)", self._dspin("bold_stroke_abs", 0.05, 0.6, 0.01))
        f.addRow("  ... or x paragraph median", self._dspin("bold_stroke_rel", 1.05, 3.0, 0.05))
        f.addRow(self._check("skip_colored_words", "Skip words with a different colour (links, syntax)"))
        f.addRow(self._check("skip_underlined_words", "Skip underlined words"))
        return w

    def _bionic_tab(self):
        w, f = QWidget(), QFormLayout()
        w.setLayout(f)
        f.addRow("Bold ratio", self._pct("bold_ratio", 5, 95))
        f.addRow("Bold-letter colour", self._color_row("text_color"))
        f.addRow("Min. word length", self._spin("min_word_length", 1, 10))
        f.addRow("Short-word max length", self._spin("short_word_max", 0, 6))
        f.addRow("Short-word mode", self._combo("short_word_mode", config.SHORT_WORD_MODES))
        for attr, text in (("bold_numbers", "Bold numbers"), ("bold_urls", "Bold URLs"),
                           ("bold_emails", "Bold e-mail addresses"), ("bold_symbols", "Bold symbol-only tokens"),
                           ("bold_cjk", "Bold CJK text")):
            f.addRow(self._check(attr, text))
        return w

    def _look_tab(self):
        w, f = QWidget(), QFormLayout()
        w.setLayout(f)
        fam = QFontComboBox()
        fam.currentFontChanged.connect(lambda font: self._set("font_family", font.family()))
        f.addRow("Font family", self._reg(fam, lambda v: fam.setCurrentFont(QFont(v)), "font_family"))
        f.addRow("Background mode", self._combo("background_mode", config.BACKGROUND_MODES))
        f.addRow("Mask opacity", self._pct("mask_opacity"))
        f.addRow("Embolden strength", self._dspin("embolden_strength", 0.2, 4.0, 0.1))
        f.addRow("Embolden min. x-height (px)", self._dspin("embolden_min_xheight", 4, 40, 1))
        f.addRow("Text opacity", self._pct("opacity", 5, 100))
        f.addRow("Font scale", self._dspin("font_scale", 0.3, 3.0))
        f.addRow("Offset X (px)", self._spin("offset_x", -200, 200))
        f.addRow("Offset Y (px)", self._spin("offset_y", -200, 200))
        f.addRow("Line spacing", self._dspin("line_spacing", 0.5, 2.0, 0.01))
        f.addRow("Word spacing", self._dspin("word_spacing", 0.5, 2.0, 0.01))
        f.addRow(self._check("fit_width", "Fit each word to its OCR box width"))
        f.addRow("Alignment (if not fitting)", self._combo("text_align", config.TEXT_ALIGNS))
        f.addRow(self._check("debug_boxes", "Show alignment debug boxes"))
        return w

    def _native_tab(self):
        w, f = QWidget(), QFormLayout()
        w.setLayout(f)
        f.addRow(self._check("native_edit_enabled", "Edit the real selection in Word / PowerPoint / Excel"))
        f.addRow(self._check("selection_fallback_to_overlay", "Fall back to the overlay in other apps / no selection"))
        f.addRow(self._check("require_restorable", "Only edit when the original formatting can be restored"))
        f.addRow(self._check("ppt_include_selected_shapes", "PowerPoint: include whole selected shapes"))
        f.addRow(self._check("show_toasts", "Show tray notifications"))
        f.addRow("Max. selection (chars)", self._spin("max_selection_chars", 100, 500000, 1000))
        f.addRow(QLabel("Uses the bold ratio and word rules from the Bionic tab. Requires pywin32;\n"
                        "this app and Office must run at the same elevation level."))
        return w

    def _hotkey_tab(self):
        w, f = QWidget(), QFormLayout()
        w.setLayout(f)
        self._hk_edits = {}
        for name, label in config.HOTKEY_LABELS.items():
            e = QLineEdit(self.s.hotkeys.get(name, ""))
            e.editingFinished.connect(lambda n=name, ed=e: self._hk_changed(n, ed))
            self._hk_edits[name] = e
            f.addRow(label, e)
        f.addRow(QLabel("Format: ctrl+alt+o, ctrl+shift+f5, esc ... (blank disables a hotkey)"))
        return w

    def _hk_failed(self, name, reason):
        """Mark hotkeys that could not be registered (already used by another program)."""
        for n, e in self._hk_edits.items():
            if name == "*" or n == name:
                e.setStyleSheet("background:#ffd0d0" if name != "*" else "")
                e.setToolTip(reason if name != "*" else "")

    def _hk_changed(self, name, edit):
        text = edit.text().strip()
        try:
            if text:
                parse_hotkey(text)
                dupes = [n for n, v in self.s.hotkeys.items() if n != name and v
                         and normalize_hotkey(v) == normalize_hotkey(text)]
                if dupes:
                    raise ValueError(f"already used by '{dupes[0]}'")
        except ValueError as e:
            edit.setStyleSheet("background:#ffd0d0")
            self.status.setText(f"Invalid hotkey: {e}")
            return
        edit.setStyleSheet("")
        self.c.set_hotkey(name, text)

    # state
    def sync_from_settings(self):
        for fn in self._syncers:
            fn()
        self.refresh_state()

    def refresh_state(self):
        c = self.c
        self.btn_toggle.setText("Overlay: ON  (click to turn off)" if c.enabled else "Overlay: OFF  (click to turn on)")
        self.btn_pause.setText("Resume" if c.paused else "Pause")
        self.btn_pause.setEnabled(c.enabled)
        r = self.s.region
        self.region_label.setText(f"{r[2]} x {r[3]} at ({r[0]}, {r[1]})" if r else "not set")

    def closeEvent(self, e):  # noqa: N802 - hide to tray instead of quitting
        e.ignore()
        self.hide()


class Tray(QSystemTrayIcon):
    def __init__(self, controller, window, app) -> None:
        super().__init__(make_icon())
        self.setToolTip("Bionic Overlay")
        menu = QMenu()
        for text, fn in (("Toggle overlay", controller.toggle), ("Pause / resume", controller.toggle_pause),
                         ("Refresh (re-OCR)", controller.refresh),
                         ("Select region...", lambda: controller.select_region()),
                         ("Pick target window (3 s)", lambda: controller.select_window_delayed()),
                         ("Settings...", lambda: (window.show(), window.raise_(), window.activateWindow())),
                         ("Quit", app.quit)):
            a = QAction(text, menu)
            a.triggered.connect(fn)
            menu.addAction(a)
        self.setContextMenu(menu)
        self.activated.connect(lambda r: window.show() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        controller.state_changed.connect(
            lambda: self.setToolTip("Bionic Overlay - " + ("ON" if controller.enabled else "OFF")))
        self._ctrl_menu = menu
        controller.notify.connect(lambda title, msg: self.showMessage(title, msg, self.icon(), 2500))
"""Scope measurements: window Vpp/Vmax/Vmin/mean/AC RMS, hold Vpp, trigger lamp and step table."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QVariantAnimation, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout)

from gui import theme
from gui.widgets import AnimatedButton, TriggerLamp, set_visible_animated, set_visible_now
from sensors import scope_processor as sp
from sensors.scope_processor import ScopeProcessor
from sensors.teng_processor import TengProcessor

BANNER_HOLD_S = 1.0   # the firmware flags are per frame, keep a banner up this long after the last flagged frame


def _stat(title):
    box = QVBoxLayout()
    box.setSpacing(0)
    t = QLabel(title)
    t.setObjectName("caption")
    v = QLabel("--")
    v.setObjectName("value")
    box.addWidget(t)
    box.addWidget(v)
    return box, v


class ScopePanel(QFrame):
    window_changed = Signal(float)
    threshold_changed = Signal(float)
    hold_reset = Signal()

    HEADERS = ["#", "Vpp (V)", "+Peak (V)", "−Peak (V)", "Width (ms)", "Rise (ms)"]

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self._clip_until = 0.0
        self._cal_until = 0.0
        self._table_version = -1
        self._hold_shown: float | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(8)

        head = QHBoxLayout()
        t = QLabel("Scope measurements")
        t.setObjectName("cardTitle")
        self.win = QComboBox()
        for s in sp.WINDOWS_S:
            self.win.addItem(f"{int(s * 1000)} ms" if s < 1 else f"{s:g} s", s)
        self.win.setCurrentIndex(sp.WINDOWS_S.index(sp.DEFAULT_WINDOW_S))
        self.win.setToolTip("Sliding measurement window")
        cap = QLabel("Window")
        cap.setObjectName("caption")
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(cap)
        head.addWidget(self.win)
        outer.addLayout(head)

        self.clip_banner = QLabel()
        self.clip_banner.setWordWrap(True)
        self.clip_banner.setStyleSheet("background:rgba(229,72,77,0.14); color:#ff8a8e;"
                                       "border:1px solid rgba(229,72,77,0.4); border-radius:8px; padding:8px 12px;")
        self.cal_banner = QLabel("Calibrating baseline, keep the TENG untouched")
        self.cal_banner.setStyleSheet("background:rgba(230,180,80,0.12); color:#e6b450;"
                                      "border:1px solid rgba(230,180,80,0.35); border-radius:8px; padding:8px 12px;")
        self.clip_banner.hide()
        self.cal_banner.hide()
        outer.addWidget(self.clip_banner)
        outer.addWidget(self.cal_banner)

        self.vpp = QLabel("--")
        self.vpp.setStyleSheet(f"font-family:'{theme.MONO}'; font-size:44px; font-weight:500; color:{theme.TEXT};")
        cap = QLabel("Vpp, Vmax minus Vmin over the window. Width at 50%, rise 10 to 90%. * clipped")
        cap.setObjectName("caption")
        outer.addWidget(self.vpp)
        outer.addWidget(cap)

        row = QHBoxLayout()
        row.setSpacing(16)
        self.v = {}
        for k, title in (("max", "Vmax (V)"), ("min", "Vmin (V)"), ("mean", "Mean (V)"), ("rms", "AC RMS (V)")):
            box, lab = _stat(title)
            self.v[k] = lab
            row.addLayout(box)
        outer.addLayout(row)

        hold = QHBoxLayout()
        self.hold = QLabel("Hold Vpp  --")
        self.hold.setObjectName("value")
        self.btn_hold = AnimatedButton("Reset hold")
        self.btn_hold.setFixedHeight(32)
        hold.addWidget(self.hold)
        hold.addStretch(1)
        hold.addWidget(self.btn_hold)
        outer.addLayout(hold)

        self.info = QLabel("")
        self.info.setObjectName("caption")
        self.info.setWordWrap(True)
        outer.addWidget(self.info)

        # ---- step capture, with the trigger lamp ----
        outer.addSpacing(8)
        th = QHBoxLayout()
        th.setSpacing(8)
        self.lamp = TriggerLamp()
        self.state = QLabel("")
        lbl = QLabel("Step capture")
        lbl.setObjectName("cardTitle")
        self.thr = QDoubleSpinBox()
        self.thr.setRange(0.05, 100.0)
        self.thr.setDecimals(2)
        self.thr.setSingleStep(0.1)
        self.thr.setSuffix(" V")
        self.thr.setValue(sp.DEFAULT_THRESHOLD_V)
        self.thr.setToolTip("Trigger when |v| exceeds this level")
        cap = QLabel("Trigger at |v| above")
        cap.setObjectName("caption")
        th.addWidget(self.lamp)
        th.addWidget(lbl)
        th.addStretch(1)
        th.addWidget(cap)
        th.addWidget(self.thr)
        outer.addLayout(th)
        outer.addWidget(self.state)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setFixedHeight(32)
        self.table.setMinimumHeight(32 * 11 + 4)       # header + 10 rows
        outer.addWidget(self.table)
        self._flash = QVariantAnimation(self)
        self._flash.setDuration(900)
        self._flash.setStartValue(0.22)
        self._flash.setEndValue(0.0)
        self._flash.valueChanged.connect(self._flash_row)

        self.win.currentIndexChanged.connect(lambda: self.window_changed.emit(self.win.currentData()))
        self.thr.valueChanged.connect(self.threshold_changed)
        self.btn_hold.clicked.connect(self.hold_reset)

    # ------------------------------------------------------------------
    def update_from(self, scope: ScopeProcessor, teng: TengProcessor):
        now = time.monotonic()
        if teng.clipped:
            self._clip_until = now + BANNER_HOLD_S
            self.clip_banner.setText(f"Input beyond ±{teng.full_scale_v:.1f} V: peaks are clipped, "
                                     "so Vpp and peaks read low")
        if teng.calibrating:
            self._cal_until = now + BANNER_HOLD_S
        set_visible_animated(self.clip_banner, now < self._clip_until)
        set_visible_animated(self.cal_banner, now < self._cal_until)

        w = scope.latest
        if w is None:
            self.vpp.setText("--")
            for lab in self.v.values():
                lab.setText("--")
        else:
            self.vpp.setText(f"{w.vpp:.2f} V")
            self.v["max"].setText(f"{w.vmax:+.3f}")
            self.v["min"].setText(f"{w.vmin:+.3f}")
            self.v["mean"].setText(f"{w.mean:+.3f}")
            self.v["rms"].setText(f"{w.ac_rms:.3f}")
        self._set_hold(scope.hold_vpp)

        if teng.sr > 0:
            self.info.setText(f"Sample rate {teng.sr:.0f} per second. Pulses shorter than one sample period "
                              f"({1000.0 / teng.sr:.2f} ms) can be missed.")
        else:
            self.info.setText("Sample rate not known yet. Pulses shorter than one sample period "
                              "(1000/sr ms) can be missed.")

        self.lamp.set_capturing(scope.capturing)
        if scope.capturing:
            self.state.setText("Capturing step")
            self.state.setStyleSheet(f"color:{theme.ACCENT};")
        else:
            self.state.setText(f"Armed, waiting for |v| above {scope.threshold:.2f} V")
            self.state.setStyleSheet(f"color:{theme.TEXT2};")

        if scope.version != self._table_version:
            had = self._table_version >= 0
            self._table_version = scope.version
            self._fill_table(scope)
            if had and scope.steps:
                self._flash.start()                 # newest row lights up and fades

    def _set_hold(self, hold):
        """Hold Vpp rolls to a new maximum instead of jumping (a number that changes tweens)."""
        if hold is None:
            self._hold_shown = None
            self.hold.setText("Hold Vpp  --")
            return
        if self._hold_shown is None or hold < self._hold_shown:
            self._hold_shown = hold
        elif hold > self._hold_shown:
            self._hold_shown += (hold - self._hold_shown) * 0.35
            if hold - self._hold_shown < 0.005:
                self._hold_shown = hold
        self.hold.setText(f"Hold Vpp  {self._hold_shown:.2f} V")

    def _flash_row(self, alpha):
        if self.table.rowCount() == 0:
            return
        c = QColor(theme.ACCENT)
        c.setAlphaF(float(alpha))
        for col in range(self.table.columnCount()):
            it = self.table.item(0, col)
            if it:
                it.setBackground(QBrush(c))

    def _fill_table(self, scope: ScopeProcessor):
        rows = list(reversed(scope.steps))        # newest first, at most 10
        self.table.setRowCount(len(rows))
        for r, s in enumerate(rows):
            cells = [str(s.index), f"{s.vpp:.2f}", f"{s.pos_peak:+.2f}", f"{s.neg_peak:+.2f}",
                     "n/a" if s.width_ms is None else f"{s.width_ms:.2f}",
                     "n/a" if s.rise_ms is None else f"{s.rise_ms:.2f}"]
            if s.clipped:
                cells[0] += "*"
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                it.setTextAlignment(Qt.AlignVCenter | (Qt.AlignLeft if c == 0 else Qt.AlignRight))
                if s.clipped:
                    it.setForeground(QColor(theme.RED))
                    it.setToolTip("The input was clipped during this step, so its peaks read low")
                elif c == 0:
                    it.setForeground(QColor(theme.MUTED))
                self.table.setItem(r, c, it)

    def clear(self):
        self.vpp.setText("--")
        for lab in self.v.values():
            lab.setText("--")
        self._hold_shown = None
        self.hold.setText("Hold Vpp  --")
        self.table.setRowCount(0)
        self._table_version = -1
        set_visible_now(self.clip_banner, False)
        set_visible_now(self.cal_banner, False)
        self._clip_until = self._cal_until = 0.0
        self.lamp.set_capturing(False)

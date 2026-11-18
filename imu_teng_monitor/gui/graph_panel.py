"""Four scrolling plots: TENG, accelerometer, gyroscope, orientation (roll/pitch/yaw).

The TENG plot is the main one and carries the scope furniture: Vmax/Vmin cursors with labels, a band
marking the measured window and amber trigger-level markers at +/- the step-capture threshold.
(Magnetometer is shown numerically and saved to Excel, but not plotted.)
"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from config import settings
from gui import theme
from gui.widgets import set_visible_animated
from sensors.imu_processor import ImuProcessor
from sensors.scope_processor import ScopeProcessor
from sensors.teng_processor import TengProcessor

pg.setConfigOptions(antialias=False, background=theme.CARD, foreground=theme.MUTED)

GRID_ALPHA = 0.10


class GraphPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.window_s = settings.DEFAULT_GRAPH_WINDOW_S
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 0, 0, 0)
        self.paused_chip = QLabel("Paused, recording continues")
        self.paused_chip.setStyleSheet(f"color:{theme.ACCENT};")
        self.paused_chip.hide()
        lbl = QLabel("Time window")
        lbl.setObjectName("caption")
        self.combo = QComboBox()
        for s in settings.GRAPH_WINDOWS_S:
            self.combo.addItem(f"{s} s", s)
        self.combo.setCurrentIndex(settings.GRAPH_WINDOWS_S.index(settings.DEFAULT_GRAPH_WINDOW_S))
        self.combo.currentIndexChanged.connect(lambda: setattr(self, "window_s", self.combo.currentData()))
        bar.addWidget(self.paused_chip)
        bar.addStretch(1)
        bar.addWidget(lbl)
        bar.addWidget(self.combo)
        lay.addLayout(bar)

        frame = QFrame()
        frame.setObjectName("card")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(8, 8, 8, 8)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground(theme.CARD)
        self.glw.ci.layout.setSpacing(4)
        fl.addWidget(self.glw)
        lay.addWidget(frame, 1)

        self.p_teng = self._plot(0, "TENG voltage", "V")
        self.c_teng = self.p_teng.plot(pen=pg.mkPen(theme.TENG_COLOR, width=1.5), name="TENG (V)")
        self.c_teng.setDownsampling(auto=True, method="peak")
        self.c_teng.setFillLevel(0)
        self.c_teng.setBrush(pg.mkBrush(230, 180, 80, 38))        # area above/below 0 V shows the sign
        self.c_teng.setClipToView(True)
        self.p_teng.addItem(pg.InfiniteLine(0, angle=0, pen=pg.mkPen(theme.MUTED, width=1, style=Qt.DashLine)))

        mono = QFont(theme.MONO)
        mono.setPixelSize(11)

        def cursor(text, color, pos):
            return pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(color, width=1, style=Qt.DashLine),
                                   label=text, labelOpts={"position": pos, "color": color, "movable": False,
                                                          "fill": (25, 25, 27, 230)})
        # measurement cursors (neutral) and trigger markers (accent)
        self.l_max = cursor("Vmax {value:0.2f} V", theme.TEXT2, 0.07)
        self.l_min = cursor("Vmin {value:0.2f} V", theme.TEXT2, 0.07)
        self.band = pg.LinearRegionItem(values=(0, 1), movable=False, brush=pg.mkBrush(237, 237, 237, 9),
                                        pen=pg.mkPen(None))
        self.band.setZValue(-10)
        self.t_hi = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(theme.ACCENT, width=1, style=Qt.DotLine),
                                    label="Trigger ±{value:0.2f} V",
                                    labelOpts={"position": 0.93, "color": theme.ACCENT, "movable": False,
                                               "fill": (25, 25, 27, 230)},
                                    markers=[("<|", 1.0, 8)])
        self.t_lo = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(theme.ACCENT, width=1, style=Qt.DotLine),
                                    markers=[("<|", 1.0, 8)])
        self._furniture = (self.l_max, self.l_min, self.band, self.t_hi, self.t_lo)
        for it in self._furniture:
            self.p_teng.addItem(it, ignoreBounds=True)
            it.hide()

        self.p_acc, self.c_acc = self._xyz(1, "Accelerometer", "m/s²", ("X", "Y", "Z"))
        self.p_gyr, self.c_gyr = self._xyz(2, "Gyroscope", "rad/s", ("X", "Y", "Z"))
        self.p_rpy, self.c_rpy = self._xyz(3, "Orientation", "deg", ("Roll", "Pitch", "Yaw"))
        self.plots = [self.p_teng, self.p_acc, self.p_gyr, self.p_rpy]
        self.glw.ci.layout.setRowStretchFactor(0, 3)          # the TENG trace is the main chart
        for p in self.plots[1:]:
            p.setXLink(self.p_teng)

    def _plot(self, row, title, unit):
        p = self.glw.addPlot(row=row, col=0)
        p.setTitle(title, color=theme.TEXT2, size="10pt")
        p.setLabel("left", unit, color=theme.MUTED)
        p.showGrid(x=True, y=True, alpha=GRID_ALPHA)
        p.addLegend(offset=(-8, 4), labelTextColor=theme.TEXT2, brush=pg.mkBrush(25, 25, 27, 200),
                    pen=pg.mkPen(None), colCount=3 if row else 1)
        p.setMouseEnabled(x=False, y=False)
        p.hideButtons()
        p.setMenuEnabled(False)
        p.enableAutoRange(axis="y")
        p.setAutoVisible(y=True)
        font = QFont(theme.MONO)
        font.setPixelSize(11)
        for name in ("left", "bottom"):
            ax = p.getAxis(name)
            ax.setTickFont(font)
            ax.setPen(pg.mkPen(theme.BORDER_HEX))
            ax.setTextPen(pg.mkPen(theme.MUTED))
        if row < 3:
            p.getAxis("bottom").setStyle(showValues=False)
        else:
            p.setLabel("bottom", "Time (s)", color=theme.MUTED)
        p.getAxis("left").setWidth(56)
        p.getAxis("left").enableAutoSIPrefix(False)         # no "(x0.001)" axis scaling
        return p

    def _xyz(self, row, title, unit, names):
        p = self._plot(row, title, unit)
        curves = []
        for color, n in zip(theme.SERIES, names):
            c = p.plot(pen=pg.mkPen(color, width=1.5), name=f"{n}")
            c.setClipToView(True)
            curves.append(c)
        return p, curves

    # ------------------------------------------------------------------
    def set_paused(self, paused: bool):
        set_visible_animated(self.paused_chip, paused)

    def refresh(self, teng: TengProcessor, imu: ImuProcessor, scope: ScopeProcessor):
        tn = max(teng.buffer._buf[teng.buffer.n - 1, 0] if teng.buffer.n else -1e9,
                 imu.buffer._buf[imu.buffer.n - 1, 0] if imu.buffer.n else -1e9)
        if tn < -1e8:
            return
        t1 = max(tn, self.window_s)                 # sweep from the left until the window is full
        t0 = t1 - self.window_s
        w = teng.buffer.window(t0)
        if len(w):
            self.c_teng.setData(w[:, 0], w[:, 1])
        w = imu.buffer.window(t0)
        if len(w):
            x = w[:, 0]
            for curves, base in ((self.c_acc, 1), (self.c_gyr, 4), (self.c_rpy, 7)):
                for i, c in enumerate(curves):
                    c.setData(x, w[:, base + i])
        self.p_teng.setXRange(t0, t1, padding=0)
        w = scope.latest
        if w is None:
            for it in self._furniture:
                it.hide()
        else:
            for it in self._furniture:          # show first: line labels only re-format while visible
                it.show()
            self.l_max.setPos(w.vmax)
            self.l_min.setPos(w.vmin)
            self.band.setRegion((w.t_start, w.t_end))
            self.t_hi.setPos(scope.threshold)
            self.t_lo.setPos(-scope.threshold)
            for ln in (self.l_max, self.l_min, self.t_hi):
                ln.label.valueChanged()

    def clear(self):
        for c in [self.c_teng, *self.c_acc, *self.c_gyr, *self.c_rpy]:
            c.setData([], [])
        for it in self._furniture:
            it.hide()

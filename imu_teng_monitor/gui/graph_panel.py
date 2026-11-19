"""Live charts: three TENG plots (Front, Middle, Back) stacked on a shared time axis, then Accelerometer,
Gyroscope and Orientation (roll/pitch/yaw).

Speed (see README, measured):
  * the UI thread only draws: samples are written into preallocated ring buffers by the receive thread and copied
    here under a short lock;
  * only the visible time window is drawn, min/max-decimated to at most 2 points per horizontal pixel (peaks survive);
  * 1 px pens, no antialiasing, no fill, no legends on the TENG plots, no per-frame autorange: the Y axes are
    rescaled at most 5 times per second;
  * the TENG and IMU plots live on separate canvases, and nothing is redrawn when no new data arrived.
"""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from config import settings
from gui import theme
from gui.widgets import set_visible_animated

pg.setConfigOptions(antialias=False, background=theme.CARD, foreground=theme.MUTED)
GRID_ALPHA = 0.10


def minmax_decimate(t: np.ndarray, v: np.ndarray, max_points: int):
    """Reduce (t, v) to at most `max_points` points keeping every bin's minimum and maximum, in time order."""
    n = len(t)
    if n <= max_points or max_points < 4:
        return t, v
    bins = max_points // 2
    step = n // bins
    m = step * bins
    vv = v[n - m:].reshape(bins, step)              # newest m samples, so the right edge stays exact
    tt = t[n - m:].reshape(bins, step)
    i_min, i_max = vv.argmin(axis=1), vv.argmax(axis=1)
    first, second = np.minimum(i_min, i_max), np.maximum(i_min, i_max)
    rows = np.arange(bins)
    out_t, out_v = np.empty(2 * bins), np.empty(2 * bins)
    out_t[0::2], out_v[0::2] = tt[rows, first], vv[rows, first]
    out_t[1::2], out_v[1::2] = tt[rows, second], vv[rows, second]
    if out_t[-1] < t[-1]:                           # always end the trace on the newest sample
        out_t, out_v = np.append(out_t, t[-1]), np.append(out_v, v[-1])
    return out_t, out_v


def nice_step(span: float, target: int = 4) -> float:
    """A 1 / 2 / 5 x 10^k tick step giving about `target` ticks over `span`."""
    raw = max(span, 1e-9) / target
    mag = 10 ** np.floor(np.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return float(m * mag)
    return float(10 * mag)


def _style_axes(p, bottom_values):
    font = QFont(theme.MONO)
    font.setPixelSize(11)
    for name in ("left", "bottom"):
        ax = p.getAxis(name)
        ax.setTickFont(font)
        ax.setPen(pg.mkPen(theme.BORDER_HEX))
        ax.setTextPen(pg.mkPen(theme.MUTED))
    p.getAxis("left").setWidth(56)
    p.getAxis("left").enableAutoSIPrefix(False)
    p.getAxis("bottom").setStyle(showValues=bottom_values)
    if bottom_values:
        p.setLabel("bottom", "Time (s)", color=theme.MUTED)


def _plot(glw, row, title, unit, bottom_values, title_color=theme.TEXT2, col=0):
    p = glw.addPlot(row=row, col=col)
    p.setTitle(title, color=title_color, size="9pt")
    p.setLabel("left", unit, color=theme.MUTED)
    # pyqtgraph's default horizontal grid draws several tick levels and cost ~17 ms per repaint (measured). We keep
    # the grid but give each Y axis ONE tick level with a 1/2/5 step chosen when the axis is rescaled (_yrange).
    p.showGrid(x=True, y=True, alpha=GRID_ALPHA)
    p.addItem(pg.InfiniteLine(0, angle=0, pen=pg.mkPen(theme.MUTED, width=1, style=Qt.DashLine)), ignoreBounds=True)
    p.setMouseEnabled(x=False, y=False)
    p.hideButtons()
    p.setMenuEnabled(False)
    p.enableAutoRange(False)                        # Y is rescaled by us, at most 5 times per second
    _style_axes(p, bottom_values)
    return p


def _curve(plot, color, name=None):
    c = plot.plot(pen=pg.mkPen(color, width=1), name=name, skipFiniteCheck=True)
    c.setDownsampling(ds=1, auto=False)
    c.setClipToView(False)
    return c


class GraphPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.window_s = settings.DEFAULT_GRAPH_WINDOW_S
        self._gen = -1
        self._y_last = 0.0
        self.p_teng: list = []
        self.c_teng: list = []

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
        self.combo.currentIndexChanged.connect(self._window_changed)
        bar.addWidget(self.paused_chip)
        bar.addStretch(1)
        bar.addWidget(lbl)
        bar.addWidget(self.combo)
        lay.addLayout(bar)

        frame = QFrame()
        frame.setObjectName("card")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(8, 8, 8, 8)
        fl.setSpacing(4)
        self.glw_teng = pg.GraphicsLayoutWidget()
        self.glw_imu = pg.GraphicsLayoutWidget()
        for g in (self.glw_teng, self.glw_imu):
            g.setBackground(theme.CARD)
            g.ci.layout.setSpacing(4)
        self.glw_teng.setMinimumHeight(330)             # three plots of >= 110 px
        self.glw_imu.setMinimumHeight(190)
        fl.addWidget(self.glw_teng, 5)
        fl.addWidget(self.glw_imu, 3)
        lay.addWidget(frame, 1)

        self.p_acc, self.c_acc = self._xyz(0, "Accelerometer", "m/s²", ("X", "Y", "Z"))
        self.p_gyr, self.c_gyr = self._xyz(1, "Gyroscope", "rad/s", ("X", "Y", "Z"))
        self.p_rpy, self.c_rpy = self._xyz(2, "Orientation", "deg", ("Roll", "Pitch", "Yaw"))
        self.imu_plots = [self.p_acc, self.p_gyr, self.p_rpy]
        self.imu_curves = self.c_acc + self.c_gyr + self.c_rpy

    def _window_changed(self):
        self.window_s = self.combo.currentData()
        self._gen = -1                                # redraw now

    def _xyz(self, col, title, unit, names):
        """IMU charts sit side by side (one column each) so every one gets a readable height."""
        p = _plot(self.glw_imu, 0, title, unit, True, col=col)
        p.addLegend(offset=(-6, 4), labelTextColor=theme.TEXT2, brush=pg.mkBrush(25, 25, 27, 200),
                    pen=pg.mkPen(None), colCount=1)
        return p, [_curve(p, color, n) for color, n in zip(theme.SERIES, names)]

    def set_channels(self, cfgs):
        """(Re)build the stacked TENG plots: 3 for the foot, 1 for the stock firmware."""
        self.glw_teng.clear()
        self.p_teng, self.c_teng = [], []
        n = len(cfgs)
        for k, cfg in enumerate(cfgs):
            p = _plot(self.glw_teng, k, cfg.label, "V", k == n - 1, title_color=cfg.color)
            if self.p_teng:
                p.setXLink(self.p_teng[0])          # shared time axis
            self.p_teng.append(p)
            self.c_teng.append(_curve(p, cfg.color))
        self._gen = -1

    def set_paused(self, paused: bool):
        set_visible_animated(self.paused_chip, paused)
        if not paused:
            self._gen = -1

    # ------------------------------------------------------------------ live
    def _max_points(self, plot) -> int:
        w = plot.vb.width()
        return int(max(200, (w if w > 50 else 800) * settings.MAX_POINTS_PER_PIXEL))

    def refresh(self, pl):
        """Draw if (and only if) new data arrived since the last draw. Called by the ~25 fps timer."""
        gen = pl.gen
        if gen == self._gen or not self.p_teng:
            return
        self._gen = gen
        chans = pl.channels
        if not chans:
            return
        tn = max(c.t_last for c in chans)
        t1 = max(tn, self.window_s)                  # sweep from the left until the window is full
        t0 = t1 - self.window_s
        now = time.perf_counter()
        rescale = now - self._y_last >= 1.0 / settings.YSCALE_MAX_HZ
        if rescale:
            self._y_last = now

        for k, (p, c) in enumerate(zip(self.p_teng, self.c_teng)):
            arr, _ = pl.view_teng(k, t0)
            if arr is None or not len(arr):
                continue
            x, y = minmax_decimate(arr[:, 0], arr[:, 1], self._max_points(p))
            c.setData(x, y, skipFiniteCheck=True)
            if rescale:
                self._yrange(p, y.min(), y.max())
        self.p_teng[0].setXRange(t0, t1, padding=0)

        imu = pl.view_imu(t0)
        if len(imu):
            t = imu[:, 0]
            mp = self._max_points(self.p_acc)
            for i, c in enumerate(self.imu_curves):
                x, y = minmax_decimate(t, imu[:, 1 + i], mp)
                c.setData(x, y, skipFiniteCheck=True)
            if rescale:
                for j, p in enumerate(self.imu_plots):
                    block = imu[:, 1 + 3 * j:4 + 3 * j]
                    self._yrange(p, block.min(), block.max())
        for p in self.imu_plots:
            p.setXRange(t0, t1, padding=0)

    @staticmethod
    def _yrange(plot, lo, hi):
        pad = max(0.1 * (hi - lo), 0.05)
        plot.setYRange(lo - pad, hi + pad, padding=0)
        height = max(plot.vb.height(), 40.0)
        span = hi - lo + 2 * pad
        step = nice_step(span, target=int(max(3, min(6, height // 30))))   # 3 to 6 labels, never fewer than 3
        while span / step < 2.5 and step > 1e-9:                          # always label both sides of zero
            step /= 2
        plot.getAxis("left").setTickSpacing(levels=[(step, 0)])

    def clear(self):
        for c in self.c_teng + self.imu_curves:
            c.setData([], [])
        self._gen = -1

"""Three TENG cards (Front (A0), Middle (A1), Back (A2)), each showing exactly two numbers:
the newest voltage and the peak-to-peak voltage over the shared sliding window. An asterisk follows the Vpp value
when that channel clipped inside the window. Plain QLabels, updated only when the text changes."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from config import settings
from config.channels import ChannelConfig
from gui import theme
from gui.widgets import StatusDot


class VppBar(QFrame):
    """The one shared selector for the Vpp window."""
    window_changed = Signal(float)

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 12, 20, 12)
        t = QLabel("TENG")
        t.setObjectName("cardTitle")
        cap = QLabel("Vpp window")
        cap.setObjectName("caption")
        self.win = QComboBox()
        for s in settings.VPP_WINDOWS_S:
            self.win.addItem(f"{int(round(s * 1000))} ms" if s < 1 else f"{s:g} s", s)
        self.win.setCurrentIndex(settings.VPP_WINDOWS_S.index(settings.DEFAULT_VPP_WINDOW_S))
        self.win.setToolTip("Vpp = Vmax minus Vmin over this sliding window, for every channel")
        row.addWidget(t)
        row.addStretch(1)
        row.addWidget(cap)
        row.addWidget(self.win)
        self.win.currentIndexChanged.connect(lambda: self.window_changed.emit(self.win.currentData()))


class ChannelCard(QFrame):
    def __init__(self, cfg: ChannelConfig):
        super().__init__()
        self.cfg = cfg
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 12, 20, 12)
        outer.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(StatusDot(cfg.color, 12))
        title = QLabel(cfg.label)                  # always "Front (A0)", "Middle (A1)", "Back (A2)"
        title.setStyleSheet("font-size:14px; font-weight:500;")
        head.addWidget(title)
        head.addStretch(1)
        outer.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(24)
        mono = f"font-family:'{theme.MONO}'; font-weight:500;"
        a = QVBoxLayout()
        a.setSpacing(0)
        ca = QLabel("Voltage (V)")
        ca.setObjectName("caption")
        self.now = QLabel("--")
        self.now.setStyleSheet(f"{mono} font-size:34px; color:{cfg.color};")
        a.addWidget(ca)
        a.addWidget(self.now)
        b = QVBoxLayout()
        b.setSpacing(0)
        cb = QLabel("Vpp (V)")
        cb.setObjectName("caption")
        self.vpp = QLabel("--")
        self.vpp.setStyleSheet(f"{mono} font-size:34px; color:{theme.TEXT};")
        b.addWidget(cb)
        b.addWidget(self.vpp)
        row.addLayout(a, 1)
        row.addLayout(b, 1)
        outer.addLayout(row)
        self.clip_banner = QLabel()
        self.clip_banner.setWordWrap(True)
        self.clip_banner.setStyleSheet("background:rgba(229,72,77,0.14); color:#ff8a8e;"
                                       "border:1px solid rgba(229,72,77,0.4); border-radius:8px; padding:6px 12px;")
        self.clip_banner.hide()
        outer.addWidget(self.clip_banner)
        self._now_txt = self._vpp_txt = "--"
        self._banner_on = False

    def show_clip(self, clipped: bool, range_v):
        """Per-channel clip banner, driven by this channel's status bit (8 / 16 / 32) from the last window."""
        if clipped != self._banner_on:
            self._banner_on = clipped
            self.clip_banner.setVisible(clipped)
        if clipped:
            self.clip_banner.setText(f"Input beyond ±{range_v:g} V: peaks are clipped, Vpp reads low"
                                     if range_v else "Input beyond the readable range: peaks are clipped, Vpp reads low")

    def show_values(self, v, vpp, clipped):
        now = "--" if v is None else f"{v:+.2f}"
        vp = "--" if vpp is None else f"{vpp:.2f}" + ("*" if clipped else "")
        if now != self._now_txt:                   # only touch the widget when the text really changes
            self._now_txt = now
            self.now.setText(now)
        if vp != self._vpp_txt:
            self._vpp_txt = vp
            self.vpp.setText(vp)

    def clear(self):
        self.show_values(None, None, False)
        self.show_clip(False, None)

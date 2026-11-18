"""TENG card: channel A3 only (ADS1115 A3, differential against the A0 bias reference)."""
from __future__ import annotations

from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout

from config import settings
from gui import theme
from gui.widgets import DotLabel
from sensors.teng_processor import (REF_NOT_CHECKED, REF_OK, REF_REST_OK, REF_TEXT, TengProcessor)


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


class TengPanel(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(f"TENG voltage, channel {settings.TENG_CHANNEL_LABEL}")
        title.setObjectName("cardTitle")
        self.status = DotLabel("--", theme.MUTED)
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.status)
        outer.addLayout(head)

        self.big = QLabel("--")
        self.big.setStyleSheet(f"font-family:'{theme.MONO}'; font-size:44px; font-weight:500; color:{theme.ACCENT};")
        outer.addWidget(self.big)

        g = QGridLayout()
        g.setHorizontalSpacing(24)
        g.setVerticalSpacing(8)
        self.v = {}
        items = (("min", f"Minimum ({settings.TENG_UNIT})"), ("max", f"Maximum ({settings.TENG_UNIT})"),
                 ("avg", f"Average ({settings.TENG_UNIT})"), ("sr", "Samples per second"))
        for i, (k, t) in enumerate(items):
            box, lab = _stat(t)
            self.v[k] = lab
            g.addLayout(box, i // 2, i % 2)
        outer.addLayout(g)
        self.ref = DotLabel("--", theme.MUTED)
        outer.addWidget(self.ref)

    def update_from(self, t: TengProcessor):
        if not t.ads_ok:
            self.status.set("ADS1115 not found", theme.RED, theme.RED)
        elif t.calibrating:
            self.status.set("Calibrating baseline, keep the TENG untouched", theme.ACCENT, theme.ACCENT)
        elif t.clipped:
            self.status.set(f"Input clipped beyond ±{t.full_scale_v:.0f} V", theme.RED, theme.RED)
        else:
            self.status.set("Reading", theme.GREEN, theme.TEXT2)

        if not t.ads_ok or t.calibrating or t.latest_v is None:
            self.big.setText("Calibrating" if t.calibrating else "--")
        else:
            self.big.setText(f"{t.latest_v:+.3f} {settings.TENG_UNIT}")

        if t.ref_code == REF_OK:
            self.ref.set("Reference A0 OK" + (f", {t.a0_v:.3f} V at start-up" if t.a0_v else ""),
                         theme.GREEN, theme.TEXT2)
        elif t.ref_code == REF_REST_OK:
            self.ref.set("Reference A0: rest level within 50 mV", theme.GREEN, theme.TEXT2)
        elif t.ref_code == REF_NOT_CHECKED:
            self.ref.set("Reference A0: " + REF_TEXT[t.ref_code].lower(), theme.MUTED, theme.MUTED)
        else:
            self.ref.set(REF_TEXT[t.ref_code], theme.ACCENT, theme.ACCENT)

        s = t.stats
        self.v["min"].setText("--" if s.count == 0 else f"{s.min:+.3f}")
        self.v["max"].setText("--" if s.count == 0 else f"{s.max:+.3f}")
        self.v["avg"].setText("--" if s.count == 0 else f"{s.mean:+.3f}")
        self.v["sr"].setText(f"{t.sr:.0f}")

    def clear(self):
        self.big.setText("--")
        for v in self.v.values():
            v.setText("--")
        self.status.set("--", theme.MUTED)
        self.ref.set("--", theme.MUTED)

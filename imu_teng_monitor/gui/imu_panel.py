"""BNO08x readings as one aligned table. Only values the firmware actually transmits are shown."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout

from communication.data_parser import Packet
from gui import theme

STAB = ["unknown", "on table", "stationary", "stable", "motion"]   # firmware page's STAB[]


def _hline():
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background:{theme.BORDER}; border:none;")
    return f


class ImuPanel(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)
        title = QLabel("Motion, BNO08x")
        title.setObjectName("cardTitle")
        outer.addWidget(title)

        self._rows = {}          # key -> (labels[], decimals)

        def section(headers, rows):
            g = QGridLayout()
            g.setHorizontalSpacing(12)
            g.setVerticalSpacing(4)
            g.setColumnStretch(0, 1)
            for c, h in enumerate(headers, start=1):
                lab = QLabel(h)
                lab.setObjectName("caption")
                lab.setAlignment(Qt.AlignRight)
                g.addWidget(lab, 0, c)
            for r, (key, name, unit, decimals) in enumerate(rows, start=1):
                n = QLabel(f'{name} <span style="color:{theme.MUTED}; font-size:12px;">{unit}</span>')
                n.setMinimumHeight(28)
                g.addWidget(n, r, 0)
                vals = []
                for c in range(len(headers)):
                    v = QLabel("--")
                    v.setObjectName("value")
                    v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    v.setMinimumWidth(68)
                    g.addWidget(v, r, c + 1)
                    vals.append(v)
                self._rows[key] = (vals, decimals)
            outer.addLayout(g)

        section(("X", "Y", "Z"), [("acc", "Accelerometer", "m/s²", 3), ("gyro", "Gyroscope", "rad/s", 4),
                                  ("mag", "Magnetometer", "µT", 2), ("lin", "Linear acceleration", "m/s²", 3),
                                  ("grav", "Gravity", "m/s²", 3)])
        outer.addWidget(_hline())
        section(("Roll", "Pitch", "Yaw"), [("euler", "Euler angles", "deg", 1)])
        outer.addWidget(_hline())
        section(("W", "X", "Y", "Z"), [("quat", "Quaternion", "", 4)])
        outer.addWidget(_hline())

        g = QGridLayout()
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(0)
        self.st = {}
        for c, (key, title) in enumerate((("imu", "Sensor"), ("acc", "Quat accuracy (rad)"),
                                          ("stab", "Stability"), ("steps", "Steps"))):
            t = QLabel(title)
            t.setObjectName("caption")
            v = QLabel("--")
            v.setObjectName("value")
            g.addWidget(t, 0, c)
            g.addWidget(v, 1, c)
            self.st[key] = v
        outer.addLayout(g)
        outer.addStretch(1)

    def _set(self, key, values):
        labs, d = self._rows[key]
        for lab, x in zip(labs, values):
            lab.setText(f"{x:+.{d}f}")

    def update_packet(self, p: Packet):
        self._set("acc", p.acc)
        self._set("gyro", p.gyro)
        self._set("mag", p.mag)
        self._set("lin", p.lin)
        self._set("grav", p.grav)
        self._set("euler", p.rpy)
        self._set("quat", p.quat)
        s = self.st
        s["imu"].setText("OK" if p.imu_ok else "Not found")
        s["imu"].setStyleSheet(f"color:{theme.GREEN if p.imu_ok else theme.RED};")
        s["acc"].setText(f"{p.quat_acc:.3f}")
        s["stab"].setText(STAB[p.stab] if 0 <= p.stab < len(STAB) else str(p.stab))
        s["steps"].setText(str(p.steps))

    def clear(self):
        for labs, _ in self._rows.values():
            for lab in labs:
                lab.setText("--")
        for v in self.st.values():
            v.setText("--")
            v.setStyleSheet("")

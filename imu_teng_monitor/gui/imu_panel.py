"""IMU numbers: accelerometer, gyroscope, Euler angles, plus the IMU status bit."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout

from gui import theme


class ImuPanel(QFrame):
    ROWS = (("Accelerometer", "m/s²", 3, 3), ("Gyroscope", "rad/s", 4, 3), ("Euler angles", "deg", 1, 3))

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 12, 20, 12)
        outer.setSpacing(8)
        title = QLabel("Motion")
        title.setObjectName("cardTitle")
        outer.addWidget(title)
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(4)
        g.setColumnStretch(0, 1)
        for c, h in enumerate(("X / Roll", "Y / Pitch", "Z / Yaw"), start=1):
            lab = QLabel(h)
            lab.setObjectName("caption")
            lab.setAlignment(Qt.AlignRight)
            g.addWidget(lab, 0, c)
        self._vals = []
        for r, (name, unit, dec, n) in enumerate(self.ROWS, start=1):
            lab = QLabel(f'{name} <span style="color:{theme.MUTED}; font-size:12px;">{unit}</span>')
            lab.setMinimumHeight(28)
            g.addWidget(lab, r, 0)
            row = []
            for c in range(n):
                v = QLabel("--")
                v.setObjectName("value")
                v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                v.setMinimumWidth(68)
                g.addWidget(v, r, c + 1)
                row.append(v)
            self._vals.append((row, dec))
        outer.addLayout(g)
        st = QGridLayout()
        self.status = QLabel("--")
        self.status.setObjectName("value")
        cap = QLabel("Sensor")
        cap.setObjectName("caption")
        st.addWidget(cap, 0, 0)
        st.addWidget(self.status, 1, 0)
        outer.addLayout(st)
        self._last = None

    def show_values(self, imu):
        """imu = (roll, pitch, yaw, ax, ay, az, gx, gy, gz, imu_ok) or None."""
        if imu is None or imu == self._last:
            return
        self._last = imu
        roll, pitch, yaw, ax, ay, az, gx, gy, gz, ok = imu
        for (row, dec), vals in zip(self._vals, ((ax, ay, az), (gx, gy, gz), (roll, pitch, yaw))):
            for lab, x in zip(row, vals):
                lab.setText(f"{x:+.{dec}f}")
        self.status.setText("OK" if ok else "Not found")
        self.status.setStyleSheet(f"color:{theme.GREEN if ok else theme.RED};")

    def clear(self):
        self._last = None
        for row, _ in self._vals:
            for lab in row:
                lab.setText("--")
        self.status.setText("--")
        self.status.setStyleSheet("")

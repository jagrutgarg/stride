from __future__ import annotations

import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from config import settings
from gui import theme
from gui.widgets import AnimatedButton, StatusDot

STATE_STYLE = {
    "connected": (theme.GREEN, "Connected"),
    "disconnected": (theme.RED, "Disconnected"),
    "connecting": (theme.ACCENT, "Connecting"),
}


def _info(title: str):
    box = QVBoxLayout()
    box.setSpacing(0)
    t = QLabel(title)
    t.setObjectName("caption")
    v = QLabel("--")
    v.setObjectName("value")
    box.addWidget(t)
    box.addWidget(v)
    return box, v


class ConnectionPanel(QFrame):
    connect_clicked = Signal()
    disconnect_clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 12, 20, 12)
        lay.setSpacing(12)

        mark = QLabel("StrideAssist")
        mark.setObjectName("wordmark")
        sub = QLabel("TENG and motion monitor")
        sub.setObjectName("caption")
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(mark)
        brand.addWidget(sub)
        lay.addLayout(brand)
        lay.addSpacing(16)

        self.mode = QComboBox()
        self.mode.addItems(["Live ESP32", "Simulation"])
        self.mode.setToolTip("Simulation uses a built-in fake ESP32 on this PC, not hardware data")
        self.ip = QLineEdit(settings.DEFAULT_IP)
        self.ip.setPlaceholderText("ESP32 IP, for example 192.168.1.50")
        self.ip.setMinimumWidth(192)
        self.btn_connect = AnimatedButton("Connect", "primary")
        self.btn_disconnect = AnimatedButton("Disconnect")
        self.btn_disconnect.setEnabled(False)

        self.dot = StatusDot(theme.RED, 20)
        self.status = QLabel()
        self.status.setStyleSheet("font-size:14px; font-weight:500;")

        lay.addWidget(self.mode)
        lay.addWidget(self.ip)
        lay.addWidget(self.btn_connect)
        lay.addWidget(self.btn_disconnect)
        lay.addSpacing(8)
        lay.addWidget(self.dot)
        lay.addWidget(self.status)
        lay.addStretch(1)

        self._vals = {}
        for key, title in (("last", "Last data"), ("hz", "Frame rate")):
            box, v = _info(title)
            self._vals[key] = v
            w = QWidget()
            w.setLayout(box)
            w.setMinimumWidth(96)
            lay.addWidget(w)

        self.btn_connect.clicked.connect(self.connect_clicked)
        self.btn_disconnect.clicked.connect(self.disconnect_clicked)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._state = "disconnected"
        self.set_state("disconnected")

    # -- API ---------------------------------------------------------------
    @property
    def simulation(self) -> bool:
        return self.mode.currentIndex() == 1

    def _mode_changed(self):
        sim = self.simulation
        self.ip.setEnabled(not sim)
        if sim:
            self._saved_ip = self.ip.text()
            self.ip.setText(f"{settings.SIM_HOST}:{settings.SIM_PORT}")
        elif hasattr(self, "_saved_ip"):
            self.ip.setText(self._saved_ip)

    def set_state(self, state: str, target: str = ""):
        self._state = state
        color, text = STATE_STYLE[state]
        self.dot.set_state(color, blink=(state == "connecting"))
        self.status.setText(text)
        busy = state != "disconnected"
        self.btn_connect.setEnabled(not busy)
        self.btn_disconnect.setEnabled(busy)
        self.mode.setEnabled(not busy)
        self.ip.setEnabled(not busy and not self.simulation)

    def update_info(self, last_rx_wall, rate_hz):
        v = self._vals
        # the dot's ring pulses only while frames are really arriving
        if self._state == "connected":
            self.dot.set_state(theme.GREEN, active=rate_hz > 0)
        if last_rx_wall is None:
            v["last"].setText("--")
        else:
            lt = time.localtime(last_rx_wall)
            v["last"].setText(time.strftime("%H:%M:%S", lt) + f".{int(last_rx_wall * 1000) % 1000:03d}")
        v["hz"].setText(f"{rate_hz:.0f} Hz")

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout

from gui import theme
from gui.widgets import AnimatedButton, DotLabel


class ControlPanel(QFrame):
    pause_toggled = Signal(bool)
    reset_clicked = Signal()
    restart_clicked = Signal()
    save_clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 12, 20, 12)
        lay.setSpacing(8)

        self.btn_pause = AnimatedButton("Pause")
        self.btn_pause.setCheckable(True)
        self.btn_reset = AnimatedButton("Reset")
        self.btn_restart = AnimatedButton("Restart")
        self.btn_save = AnimatedButton("Save", "primary")
        self.btn_pause.setToolTip("Freeze the display. Data keeps being received and recorded")
        self.btn_reset.setToolTip("Clear graphs, statistics and log. Recorded data is kept")
        self.btn_restart.setToolTip("Start a new session (asks before discarding unsaved data) and reconnect")
        self.btn_save.setToolTip("Write the whole session to an Excel file")
        for b in (self.btn_pause, self.btn_reset, self.btn_restart, self.btn_save):
            lay.addWidget(b)

        self.display_badge = DotLabel()
        self.saved_badge = DotLabel()
        self.sim_badge = DotLabel("Simulation: data is not from hardware", theme.ACCENT, theme.ACCENT)
        self.sim_badge.setObjectName("simchip")
        self.sim_badge.setAttribute(Qt.WA_StyledBackground, True)
        self.sim_badge.setStyleSheet("QWidget#simchip{border:1px solid rgba(230,180,80,0.4); border-radius:8px;}")
        self.sim_badge.layout().setContentsMargins(12, 4, 12, 4)
        self.sim_badge.hide()
        lay.addSpacing(16)
        lay.addWidget(self.display_badge)
        lay.addStretch(1)
        lay.addWidget(self.sim_badge)
        lay.addSpacing(8)
        lay.addWidget(self.saved_badge)

        self.btn_pause.toggled.connect(self._toggled)
        self.btn_reset.clicked.connect(self.reset_clicked)
        self.btn_restart.clicked.connect(self.restart_clicked)
        self.btn_save.clicked.connect(self.save_clicked)
        self.set_paused(False)
        self.set_saved(True, 0, 0)

    def _toggled(self, on):
        self.btn_pause.setText("Resume" if on else "Pause")
        self.set_paused(on)
        self.pause_toggled.emit(on)

    def set_paused(self, paused: bool, buffered: int = 0):
        if paused:
            self.display_badge.set(f"Display paused, {buffered:,} TENG samples buffered", theme.ACCENT, theme.ACCENT)
        else:
            self.display_badge.set("Display live", theme.GREEN, theme.TEXT2)

    def set_saved(self, saved: bool, imu_n: int, teng_n: int):
        if saved:
            self.saved_badge.set("All data saved" if imu_n == teng_n == 0 else "Saved", theme.GREEN, theme.TEXT2)
        else:
            self.saved_badge.set(f"Unsaved: {imu_n:,} IMU frames, {teng_n:,} TENG samples", theme.ACCENT, theme.TEXT)

from __future__ import annotations

import html
import time

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QTextEdit, QVBoxLayout

from gui import theme
from gui.widgets import AnimatedButton

LEVEL_COLOR = {"INFO": theme.TEXT2, "OK": theme.GREEN, "WARN": theme.ACCENT, "ERROR": theme.RED}


class LogPanel(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 12, 20, 16)
        lay.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("Event log")
        t.setObjectName("cardTitle")
        self.btn_clear = AnimatedButton("Clear logs")
        self.btn_clear.setFixedHeight(32)
        self.btn_clear.setToolTip("Clears this panel only. Sensor data is not affected")
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(self.btn_clear)
        lay.addLayout(head)
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.document().setMaximumBlockCount(2000)   # visible log only; data is never trimmed
        lay.addWidget(self.view)
        self.btn_clear.clicked.connect(self.clear)

    def add(self, level: str, msg: str, wall: float | None = None):
        wall = time.time() if wall is None else wall
        ts = time.strftime("%H:%M:%S", time.localtime(wall)) + f".{int(wall * 1000) % 1000:03d}"
        c = LEVEL_COLOR.get(level, theme.TEXT)
        tag = {"INFO": "info", "OK": "ok", "WARN": "warn", "ERROR": "error"}.get(level, level.lower())
        self.view.append(f'<span style="color:{theme.MUTED}">{ts}</span>&nbsp;&nbsp;'
                         f'<span style="color:{c}">{tag}&nbsp;&nbsp;{html.escape(msg)}</span>')

    def clear(self):
        self.view.clear()

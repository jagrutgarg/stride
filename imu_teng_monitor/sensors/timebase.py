"""Time axis from the ESP32 clock only (the frame arrival time is never used for sample times).

A frame carries the ESP32 time `t` (ms) of its newest sample, the sample period `cp` (us) and n samples per channel.
Sample k happened at   t - (n-1-k) * cp / 1000   ms.
Session time is that device time minus the device time of the first frame, so t = 0 is the first sample batch.
If the ESP32 restarts (its clock goes backwards) the axis continues from the last time instead of jumping back.
"""
from __future__ import annotations

import numpy as np


class Timebase:
    def __init__(self):
        self.origin_ms: float | None = None
        self.shift_s = 0.0               # added after a device restart so the axis keeps increasing
        self.last_t_ms: float | None = None
        self.last_rel = -1e18

    def frame(self, t_ms: float) -> float:
        """Session time (s) of a frame's newest sample. Returns it and notes whether the device restarted."""
        if self.origin_ms is None:
            self.origin_ms = t_ms
        elif self.last_t_ms is not None and t_ms < self.last_t_ms:          # device restarted
            self.shift_s = self.last_rel + 1e-3 - (t_ms - self.origin_ms) / 1000.0
        self.last_t_ms = t_ms
        rel = (t_ms - self.origin_ms) / 1000.0 + self.shift_s
        self.last_rel = max(self.last_rel, rel)
        return rel

    @staticmethod
    def samples(t_rel: float, n: int, cp_us: float) -> np.ndarray:
        """Times (s) of the n samples of a frame: t - (n-1-k) * cp, oldest to newest."""
        return t_rel - (n - 1 - np.arange(n)) * (cp_us / 1e6)

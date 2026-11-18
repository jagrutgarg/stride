"""Maps the ESP32 millis() clock onto the session time axis.

Each frame carries the ESP32 time `t` and N TENG samples taken since the previous
frame. Sample times are spread evenly between the previous and current frame time
(monotonic, jitter-free). After a gap (reconnect) or an ESP32 reboot (t going
backwards) the mapping is re-anchored to the PC arrival time.
"""
from __future__ import annotations

import numpy as np

from config import settings

CONTINUOUS_MAX_GAP_S = 0.25


class Timebase:
    def __init__(self, perf0: float):
        self.perf0 = perf0
        self.reset_stream()
        self.last_rel = -1e18

    def reset_stream(self):
        self.offset: float | None = None
        self.prev_dev_s: float | None = None

    def map(self, t_ms: float, rx_perf: float, n_samples: int, sr: float):
        """Return (packet_rel_time, restarted, sample_rel_times[n], sample_dev_ms[n])."""
        dev_s = t_ms / 1000.0
        restarted = False
        if self.prev_dev_s is not None and dev_s < self.prev_dev_s:
            restarted = True
            self.reset_stream()
        rx_rel = rx_perf - self.perf0
        if self.offset is None:
            self.offset = rx_rel - dev_s
            if self.offset + dev_s <= self.last_rel:       # keep time axis monotonic
                self.offset = self.last_rel + 1e-3 - dev_s
        t_rel = dev_s + self.offset

        if n_samples:
            if self.prev_dev_s is not None and 0 < dev_s - self.prev_dev_s <= CONTINUOUS_MAX_GAP_S:
                frac = np.arange(1, n_samples + 1) / n_samples
                times = (self.prev_dev_s + frac * (dev_s - self.prev_dev_s)) + self.offset
            else:
                dt = 1.0 / sr if sr > 0 else settings.ADS_FALLBACK_PERIOD_S
                times = t_rel - (n_samples - 1 - np.arange(n_samples)) * dt
                if times[0] <= self.last_rel:
                    times = np.maximum(times, self.last_rel + 1e-6 * (1 + np.arange(n_samples)))
        else:
            times = np.empty(0)
        self.prev_dev_s = dev_s
        self.last_rel = max(self.last_rel, t_rel, float(times[-1]) if n_samples else t_rel)
        return t_rel, restarted, times, (times - self.offset) * 1000.0

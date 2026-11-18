"""Oscilloscope-style measurements computed in the GUI process from the raw v[] samples.

Nothing here uses the firmware's pp/mx/mn/hpp fields.

Window statistics (sliding window of the last `window_s` seconds of valid samples):
    Vpp = max - min, Vmax, Vmin, mean, AC RMS = sqrt(mean((v - mean)^2))

Single-shot step capture (state machine over the sample stream):
    trigger  : |v| > threshold
    capture  : from 50 ms before the trigger until |v| <= threshold for 150 ms
    per step : Vpp, positive peak, negative peak, pulse width at 50 %, 10-90 % rise time

Time inside a capture is sample index * dt with dt = 1/sr (samples are evenly spaced, as the
firmware's sr is the real sample rate). Width and rise time therefore have a resolution of
one sample period and pulses shorter than that can be missed.

Pulse width / rise time are measured on the dominant lobe (the larger |peak|):
    s = sign(peak) * v, p = |peak|
    width@50 % = time between the last crossing of 0.5 p before the peak and the first crossing after it
    rise 10-90 % = time between the crossings of 0.1 p and 0.9 p on the rising edge before the peak
    (linear interpolation between samples; None if the capture starts already above that level)
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from config import settings
from sensors.rolling_buffer import RollingBuffer

WINDOWS_S = (0.1, 0.5, 1.0, 5.0)
DEFAULT_WINDOW_S = 1.0
DEFAULT_THRESHOLD_V = 1.0
PRE_TRIGGER_S = 0.050
QUIET_END_S = 0.150
MAX_CAPTURE_S = 2.0


@dataclass
class WindowStats:
    vpp: float
    vmax: float
    vmin: float
    mean: float
    ac_rms: float
    n: int
    t_start: float
    t_end: float


@dataclass
class StepRecord:
    index: int
    t_rel: float                 # session time of the trigger sample
    vpp: float
    pos_peak: float
    neg_peak: float
    width_ms: Optional[float]
    rise_ms: Optional[float]
    n_samples: int
    clipped: bool
    truncated: bool
    segment: int


def _cross_left(s: np.ndarray, ip: int, level: float) -> Optional[float]:
    """Fractional index where s rises through `level` on the way to the peak at ip."""
    i = ip
    while i > 0 and s[i - 1] >= level:
        i -= 1
    if i == 0:
        return None                       # capture starts above the level
    a, b = s[i - 1], s[i]
    return (i - 1) + (level - a) / (b - a)


def _cross_right(s: np.ndarray, ip: int, level: float) -> Optional[float]:
    j = ip
    n = len(s)
    while j < n - 1 and s[j + 1] >= level:
        j += 1
    if j == n - 1:
        return None                       # capture ends above the level
    a, b = s[j], s[j + 1]
    return j + (a - level) / (a - b)


def measure_step(x: np.ndarray, dt: float):
    """Return (vpp, pos_peak, neg_peak, width_ms|None, rise_ms|None) for one captured step."""
    pos, neg = float(x.max()), float(x.min())
    ip = int(np.argmax(np.abs(x)))
    peak = float(x[ip])
    s = x if peak >= 0 else -x
    p = abs(peak)
    width = rise = None
    l50, r50 = _cross_left(s, ip, 0.5 * p), _cross_right(s, ip, 0.5 * p)
    if l50 is not None and r50 is not None:
        width = (r50 - l50) * dt * 1000.0
    l90, l10 = _cross_left(s, ip, 0.9 * p), _cross_left(s, ip, 0.1 * p)
    if l90 is not None and l10 is not None:
        rise = (l90 - l10) * dt * 1000.0
    return pos - neg, pos, neg, width, rise


class ScopeProcessor:
    def __init__(self):
        self.window_s = DEFAULT_WINDOW_S
        self.threshold = DEFAULT_THRESHOLD_V
        self.total_steps = 0
        self.new_session()

    # -- lifecycle -------------------------------------------------------
    def new_session(self):
        self.total_steps = 0
        self.reset_display()

    def reset_display(self):
        self.latest: Optional[WindowStats] = None
        self.hold_vpp: Optional[float] = None
        self.steps: List[StepRecord] = []          # display list (newest last)
        self.version = 0                           # bumps when `steps` changes
        self._hist: deque = deque(maxlen=4000)
        self._cap: Optional[list] = None
        self._quiet = 0
        self._cap_t = 0.0
        self._cap_clip = False
        self._cap_seg = 0
        self._dt = settings.ADS_FALLBACK_PERIOD_S

    def reset_hold(self):
        self.hold_vpp = None

    @property
    def capturing(self) -> bool:
        return self._cap is not None

    # -- window statistics -----------------------------------------------
    def update_window(self, buf: RollingBuffer):
        if buf.n == 0:
            self.latest = None
            return
        t_end = float(buf._buf[buf.n - 1, 0])
        w = buf.window(t_end - self.window_s)
        v = w[:, 1]
        if len(v) < 2:
            self.latest = None
            return
        mx, mn, mean = float(v.max()), float(v.min()), float(v.mean())
        ac = float(np.sqrt(np.mean((v - mean) ** 2)))
        self.latest = WindowStats(mx - mn, mx, mn, mean, ac, len(v), float(w[0, 0]), t_end)
        if self.hold_vpp is None or self.latest.vpp > self.hold_vpp:
            self.hold_vpp = self.latest.vpp

    # -- step capture ----------------------------------------------------
    def feed(self, v: np.ndarray, times: np.ndarray, sr: float, clipped: bool, segment: int) -> List[StepRecord]:
        """Feed valid (non-calibrating) samples in order. Returns steps completed in this call."""
        out: List[StepRecord] = []
        dt = 1.0 / sr if sr > 0 else settings.ADS_FALLBACK_PERIOD_S
        self._dt = dt
        pre_n = int(round(PRE_TRIGGER_S / dt))
        post_n = max(1, int(round(QUIET_END_S / dt)))
        max_n = int(MAX_CAPTURE_S / dt)
        thr = self.threshold
        for t, x in zip(times.tolist(), v.tolist()):
            if self._cap is None:
                if abs(x) > thr:
                    pre = list(self._hist)[-pre_n:] if pre_n > 0 else []
                    self._cap = pre + [x]
                    self._cap_t, self._quiet = t, 0
                    self._cap_clip, self._cap_seg = clipped, segment
            else:
                self._cap.append(x)
                self._cap_clip = self._cap_clip or clipped
                self._quiet = self._quiet + 1 if abs(x) <= thr else 0
                if self._quiet >= post_n:
                    out.append(self._finish(False))
                elif len(self._cap) >= max_n:
                    out.append(self._finish(True))
            self._hist.append(x)
        return out

    def on_gap(self) -> List[StepRecord]:
        """Data stream interrupted (reconnect / calibration): close any open capture."""
        out = []
        if self._cap is not None:
            out.append(self._finish(True))
        self._hist.clear()
        return out

    def _finish(self, truncated: bool) -> StepRecord:
        x = np.asarray(self._cap, dtype=np.float64)
        vpp, pos, neg, width, rise = measure_step(x, self._dt)
        self.total_steps += 1
        rec = StepRecord(self.total_steps, self._cap_t, vpp, pos, neg, width, rise, len(x),
                         self._cap_clip, truncated, self._cap_seg)
        self._cap = None
        self.steps.append(rec)
        if len(self.steps) > 10:
            del self.steps[:-10]
        self.version += 1
        return rec

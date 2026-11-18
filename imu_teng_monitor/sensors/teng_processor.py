"""TENG (ADS1115 channel A3) handling.

No conversion is done here: the calibrated value is the firmware's own `v[]`
(see settings.TENG_FORMULA_TEXT). The only derived number is the raw ADC code
when the firmware does not send `r[]`: it is the exact inverse of the firmware's
`d = raw * ADS_LSB_V * 1000` for the latest sample of each frame.
"""
from __future__ import annotations

import numpy as np

from config import settings
from communication.data_parser import Packet
from sensors.rolling_buffer import RollingBuffer
from storage.session import FLAG_CAL, FLAG_CLIP


# A0 reference status codes (stored per sample, shown in Excel)
REF_NOT_CHECKED, REF_OK, REF_A0_OUT_OF_RANGE, REF_REST_OFFSET, REF_REST_OK = 0, 1, 2, 3, 4
REF_TEXT = {
    REF_NOT_CHECKED: "Not checked",
    REF_OK: "OK",
    REF_A0_OUT_OF_RANGE: "WARNING: A0 reference outside 1.45-1.85 V",
    REF_REST_OFFSET: "WARNING: A3 rest level more than 50 mV from A0",
    REF_REST_OK: "A3 rest level within 50 mV of A0 (A0 voltage itself is not reported by the firmware)",
}


def reference_status(pkt: Packet) -> int:
    """Validate (never apply) the A0 reference. A0 is NOT subtracted here: the firmware's differential
    reading A0 - A3 already contains it, exactly once."""
    if pkt.a0_status == 2:
        return REF_A0_OUT_OF_RANGE
    if not pkt.calibrating and abs(pkt.baseline_mv) > settings.REST_OFFSET_WARN_MV:
        return REF_REST_OFFSET
    if pkt.a0_status is None:
        return REF_NOT_CHECKED if pkt.calibrating else REF_REST_OK
    return REF_OK


class RunningStats:
    def __init__(self):
        self.reset()

    def reset(self):
        self.count = 0
        self.total = 0.0
        self.min = float("nan")
        self.max = float("nan")

    def update(self, x: np.ndarray):
        if len(x) == 0:
            return
        lo, hi = float(x.min()), float(x.max())
        if self.count == 0:
            self.min, self.max = lo, hi
        else:
            self.min, self.max = min(self.min, lo), max(self.max, hi)
        self.count += len(x)
        self.total += float(x.sum())

    @property
    def mean(self):
        return self.total / self.count if self.count else float("nan")


class TengProcessor:
    def __init__(self):
        self.buffer = RollingBuffer(settings.TENG_BUFFER_CAPACITY, 2)   # t, V
        self.stats = RunningStats()
        self.reset_display()
        self._prev_cal = False

    def process(self, pkt: Packet, times: np.ndarray, dev_ms: np.ndarray, segment: int) -> np.ndarray:
        """Return storage rows (TENG_COLUMNS order) for the samples in this frame."""
        n = len(pkt.v)
        v = np.asarray(pkt.v, dtype=np.float64)
        cal = pkt.calibrating or self._prev_cal       # conservative at the calibration edge
        self._prev_cal = pkt.calibrating

        raw = np.full(n, np.nan)
        if pkt.raw is not None:
            raw[:] = pkt.raw
            self.raw_is_per_sample = True
        else:
            self.raw_is_per_sample = False
            if n:
                raw[-1] = round(pkt.diff_mv / 1000.0 / settings.ADS_LSB_V)
        # latest raw for the display card (works even if this frame had no samples)
        if n and not np.isnan(raw[-1]):
            self.latest_raw = int(raw[-1])
        elif not self.raw_is_per_sample:
            self.latest_raw = int(round(pkt.diff_mv / 1000.0 / settings.ADS_LSB_V))

        flags = np.full(n, FLAG_CLIP if pkt.clipped else 0, dtype=np.float64)
        if cal:
            flags += FLAG_CAL

        self.ads_ok, self.calibrating, self.clipped = pkt.ads_ok, cal, pkt.clipped
        self.ref_code = reference_status(pkt)
        self.a0_v = pkt.a0_v
        self.full_scale_v = pkt.full_scale_v
        self.baseline_mv = pkt.baseline_mv
        self.sr = pkt.sr

        if n and not cal:
            self.stats.update(v)
            self.latest_v = float(v[-1])
            self.buffer.append(np.column_stack([times, v]))
        elif cal:
            self.latest_v = None

        return (np.column_stack([times, dev_ms, np.full(n, segment), raw, v, flags, np.full(n, self.ref_code)])
                if n else np.empty((0, 7)))

    def reset_display(self):
        self.buffer.clear()
        self.stats.reset()
        self.latest_v: float | None = None
        self.latest_raw: int | None = None
        self.ads_ok = True
        self.ref_code = REF_NOT_CHECKED
        self.a0_v = None
        self.calibrating = False
        self.clipped = False
        self.full_scale_v = 0.0
        self.baseline_mv = 0.0
        self.sr = 0.0
        self.raw_is_per_sample = False

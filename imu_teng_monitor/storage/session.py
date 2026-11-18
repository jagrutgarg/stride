"""In-memory store of everything received in the current session.

Nothing here is ever trimmed: Reset / Pause only affect what is *displayed*.
Data is released only by `clear()` (after the user saved or confirmed discard).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

IMU_COLUMNS = [
    ("t_rel", "Time (s)"), ("device_ms", "ESP32 millis (ms)"), ("seq", "Frame #"),
    ("segment", "Display segment"),
    ("roll", "Roll (deg)"), ("pitch", "Pitch (deg)"), ("yaw", "Yaw (deg)"),
    ("qw", "Quat W"), ("qx", "Quat X"), ("qy", "Quat Y"), ("qz", "Quat Z"),
    ("qacc", "Quat accuracy (rad)"),
    ("ax", "Accel X (m/s²)"), ("ay", "Accel Y (m/s²)"), ("az", "Accel Z (m/s²)"),
    ("gx", "Gyro X (rad/s)"), ("gy", "Gyro Y (rad/s)"), ("gz", "Gyro Z (rad/s)"),
    ("lx", "Linear accel X (m/s²)"), ("ly", "Linear accel Y (m/s²)"), ("lz", "Linear accel Z (m/s²)"),
    ("grx", "Gravity X (m/s²)"), ("gry", "Gravity Y (m/s²)"), ("grz", "Gravity Z (m/s²)"),
    ("mgx", "Mag X (µT)"), ("mgy", "Mag Y (µT)"), ("mgz", "Mag Z (µT)"),
    ("steps", "Steps"), ("stab", "Stability class"), ("imu_ok", "BNO08x OK"),
    ("win_vpp", "Scope window Vpp (V)"), ("step_vpp", "Last step Vpp (V)"),
]
TENG_COLUMNS = [
    ("t_rel", "Time (s)"), ("device_ms", "ESP32 millis (ms)"), ("segment", "Display segment"),
    ("raw", "A3 raw ADC (A0-A3 code, int16)"), ("v", "TENG calculated (V)"),
    ("flags", "Flags (1=calibrating, 2=clipped)"),
    ("ref", "A0 reference status code"),
]
IMU_IDX = {k: i for i, (k, _) in enumerate(IMU_COLUMNS)}
TENG_IDX = {k: i for i, (k, _) in enumerate(TENG_COLUMNS)}
FLAG_CAL, FLAG_CLIP = 1, 2


class GrowArray:
    """Append-only 2-D float64 array with amortised O(1) growth."""

    def __init__(self, ncols: int, initial: int = 4096):
        self.ncols = ncols
        self._buf = np.empty((initial, ncols), dtype=np.float64)
        self.n = 0

    def append(self, rows: np.ndarray):
        rows = np.atleast_2d(rows)
        k = len(rows)
        if k == 0:
            return
        need = self.n + k
        if need > len(self._buf):
            new = np.empty((max(need, len(self._buf) * 2), self.ncols), dtype=np.float64)
            new[: self.n] = self._buf[: self.n]
            self._buf = new
        self._buf[self.n:need] = rows
        self.n = need

    @property
    def data(self) -> np.ndarray:
        return self._buf[: self.n]

    def clear(self):
        self.n = 0


@dataclass
class ConnectionInfo:
    mode: str = "Live"            # "Live" or "SIMULATION"
    host: str = ""
    port: int = 0
    url: str = ""
    connect_count: int = 0        # successful connections
    disconnect_count: int = 0
    frames_ok: int = 0
    frames_invalid: int = 0
    frames_duplicate: int = 0
    frames_missed: int = 0        # detected via seq gaps (new firmware only)
    firmware_has_raw: bool | None = None
    firmware_has_seq: bool | None = None
    total_connected_s: float = 0.0


class SessionStore:
    def __init__(self):
        self.imu = GrowArray(len(IMU_COLUMNS))
        self.teng = GrowArray(len(TENG_COLUMNS))
        self.conn = ConnectionInfo()
        self.segment = 0
        self.reset_session()

    def reset_session(self):
        """Start a brand-new empty session (caller must have saved / confirmed)."""
        self.imu.clear()
        self.teng.clear()
        self.segment = 0
        self.steps = []                    # every captured StepRecord of the session
        self.start_wall = time.time()      # session object created
        self.perf0 = time.perf_counter()   # t_rel = 0 here
        self.saved_imu_n = 0
        self.saved_teng_n = 0
        self.conn = ConnectionInfo(mode=self.conn.mode, host=self.conn.host,
                                   port=self.conn.port, url=self.conn.url)

    def next_segment(self):
        self.segment += 1

    # -- bookkeeping ----------------------------------------------------
    @property
    def has_unsaved(self) -> bool:
        return self.imu.n != self.saved_imu_n or self.teng.n != self.saved_teng_n

    @property
    def unsaved_counts(self):
        return self.imu.n - self.saved_imu_n, self.teng.n - self.saved_teng_n

    def mark_saved(self, imu_n: int, teng_n: int):
        self.saved_imu_n, self.saved_teng_n = imu_n, teng_n

    @property
    def has_data(self) -> bool:
        return self.imu.n > 0 or self.teng.n > 0

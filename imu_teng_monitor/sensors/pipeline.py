"""Everything that happens to a version 2 frame after parsing. Runs in the RECEIVE thread; the UI thread only reads.

Per TENG channel (Front A0 / Middle A1 / Back A2):
  GUI pass-through factor (config/channels.py, identity: the firmware already calibrates) -> sample times from the
  ESP32 clock (Timebase) -> preallocated ring buffer (t, V) -> monotonic-deque sliding window for Vpp -> store rows.
IMU: preallocated ring buffer of acc / gyro / Euler per frame + the latest numbers for the text readout.

Calibration is applied in exactly one place, `cs.cfg.apply(...)` below.

Thread safety: the writer (receive thread) holds `lock` while it mutates ring buffers and window state; the UI thread
copies what it needs under the same lock (`view_teng`, `view_imu`, `readout`). The lock is held for microseconds.
"""
from __future__ import annotations

import threading

import numpy as np

from config import settings
from config.channels import CHANNELS, ChannelConfig
from communication.data_parser import Info, Packet
from sensors.rolling_buffer import RollingBuffer
from sensors.sliding_minmax import SlidingMinMax
from sensors.timebase import Timebase
from storage.session import FLAG_CAL, FLAG_CLIP, SessionStore

STALL_MS = 300.0     # ESP32 frame gap that means its sampling loop was blocked
IMU_COLS = 10        # t, ax ay az, gx gy gz, roll pitch yaw


class ChannelState:
    __slots__ = ("index", "cfg", "ring", "win", "latest_v", "vpp", "last_clip_t", "t_last")

    def __init__(self, index: int, cfg: ChannelConfig, window_s: float):
        self.index, self.cfg = index, cfg
        self.ring = RollingBuffer(settings.TENG_BUFFER_CAPACITY, 2)     # (t, V), preallocated
        self.win = SlidingMinMax(window_s)
        self.latest_v: float | None = None
        self.vpp: float | None = None
        self.last_clip_t = -1e18
        self.t_last = -1e18

    def clear_display(self):
        self.ring.clear()
        self.win.clear()
        self.latest_v = self.vpp = None
        self.last_clip_t = -1e18

    @property
    def clipped(self) -> bool:
        """True if this channel clipped inside the current Vpp window."""
        return self.t_last - self.last_clip_t <= self.win.window_s


class Pipeline:
    def __init__(self, store: SessionStore):
        self.store = store
        self.lock = threading.Lock()
        self.window_s = settings.DEFAULT_VPP_WINDOW_S
        self.channels: list[ChannelState] = [ChannelState(i, c, self.window_s) for i, c in enumerate(CHANNELS)]
        store.channel_labels = [c.cfg.label for c in self.channels]
        self.imu_ring = RollingBuffer(settings.IMU_BUFFER_CAPACITY, IMU_COLS)
        self.imu_latest = None                  # (roll, pitch, yaw, ax, ay, az, gx, gy, gz, imu_ok)
        self.timebase = Timebase()
        self.info: dict | None = None           # the {"info": ...} message, kept for the export
        self.range_v: float | None = None
        self.gen = 0                            # bumps with every frame: the UI redraws only when it changed
        self.frames_total = 0
        self.last_wall: float | None = None
        self.calibrating = False
        self._last_t_ms: float | None = None
        self._first = True                      # first frame of this connection: one-time checks

    # -- session lifecycle (UI thread) ----------------------------------
    def new_session(self):
        """After store.reset_session(): drop all state."""
        with self.lock:
            self.timebase = Timebase()
            self._last_t_ms = None
            self.channels = [ChannelState(i, c, self.window_s) for i, c in enumerate(CHANNELS)]
            self.store.channel_labels = [c.cfg.label for c in self.channels]
            self.last_wall = None
            self.frames_total = 0
            self.imu_ring.clear()
            self.imu_latest = None
            self.gen += 1

    def reset_display(self):
        with self.lock:
            for c in self.channels:
                c.clear_display()
            self.imu_ring.clear()
            self.imu_latest = None
            self.gen += 1

    def set_window(self, s: float):
        """Vpp window. Rebuilt from the ring buffers so the new span is covered immediately."""
        with self.lock:
            self.window_s = s
            for c in self.channels:
                w = c.ring.window(c.t_last - s)
                c.win.rebuild(w[:, 0], w[:, 1], s)
                c.vpp = c.win.vpp
            self.gen += 1

    def on_connected(self):
        with self.lock:
            self._last_t_ms = None
            self._first = True
            self.info = None

    def on_info(self, info: Info):
        """The first message after connecting: configuration, not a data frame."""
        with self.lock:
            self.info = info.raw
            if info.range_v is not None:
                self.range_v = info.range_v

    # -- main entry (receive thread) -------------------------------------
    def process(self, pkt: Packet):
        """Timestamp, buffer, measure and store one frame. Returns a list of (level, text) errors."""
        errors = []
        c = self.store.conn
        if self._last_t_ms is not None and pkt.t_ms == self._last_t_ms:
            c.frames_duplicate += 1
            return errors
        if self._last_t_ms is not None and pkt.t_ms - self._last_t_ms > STALL_MS:
            gap = pkt.t_ms - self._last_t_ms
            c.stalls += 1
            c.longest_stall_ms = max(c.longest_stall_ms, gap)
            errors.append(("ERROR", f"ESP32 stopped sending for {gap:.0f} ms (its loop was blocked, for example by a "
                                    "missing sensor): TENG samples in that gap were never taken"))
        self._last_t_ms = pkt.t_ms
        if self._first:                                             # once per connection, never per message
            self._first = False
            if not pkt.imu_ok:
                errors.append(("ERROR", "IMU not found (status bit 2 is clear): IMU values stay at zero"))
            if not pkt.ads_ok:
                errors.append(("ERROR", "ADS1115 not found (status bit 1 is clear): no TENG samples"))
            if any(cs.cfg.scale != 1.0 or cs.cfg.sign != 1 for cs in self.channels):
                errors.append(("WARN", "A GUI scale/sign other than 1.0/+1 is set in config/channels.py, but the "
                                       "firmware already sends calibrated volts: the correction is applied twice"))

        seg = self.store.segment
        cal = pkt.calibrating
        n = len(pkt.channels[0].v)
        teng_parts = []
        with self.lock:
            if self.timebase.origin_ms is None:
                self.store.start_wall = pkt.rx_wall          # wall-clock label of t = 0 (never used for sample times)
            t_rel = self.timebase.frame(pkt.t_ms)
            times = Timebase.samples(t_rel, n, pkt.cp_us)    # t - (n-1-k) * cp, from the ESP32 clock only
            for cs, cf in zip(self.channels, pkt.channels):
                v = cs.cfg.apply(cf.v)                       # the one place a GUI-side factor is applied
                if n:
                    flags = (FLAG_CAL if cal else 0) | (FLAG_CLIP if cf.clip else 0)
                    teng_parts.append(np.column_stack([times, np.full(n, cs.index), v, np.full(n, flags),
                                                       np.full(n, seg)]))
                    cs.t_last = float(times[-1])
                    if cf.clip:
                        cs.last_clip_t = cs.t_last
                    if not cal:                              # calibration samples are not shown
                        cs.ring.append(np.column_stack([times, v]))
                        push = cs.win.push
                        for t, x in zip(times.tolist(), v.tolist()):
                            push(t, x)
                        cs.latest_v = float(v[-1])
                        cs.vpp = cs.win.vpp
                if cal:
                    cs.latest_v = cs.vpp = None
            self.imu_ring.append(np.array([[t_rel, *pkt.acc, *pkt.gyro, *pkt.rpy]]))
            self.imu_latest = (*pkt.rpy, *pkt.acc, *pkt.gyro, pkt.imu_ok)
            self.calibrating = cal
            self.last_wall = pkt.rx_wall
            self.frames_total += 1
            self.gen += 1
        imu_row = np.array([[t_rel, seg, *pkt.rpy, *pkt.acc, *pkt.gyro, 1.0 if pkt.imu_ok else 0.0]])
        self.store.append(imu_row, np.vstack(teng_parts) if teng_parts else np.empty((0, 5)))
        c.frames_ok += 1
        return errors

    # -- readers (UI thread) -----------------------------------------------
    def view_teng(self, k: int, t_min: float):
        """Copy of channel k's (t, V) samples newer than t_min, plus the newest time. Taken under the lock."""
        with self.lock:
            cs = self.channels[k]
            return cs.ring.window(t_min).copy(), cs.t_last

    def view_imu(self, t_min: float):
        with self.lock:
            return self.imu_ring.window(t_min).copy()

    def readout(self):
        """Small snapshot for the text numbers: per channel (latest V, Vpp, clipped) and the IMU numbers."""
        with self.lock:
            return ([(c.latest_v, c.vpp, c.clipped) for c in self.channels], self.imu_latest,
                    self.calibrating, self.range_v)

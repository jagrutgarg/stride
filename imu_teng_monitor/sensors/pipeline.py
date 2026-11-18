"""Glue between parsed frames, processors and the session store (no Qt, fully testable)."""
from __future__ import annotations

import time
from collections import deque

import numpy as np

from config import settings
from communication.data_parser import Packet
from sensors.imu_processor import ImuProcessor
from sensors.scope_processor import ScopeProcessor
from sensors.teng_processor import TengProcessor
from sensors.timebase import Timebase
from storage.session import SessionStore


class Pipeline:
    def __init__(self, store: SessionStore):
        self.store = store
        self.imu = ImuProcessor()
        self.teng = TengProcessor()
        self.scope = ScopeProcessor()
        self.timebase = Timebase(store.perf0)
        self._last_key = None
        self._last_seq = None
        self._rx_times: deque = deque()
        self._mem_warned = False
        self.first_wall: float | None = None
        self.last_wall: float | None = None

    # -- session lifecycle ----------------------------------------------
    def new_session(self):
        """After store.reset_session(): drop all processor state."""
        self.timebase = Timebase(self.store.perf0)
        self._last_key = self._last_seq = None
        self._mem_warned = False
        self.first_wall = self.last_wall = None
        self.scope.new_session()
        self.reset_display()

    def reset_display(self):
        self.imu.reset_display()
        self.teng.reset_display()
        self.scope.reset_display()
        self._rx_times.clear()

    def on_connected(self):
        self.timebase.reset_stream()
        self._last_key = self._last_seq = None
        self.store.steps.extend(self.scope.on_gap())

    # -- main entry --------------------------------------------------------
    def process(self, pkt: Packet):
        """Store + process one frame. Returns (accepted, notes[(level, text)])."""
        notes = []
        c = self.store.conn
        key = pkt.seq if pkt.seq is not None else pkt.t_ms
        c.firmware_has_seq = pkt.seq is not None
        c.firmware_has_raw = pkt.raw is not None

        if self._last_key is not None and key == self._last_key:
            c.frames_duplicate += 1
            return False, [("WARN", f"Duplicate frame ignored (key {key:g})")]
        if self._last_key is not None and key < self._last_key:
            notes.append(("WARN", "ESP32 counter went backwards: device restarted"))
            self._last_seq = None
        if pkt.seq is not None and self._last_seq is not None and pkt.seq > self._last_seq + 1:
            missed = pkt.seq - self._last_seq - 1
            c.frames_missed += missed
            notes.append(("WARN", f"{missed} frame(s) missing before frame #{pkt.seq}"))
        self._last_key = key
        self._last_seq = pkt.seq

        t_rel, restarted, times, dev_ms = self.timebase.map(pkt.t_ms, pkt.rx_perf, len(pkt.v), pkt.sr)
        if restarted and not any("restarted" in n[1] for n in notes):
            notes.append(("WARN", "ESP32 clock restarted: re-anchored timestamps"))

        seg = self.store.segment
        self.store.teng.append(self.teng.process(pkt, times, dev_ms, seg))
        # scope: only valid (non-calibrating) samples; a calibration period closes any open capture
        if self.teng.calibrating:
            new_steps = self.scope.on_gap()
        elif len(pkt.v):
            new_steps = self.scope.feed(np.asarray(pkt.v), times, pkt.sr, pkt.clipped, seg)
        else:
            new_steps = []
        self.store.steps.extend(new_steps)
        self.scope.update_window(self.teng.buffer)
        win_vpp = self.scope.latest.vpp if self.scope.latest else float("nan")
        step_vpp = self.store.steps[-1].vpp if self.store.steps else float("nan")
        self.store.imu.append(self.imu.process(pkt, t_rel, seg, win_vpp, step_vpp))
        c.frames_ok += 1
        self._rx_times.append(pkt.rx_perf)
        if self.first_wall is None:
            self.first_wall = pkt.rx_wall
        self.last_wall = pkt.rx_wall

        if not self._mem_warned and self.store.teng.n >= settings.MEMORY_WARN_TENG_SAMPLES:
            self._mem_warned = True
            notes.append(("WARN", f"{self.store.teng.n:,} TENG samples held in memory: consider saving"))
        return True, notes

    # -- rate --------------------------------------------------------------
    def rx_rate_hz(self) -> float:
        now = time.perf_counter()
        q = self._rx_times
        while q and now - q[0] > 1.0:
            q.popleft()
        return float(len(q))

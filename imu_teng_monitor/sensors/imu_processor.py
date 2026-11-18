"""BNO08x frame handling: builds the storage row and the rolling plot buffer."""
from __future__ import annotations

import numpy as np

from config import settings
from communication.data_parser import Packet
from sensors.rolling_buffer import RollingBuffer
from storage.session import IMU_COLUMNS

# plot buffer columns
PLOT_COLS = ["t", "ax", "ay", "az", "gx", "gy", "gz", "roll", "pitch", "yaw"]


class ImuProcessor:
    def __init__(self):
        self.buffer = RollingBuffer(settings.IMU_BUFFER_CAPACITY, len(PLOT_COLS))
        self.latest: Packet | None = None

    def process(self, pkt: Packet, t_rel: float, segment: int,
                win_vpp: float = float('nan'), step_vpp: float = float('nan')) -> np.ndarray:
        """Return one storage row (IMU_COLUMNS order). Values are exactly as sent."""
        self.latest = pkt
        row = np.array([
            t_rel, pkt.t_ms, np.nan if pkt.seq is None else pkt.seq, segment,
            *pkt.rpy, *pkt.quat, pkt.quat_acc,
            *pkt.acc, *pkt.gyro, *pkt.lin, *pkt.grav, *pkt.mag,
            pkt.steps, pkt.stab, 1.0 if pkt.imu_ok else 0.0, win_vpp, step_vpp,
        ])
        assert len(row) == len(IMU_COLUMNS)
        self.buffer.append(np.array([[t_rel, *pkt.acc, *pkt.gyro, *pkt.rpy]]))
        return row

    def reset_display(self):
        self.buffer.clear()
        self.latest = None

"""Parse ESP32 "version 2" messages, once, in the receive thread (orjson if installed, else json).

Info message (the first message after connecting; configuration, NOT a data frame):
    {"info": {... scales ..., "range_V": ...}}

Data frame (about 50 per second, every value an integer):
    {"t":ms, "s":status, "cp":us, "r":[roll,pitch,yaw], "a":[x,y,z], "g":[x,y,z], "f":[...], "m":[...], "b":[...]}

    t   ESP32 millis() when the frame was sent            cp  sample period in microseconds
    r   Euler angles, value / 100  = degrees              a   accelerometer, value / 1000 = m/s^2
    g   gyroscope, value / 1000    = rad/s
    f / m / b   TENG Front (A0) / Middle (A1) / Back (A2), value / 1000 = volts, already calibrated by the firmware.
                All three have the same length n. Sample k happened at  t - (n-1-k) * cp / 1000  ms.
    s   status bit mask: 1 ADS ok, 2 IMU ok, 4 calibrating, 8 / 16 / 32 front / middle / back clipped in this frame

No other field is read. Anything from the old format (v, pp, mx, mn, hpp, hmx, hmn, d, b as a number, sr, q, qa, rpy,
lin, grav, mag, steps, stab, ok, iok, cal, clip, fs) is not used anywhere; such a frame is reported as invalid.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import List

import numpy as np

try:                                    # much faster than json; falls back silently
    import orjson
    _loads = orjson.loads
    JSON_LIB = "orjson"
except ImportError:                     # pragma: no cover
    _loads = json.loads
    JSON_LIB = "json"

# scale divisors of the version 2 format
DEG_DIV, ACC_DIV, GYRO_DIV, VOLT_DIV = 100.0, 1000.0, 1000.0, 1000.0
# status bits
ST_ADS_OK, ST_IMU_OK, ST_CALIBRATING = 1, 2, 4
ST_CLIP = (8, 16, 32)                   # front, middle, back
CHANNEL_IDS = ("A0", "A1", "A2")        # f, m, b


class PacketError(ValueError):
    """Raised for corrupted / incomplete / invalid frames."""


@dataclass(slots=True)
class ChannelFrame:
    id: str
    clip: bool
    v: np.ndarray                      # volts (float64), oldest to newest


@dataclass(slots=True)
class Packet:
    rx_wall: float                     # time.time() at reception (only used to label wall-clock time)
    t_ms: float
    status: int
    cp_us: float
    rpy: tuple                         # degrees
    acc: tuple                         # m/s^2
    gyro: tuple                        # rad/s
    channels: List[ChannelFrame] = field(default_factory=list)

    @property
    def ads_ok(self) -> bool:
        return bool(self.status & ST_ADS_OK)

    @property
    def imu_ok(self) -> bool:
        return bool(self.status & ST_IMU_OK)

    @property
    def calibrating(self) -> bool:
        return bool(self.status & ST_CALIBRATING)


@dataclass(slots=True)
class Info:
    """The {"info": {...}} message. `range_v` is the readable TENG range in volts if the firmware sent one."""
    raw: dict
    range_v: float | None


def _num(x, name):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise PacketError(f"field '{name}' is not a number: {x!r}")
    if not math.isfinite(x):
        raise PacketError(f"field '{name}' is not finite")
    return float(x)


def _vec3(d, key, div):
    a = d.get(key)
    if not isinstance(a, list) or len(a) != 3:
        raise PacketError(f"field '{key}' must be a list of 3 integers")
    return (_num(a[0], key) / div, _num(a[1], key) / div, _num(a[2], key) / div)


def _volts(x, name):
    if not isinstance(x, list):
        raise PacketError(f"field '{name}' must be a list")
    try:
        v = np.asarray(x, dtype=np.float64)
    except (TypeError, ValueError):
        raise PacketError(f"field '{name}' has a non-numeric entry") from None
    if v.ndim != 1 or not np.isfinite(v).all():
        raise PacketError(f"field '{name}' is not a flat list of finite numbers")
    return v / VOLT_DIV


def _range_v(info: dict):
    r = info.get("range_V")
    if isinstance(r, (int, float)) and not isinstance(r, bool) and math.isfinite(r):
        return float(r)
    if isinstance(r, list) and r and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in r):
        return float(max(abs(x) for x in r))
    return None


def parse_packet(text, rx_wall: float):
    """Return a Packet (data frame) or an Info (configuration message). Raises PacketError for anything else."""
    if not isinstance(text, (str, bytes)):
        raise PacketError("unexpected frame type")
    try:
        d = _loads(text)
    except Exception as e:  # json.JSONDecodeError / orjson.JSONDecodeError
        raise PacketError(f"JSON decode error: {str(e)[:60]}") from None
    if not isinstance(d, dict):
        raise PacketError("frame is not a JSON object")
    if "info" in d:
        info = d["info"]
        if not isinstance(info, dict):
            raise PacketError("'info' is not an object")
        return Info(raw=info, range_v=_range_v(info))
    for k in ("t", "s", "cp", "r", "a", "g", "f", "m", "b"):
        if k not in d:
            raise PacketError(f"not a version 2 frame: missing field '{k}'")
    status = _num(d["s"], "s")
    cp = _num(d["cp"], "cp")
    if status != int(status) or status < 0:
        raise PacketError("field 's' must be a non-negative integer")
    if cp <= 0:
        raise PacketError("field 'cp' must be positive")
    f, m, b = _volts(d["f"], "f"), _volts(d["m"], "m"), _volts(d["b"], "b")
    if not (len(f) == len(m) == len(b)):
        raise PacketError(f"f, m and b must have the same length (got {len(f)}, {len(m)}, {len(b)})")
    status = int(status)
    return Packet(
        rx_wall=rx_wall, t_ms=_num(d["t"], "t"), status=status, cp_us=cp,
        rpy=_vec3(d, "r", DEG_DIV), acc=_vec3(d, "a", ACC_DIV), gyro=_vec3(d, "g", GYRO_DIV),
        channels=[ChannelFrame(CHANNEL_IDS[i], bool(status & ST_CLIP[i]), v) for i, v in enumerate((f, m, b))])

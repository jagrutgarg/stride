"""Parse the JSON frames produced by the ESP32 firmware (buildJson()).

Frame fields (firmware names):
  t      millis() on the ESP32            ok     ADS1115 found     iok   BNO085 found
  cal    TENG baseline calibrating        clip   input clipped     fs    readable range [V]
  d      A0-A3 differential [mV]          b      baseline [mV]     sr    TENG samples/s
  q      [w,x,y,z] quaternion             qa     quaternion accuracy [rad]
  rpy    [roll,pitch,yaw] [deg]           a      accelerometer [m/s^2]
  g      gyroscope [rad/s]                lin    linear accel [m/s^2]
  grav   gravity [m/s^2]                  mag    magnetometer [uT]
  steps  step counter                     stab   stability class (0..4)
  v      list of calibrated TENG volts, one per ADS1115 sample since the last frame
Optional (added by firmware/stride_assist_gui, absent on the stock firmware):
  r      list of raw ADS1115 codes (int16), same length as v
  seq    frame counter
  a0, a0s  A0 reference voltage [V] measured by the firmware's start-up self-test and its verdict
         (0 = not measured, 1 = within 1.45..1.85 V, 2 = outside)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import List, Optional


class PacketError(ValueError):
    """Raised for corrupted / incomplete / invalid frames."""


@dataclass
class Packet:
    rx_wall: float                 # time.time() at reception
    rx_perf: float                 # time.perf_counter() at reception
    t_ms: float
    ads_ok: bool
    imu_ok: bool
    calibrating: bool
    clipped: bool
    full_scale_v: float
    diff_mv: float
    baseline_mv: float
    sr: float
    quat: List[float]
    quat_acc: float
    rpy: List[float]
    acc: List[float]
    gyro: List[float]
    lin: List[float]
    grav: List[float]
    mag: List[float]
    steps: int
    stab: int
    v: List[float]
    raw: Optional[List[int]] = None
    seq: Optional[int] = None
    a0_v: Optional[float] = None
    a0_status: Optional[int] = None


_VEC3 = ("rpy", "a", "g", "lin", "grav", "mag")


def _num(x, name):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise PacketError(f"field '{name}' is not a number: {x!r}")
    if not math.isfinite(x):
        raise PacketError(f"field '{name}' is not finite")
    return float(x)


def _vec(d, key, n):
    if key not in d:
        raise PacketError(f"missing field '{key}'")
    arr = d[key]
    if not isinstance(arr, list) or len(arr) != n:
        raise PacketError(f"field '{key}' must be a list of {n} numbers")
    return [_num(x, key) for x in arr]


def parse_packet(text: str, rx_wall: float, rx_perf: float) -> Packet:
    if not isinstance(text, str):
        raise PacketError("binary frame received (expected JSON text)")
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        raise PacketError(f"JSON decode error: {e.msg} (pos {e.pos})") from None
    if not isinstance(d, dict):
        raise PacketError("frame is not a JSON object")
    for k in ("t", "ok", "iok", "cal", "clip", "d", "b", "sr", "qa", "steps", "stab", "q", "v") + _VEC3:
        if k not in d:
            raise PacketError(f"missing field '{k}'")

    v = d["v"]
    if not isinstance(v, list):
        raise PacketError("field 'v' must be a list")
    v = [_num(x, "v") for x in v]

    raw = None
    if "r" in d:
        r = d["r"]
        if not isinstance(r, list) or len(r) != len(v):
            raise PacketError("field 'r' length does not match 'v'")
        raw = []
        for x in r:
            x = _num(x, "r")
            if x != int(x) or not -32768 <= x <= 32767:
                raise PacketError("raw ADC value out of int16 range")
            raw.append(int(x))

    seq = None
    if "seq" in d:
        seq = int(_num(d["seq"], "seq"))

    a0_v = a0_status = None
    if "a0" in d and "a0s" in d:
        a0_v, a0_status = _num(d["a0"], "a0"), int(_num(d["a0s"], "a0s"))

    return Packet(
        rx_wall=rx_wall, rx_perf=rx_perf,
        t_ms=_num(d["t"], "t"),
        ads_ok=bool(d["ok"]), imu_ok=bool(d["iok"]),
        calibrating=bool(d["cal"]), clipped=bool(d["clip"]),
        full_scale_v=_num(d.get("fs", 0.0), "fs"),
        diff_mv=_num(d["d"], "d"), baseline_mv=_num(d["b"], "b"),
        sr=_num(d["sr"], "sr"),
        quat=_vec(d, "q", 4), quat_acc=_num(d["qa"], "qa"),
        rpy=_vec(d, "rpy", 3), acc=_vec(d, "a", 3), gyro=_vec(d, "g", 3),
        lin=_vec(d, "lin", 3), grav=_vec(d, "grav", 3), mag=_vec(d, "mag", 3),
        steps=int(_num(d["steps"], "steps")), stab=int(_num(d["stab"], "stab")),
        v=v, raw=raw, seq=seq, a0_v=a0_v, a0_status=a0_status,
    )

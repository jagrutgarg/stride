"""TENG channel config block.

Which calibration applies
-------------------------
The ESP32 "version 2" firmware already sends CALIBRATED volts (f / m / b divided by 1000). That firmware calibration
is the one that applies. The factor below is a GUI-side pass-through that must stay at scale 1.0, sign +1: the receive
thread computes  V = sign * scale * V_firmware  in exactly one place (sensors/pipeline.py, `cfg.apply`), so with
1.0 / +1 the volts are shown and recorded exactly as the firmware sent them.

Only if the firmware were changed back to uncalibrated volts would you put a scope calibration here instead; never
both. If a factor other than 1.0 / +1 is set the app logs a warning on every connection (double scaling).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChannelConfig:
    key: str                 # ADS1115 input as the firmware names it: "A0", "A1", "A2" ("" for the stock single channel)
    name: str                # label shown in the GUI
    color: str
    scale: float = 1.0
    sign: int = +1

    @property
    def label(self) -> str:
        return f"{self.name} ({self.key})" if self.key else self.name

    def apply(self, v):
        """The single place where a channel's calibration is applied. `v` may be a float or a numpy array."""
        return (self.sign * self.scale) * v


# ============================== EDIT HERE ==============================
#                   key   name      colour      scale        sign
FRONT = ChannelConfig("A0", "Front", "#e6b450", scale=1.0, sign=+1)
MIDDLE = ChannelConfig("A1", "Middle", "#5cc8d6", scale=1.0, sign=+1)
BACK = ChannelConfig("A2", "Back", "#d98bb5", scale=1.0, sign=+1)
# =======================================================================

CHANNELS = [FRONT, MIDDLE, BACK]                        # always shown in this order
LEGACY = ChannelConfig("", "TENG", "#e6b450")           # stock firmware: one channel, one "v" array
_BY_KEY = {c.key: c for c in CHANNELS}


def config_for(channel_id: str, index: int) -> ChannelConfig:
    """Config for a channel id from the firmware (falls back by position, then to a neutral default)."""
    if channel_id in _BY_KEY:
        return _BY_KEY[channel_id]
    if channel_id == "TENG":
        return LEGACY
    if 0 <= index < len(CHANNELS):
        return CHANNELS[index]
    return ChannelConfig(channel_id, channel_id, "#c8c8cc")

"""Simulated ESP32 (SIMULATION ONLY - not hardware).

Serves the same WebSocket JSON frames as the real firmware so the whole GUI
pipeline can be exercised without hardware. Run standalone:
    python -m simulation.fake_esp32 [--port 8765] [--bad 0.0]
or let the GUI start it in "Simulation" mode.

Constants below mirror the firmware (divider ratio 0.0876, ADS LSB, 1.2 ms TENG
period) only so that simulated raw codes and volts are self-consistent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import threading
import time

from websockets.asyncio.server import serve

ADS_LSB_V = 2.048 / 32768.0
DIV_RATIO = 0.0876
PERIOD_S = 1.2e-3
SEND_S = 0.010


class FakeESP32Server:
    def __init__(self, host="127.0.0.1", port=8765, bad_rate=0.0, with_raw=True):
        self.host, self.port, self.bad_rate, self.with_raw = host, port, bad_rate, with_raw
        self.drop_clients_flag = False
        self.amp = 20.0               # pulse amplitude [V] (demo: about +/-20 V)
        self.fs = 25.0                # simulated readable range [V]; |v| beyond it is clamped and clip=1
        self.stall = False            # True: keep connection open but send nothing
        self._loop = None
        self._thread = None
        self._stop = None
        self._ready = threading.Event()
        self.clients = 0
        self.frames_sent = 0
        self.inject_bad = []          # frames (str) to send once, verbatim

    # -- thread control --------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def stop(self):
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(3)

    def drop_clients(self):
        self.drop_clients_flag = True

    def _run(self):
        asyncio.run(self._main())

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with serve(self._handler, self.host, self.port):
            self._ready.set()
            await self._stop.wait()

    # -- frame generation ------------------------------------------------
    async def _handler(self, ws):
        self.clients += 1
        rnd = random.Random(1)
        t0 = time.perf_counter()
        seq = steps = 0
        sample_t = 0.0
        cal_until = 1.0                      # first second: baseline calibration
        last_send = t0
        try:
            while not self._stop.is_set():
                await asyncio.sleep(SEND_S)
                if self.drop_clients_flag:
                    self.drop_clients_flag = False
                    await ws.close()
                    return
                if self.stall:
                    continue
                now = time.perf_counter()
                el = now - t0
                vs, raws, clip = [], [], 0
                while sample_t + PERIOD_S <= el:
                    sample_t += PERIOD_S
                    cal = sample_t < cal_until
                    # periodic "footstep" pulse pair + noise
                    ph = (sample_t % 1.1)
                    v = 0.0 if cal else (
                        self.amp * math.exp(-((ph - 0.20) / 0.020) ** 2)
                        - self.amp * math.exp(-((ph - 0.26) / 0.030) ** 2)
                        + rnd.gauss(0, 0.05))
                    if abs(v) > self.fs:
                        v, clip = math.copysign(self.fs, v), 1
                    v = round(v, 3)
                    raw = max(-32768, min(32767, int(round((v * DIV_RATIO) / ADS_LSB_V))))   # baseline = 0
                    vs.append(v)
                    raws.append(raw)
                if int(el * 1.1) > steps:
                    steps = int(el * 1.1)
                w = 2 * math.pi * 0.5
                roll = 20 * math.sin(w * el)
                pitch = 10 * math.sin(w * el * 0.7 + 1)
                yaw = (30 * el) % 360 - 180
                ca, sa = math.cos(math.radians(roll) / 2), math.sin(math.radians(roll) / 2)
                frame = {
                    "t": int(el * 1000) + 5000, "ok": 1, "iok": 1,
                    "cal": 1 if el < cal_until else 0, "clip": clip, "fs": self.fs,
                    "d": round((raws[-1] if raws else 0) * ADS_LSB_V * 1000, 2), "b": 0.31,
                    "pk": 0, "mx": 0, "mn": 0, "pp": 0, "hmx": 0, "hmn": 0, "hpp": 0, "sr": 833,
                    "q": [round(ca, 4), round(sa, 4), 0.0, 0.0], "qa": 0.021,
                    "rpy": [round(roll, 2), round(pitch, 2), round(yaw, 2)],
                    "a": [round(2 * math.sin(w * el), 3), round(1.5 * math.cos(w * el), 3),
                          round(9.81 + 3 * math.sin(2 * w * el), 3)],
                    "g": [round(0.8 * math.cos(w * el), 4), round(0.5 * math.sin(w * el), 4), 0.01],
                    "lin": [round(2 * math.sin(w * el), 3), round(1.5 * math.cos(w * el), 3), 0.1],
                    "grav": [0.0, 0.0, 9.81],
                    "mag": [round(20 + 5 * math.sin(el), 2), round(-8 + 3 * math.cos(el), 2), 40.0],
                    "steps": steps, "stab": 4, "v": vs,
                }
                if self.with_raw:
                    frame["r"] = raws
                    frame["seq"] = seq
                seq += 1
                text = json.dumps(frame, separators=(",", ":"))
                if self.inject_bad:
                    await ws.send(self.inject_bad.pop(0))
                if self.bad_rate and rnd.random() < self.bad_rate:
                    text = rnd.choice([text[: len(text) // 2], "garbage{{", '{"t":1}',
                                       text.replace('"mag":[', '"mag":["x",')])
                await ws.send(text)
                self.frames_sent += 1
                last_send = now
        except Exception:  # client went away
            pass
        finally:
            self.clients -= 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bad", type=float, default=0.0, help="fraction of corrupted frames")
    ap.add_argument("--stock", action="store_true", help="omit r[] and seq like the stock firmware")
    a = ap.parse_args()
    s = FakeESP32Server(a.host, a.port, a.bad, with_raw=not a.stock)
    s.start()
    print(f"Fake ESP32 on ws://{a.host}:{a.port}/  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s.stop()

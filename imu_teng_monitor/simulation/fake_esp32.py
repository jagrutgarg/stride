"""Simulated ESP32, "version 2" message format (SIMULATION ONLY - not hardware).

Run standalone:   python -m simulation.fake_esp32 [--port 8765] [--bad 0.0] [--seconds 30]
or let the GUI start it in "Simulation" mode.

Sends what the version 2 firmware sends: first {"info": {...}}, then about 100 frames per second
    {"t":ms,"s":status,"cp":us,"r":[...],"a":[...],"g":[...],"f":[...],"m":[...],"b":[...]}
with integer values (r in 1/100 deg, a and g in 1/1000, f/m/b in millivolts). The three TENG trains are about
+/-5 V bipolar pulse pairs with noise, once per 1.1 s: Back fires first, Middle 40 ms later, Front 80 ms after Back.
The sample period `cp` is 3700 us (about 270 samples/s), and sample k of an n-sample frame is at t - (n-1-k)*cp.
The text command "zero" starts a 0.6 s baseline recalibration (status bit 4), like the real firmware.
`amp` (volts) and `range_v` can be changed at run time; a pulse beyond range_v is clamped and sets the clip bit.
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

CP_US = 3700
SEND_S = 0.010
CH_DELAY_S = (0.080, 0.040, 0.0)        # Front, Middle, Back: Back fires first
CH_GAIN = (1.0, 0.8, 1.0)
ZERO_CAL_S = 0.6
FIRST_CAL_S = 1.0


def _pulse(ph, amp):
    return amp * math.exp(-((ph - 0.20) / 0.020) ** 2) - amp * math.exp(-((ph - 0.26) / 0.030) ** 2)


class FakeESP32Server:
    def __init__(self, host="127.0.0.1", port=8765, bad_rate=0.0):
        self.host, self.port, self.bad_rate = host, port, bad_rate
        self.drop_clients_flag = False
        self.amp = 5.0                # pulse amplitude [V]
        self.range_v = 18.3           # readable range [V]; beyond it a value is clamped and the clip bit is set
        self.noise = 0.05
        self.stall = False            # True: keep connection open but send nothing
        self.zero_count = 0           # "zero" commands received
        self.send_info = True
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

    # -- frames ------------------------------------------------------------
    def _info(self):
        return {"info": {"fw": "simulator v2", "scales": {"r": 100, "a": 1000, "g": 1000, "f": 1000, "m": 1000,
                                                          "b": 1000}, "cp_us": CP_US, "range_V": self.range_v}}

    async def _reader(self, ws, state):
        """Commands from the GUI."""
        try:
            async for msg in ws:
                if msg == "zero":
                    self.zero_count += 1
                    state["cal_until"] = time.perf_counter() - state["t0"] + ZERO_CAL_S
        except Exception:  # noqa: BLE001 - client went away
            pass

    async def _handler(self, ws):
        self.clients += 1
        rnd = random.Random(1)
        t0 = time.perf_counter()
        state = {"t0": t0, "cal_until": FIRST_CAL_S}
        reader = asyncio.create_task(self._reader(ws, state))
        k_next = 0                                  # index of the next sample on the cp grid
        try:
            if self.send_info:
                await ws.send(json.dumps(self._info(), separators=(",", ":")))
            while not self._stop.is_set():
                await asyncio.sleep(SEND_S)
                if self.drop_clients_flag:
                    self.drop_clients_flag = False
                    await ws.close()
                    return
                if self.stall:
                    continue
                el = time.perf_counter() - t0
                t_ms = int(el * 1000)
                # samples whose time (k * cp) is <= the frame time; the newest one is at t_frame
                k_last = int(el / (CP_US / 1e6))
                n = max(0, k_last - k_next + 1)
                ks = range(k_next, k_next + n)
                k_next += n
                cal = el < state["cal_until"]
                trains, clip = [[], [], []], 0
                for k in ks:
                    ts = k * CP_US / 1e6
                    for c in range(3):
                        v = 0.0 if cal else _pulse((ts - CH_DELAY_S[c]) % 1.1, self.amp * CH_GAIN[c]) + \
                            rnd.gauss(0, self.noise)
                        if abs(v) > self.range_v:
                            v, clip = math.copysign(self.range_v, v), clip | (8 << c)
                        trains[c].append(round(v * 1000))
                w = 2 * math.pi * 0.5
                roll = 20 * math.sin(w * el)
                pitch = 10 * math.sin(w * el * 0.7 + 1)
                yaw = (30 * el) % 360 - 180
                frame = {
                    "t": t_ms + 5000, "s": 1 | 2 | (4 if cal else 0) | clip, "cp": CP_US,
                    "r": [round(roll * 100), round(pitch * 100), round(yaw * 100)],
                    "a": [round(2000 * math.sin(w * el)), round(1500 * math.cos(w * el)),
                          round(9810 + 3000 * math.sin(2 * w * el))],
                    "g": [round(800 * math.cos(w * el)), round(500 * math.sin(w * el)), 10],
                    "f": trains[0], "m": trains[1], "b": trains[2],
                }
                text = json.dumps(frame, separators=(",", ":"))
                if self.inject_bad:
                    await ws.send(self.inject_bad.pop(0))
                if self.bad_rate and rnd.random() < self.bad_rate:
                    text = rnd.choice([text[: len(text) // 2], "garbage{{", '{"t":1}',
                                       text.replace('"a":[', '"a":["x",')])
                await ws.send(text)
                self.frames_sent += 1
        except Exception:  # client went away
            pass
        finally:
            reader.cancel()
            self.clients -= 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bad", type=float, default=0.0, help="fraction of corrupted frames")
    ap.add_argument("--seconds", type=float, default=0, help="exit by itself after this many seconds (0 = run until Ctrl+C)")
    a = ap.parse_args()
    s = FakeESP32Server(a.host, a.port, a.bad)
    s.start()
    print(f"Fake ESP32 (version 2 format) on ws://{a.host}:{a.port}/  (Ctrl+C to stop)")
    try:
        end = time.time() + a.seconds if a.seconds else None
        while end is None or time.time() < end:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    s.stop()

"""WebSocket client running in its own QThread.

All output goes through one thread-safe deque (`events`) so ordering between
state changes, log lines and data frames is preserved. The GUI thread drains it
on a timer; nothing is ever discarded here (the deque is unbounded and drained
every few milliseconds).

Event tuples:
  ("state", "connecting" | "connected" | "disconnected", wall_time)
  ("log", level, message, wall_time)
  ("packet", Packet)
  ("bad", reason, snippet, wall_time)
"""
from __future__ import annotations

import time
from collections import deque

import websocket  # websocket-client
from PySide6.QtCore import QThread

from config import settings
from communication.data_parser import PacketError, parse_packet


class ESP32Client(QThread):
    def __init__(self, host: str, port: int = settings.WS_PORT, parent=None):
        super().__init__(parent)
        self.host, self.port = host, port
        self.url = f"ws://{host}:{port}{settings.WS_PATH}"
        self.events: deque = deque()
        self._stop_requested = False
        self.frames_received = 0

    # -- control ---------------------------------------------------------
    def stop(self):
        self._stop_requested = True

    # -- helpers ---------------------------------------------------------
    def _log(self, level, msg):
        self.events.append(("log", level, msg, time.time()))

    def _state(self, s):
        self.events.append(("state", s, time.time()))

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while not self._stop_requested and time.monotonic() < end:
            time.sleep(0.05)

    # -- thread body -----------------------------------------------------
    def run(self):
        attempt = 0
        while not self._stop_requested:
            self._state("connecting")
            ws = None
            try:
                ws = websocket.create_connection(self.url, timeout=settings.CONNECT_TIMEOUT_S)
                ws.settimeout(0.2)
            except Exception as e:  # noqa: BLE001 - any network error
                self._log("ERROR", f"Connection to {self.url} failed: {e}")
                self._state("disconnected")
                delays = settings.RECONNECT_DELAY_S
                self._sleep(delays[min(attempt, len(delays) - 1)])
                attempt += 1
                continue

            attempt = 0
            self._state("connected")
            self._receive_loop(ws)
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._state("disconnected")
            if not self._stop_requested:
                self._sleep(settings.RECONNECT_DELAY_S[0])

    def _receive_loop(self, ws):
        last_rx = time.monotonic()
        while not self._stop_requested:
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                if time.monotonic() - last_rx > settings.STALE_TIMEOUT_S:
                    self._log("ERROR", f"No data for {settings.STALE_TIMEOUT_S:.0f} s: connection lost")
                    return
                continue
            except (websocket.WebSocketConnectionClosedException, ConnectionError, OSError) as e:
                if not self._stop_requested:
                    self._log("ERROR", f"Connection lost: {e}")
                return
            except Exception as e:  # noqa: BLE001
                self._log("ERROR", f"Communication error: {e}")
                return

            if msg is None or msg == "":
                if not self._stop_requested:
                    self._log("ERROR", "Connection closed by ESP32")
                return
            last_rx = time.monotonic()
            self.frames_received += 1
            rx_wall, rx_perf = time.time(), time.perf_counter()
            try:
                pkt = parse_packet(msg, rx_wall, rx_perf)
            except PacketError as e:
                snippet = msg[:80] if isinstance(msg, str) else repr(msg[:40])
                self.events.append(("bad", str(e), snippet, rx_wall))
                continue
            self.events.append(("packet", pkt))

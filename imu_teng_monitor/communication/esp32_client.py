"""WebSocket client in its own QThread: receive -> parse once -> process, all off the UI thread.

The UI thread never touches frames. `on_packet(pkt)` (the pipeline) runs in this thread and returns a list of
(level, text) errors; `on_info(info)` receives the {"info": ...} message that follows a connection. Only connection
events and errors reach the UI, through a small thread-safe deque:
  ("state", "connecting" | "connected" | "disconnected", wall_time)
  ("log", level, message, wall_time)
Nothing is logged per message; repeated invalid frames are summarised at most twice a second.

Commands to the ESP32 (the text "zero" re-zeroes the TENG baselines) are queued with `send_text` and written by this
thread between receives, so the socket is never read and written from two threads at once.
"""
from __future__ import annotations

import time
from collections import deque

import websocket  # websocket-client
from PySide6.QtCore import QThread

from config import settings
from communication.data_parser import Info, PacketError, parse_packet


class ESP32Client(QThread):
    def __init__(self, host: str, port: int, on_packet, on_connect=None, on_bad=None, on_info=None, parent=None):
        super().__init__(parent)
        self.host, self.port = host, port
        self.url = f"ws://{host}:{port}{settings.WS_PATH}"
        self.on_packet, self.on_connect, self.on_bad, self.on_info = on_packet, on_connect, on_bad, on_info
        self.events: deque = deque()
        self._outbox: deque = deque()
        self._stop_requested = False
        self._last_err_log = {}
        self.connected = False

    def stop(self):
        self._stop_requested = True

    def send_text(self, text: str) -> bool:
        """Queue a text command for the ESP32. Returns False if there is no connection to send it on."""
        if not self.connected:
            return False
        self._outbox.append(text)
        return True

    # -- helpers ---------------------------------------------------------
    def _log(self, level, msg):
        self.events.append(("log", level, msg, time.time()))

    def _state(self, s):
        self.events.append(("state", s, time.time()))

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while not self._stop_requested and time.monotonic() < end:
            time.sleep(0.05)

    def _rate_limited_error(self, key, text, every=0.5, level="ERROR"):
        now = time.monotonic()
        if now - self._last_err_log.get(key, 0.0) >= every:
            self._last_err_log[key] = now
            self._log(level, text)

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
            self._outbox.clear()
            if self.on_connect:
                self.on_connect()
            self.connected = True
            self._state("connected")
            self._receive_loop(ws)
            self.connected = False
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._state("disconnected")
            if not self._stop_requested:
                self._sleep(settings.RECONNECT_DELAY_S[0])

    def _flush_outbox(self, ws):
        while self._outbox:
            ws.send(self._outbox.popleft())

    def _receive_loop(self, ws):
        last_rx = time.monotonic()
        while not self._stop_requested:
            try:
                self._flush_outbox(ws)
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
            try:
                pkt = parse_packet(msg, time.time())                # parsed exactly once, here
            except PacketError as e:
                if self.on_bad:
                    self.on_bad()
                snippet = msg[:60] if isinstance(msg, str) else repr(msg[:30])
                self._rate_limited_error("bad", f"Invalid packet: {e} | {snippet!r}")
                continue
            try:
                if isinstance(pkt, Info):                           # configuration, not a data frame
                    if self.on_info:
                        self.on_info(pkt)
                    continue
                for level, text in self.on_packet(pkt):
                    self._rate_limited_error(text[:30], text, every=2.0, level=level)
            except Exception as e:  # noqa: BLE001 - a bug must not kill the connection
                self._rate_limited_error("proc", f"Processing error: {type(e).__name__}: {e}", every=2.0)

from __future__ import annotations

import os
import time

from PySide6.QtCore import QEventLoop, QSettings, Qt, QThread, QTimer
from PySide6.QtWidgets import (QFileDialog, QMainWindow, QMessageBox, QProgressDialog, QSplitter,
                               QVBoxLayout, QWidget, QScrollArea, QFrame)

from communication.esp32_client import ESP32Client
from config import settings
from gui.connection_panel import ConnectionPanel
from gui.control_panel import ControlPanel
from gui.graph_panel import GraphPanel
from gui.imu_panel import ImuPanel
from gui.log_panel import LogPanel
from gui.widgets import fade_in
from gui.scope_panel import ScopePanel
from gui.teng_panel import TengPanel
from sensors.pipeline import Pipeline
from simulation.fake_esp32 import FakeESP32Server
from storage.excel_exporter import export_xlsx
from storage.session import SessionStore


class ExportThread(QThread):
    def __init__(self, store, path, extra, parent=None):
        super().__init__(parent)
        self.store, self.path, self.extra = store, path, extra
        self.error: str | None = None
        self.result: dict | None = None
        self.progress = (0, 1)

    def run(self):
        try:
            self.result = export_xlsx(self.store, self.path, self.extra,
                                      lambda d, t: setattr(self, "progress", (d, t)))
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(settings.APP_NAME)
        self.resize(1750, 950)
        self.qs = QSettings("imu_teng_monitor", "gui")

        self.store = SessionStore()
        self.pipeline = Pipeline(self.store)
        self.client: ESP32Client | None = None
        self.sim_server: FakeESP32Server | None = None
        self.paused = False
        self.paused_at_teng = 0
        self.conn_state = "disconnected"
        self.connected_since: float | None = None
        self.want_connection = False
        self.first_frame_logged = False
        self._bad_suppressed = 0
        self._last_bad_log = 0.0
        self._last_heartbeat = 0.0
        self._prev_flags = None
        self._tick_n = 0
        self._stream_ok = False

        self._build_ui()
        self.conn.ip.setText(self.qs.value("ip", settings.DEFAULT_IP))
        self.timer = QTimer(self)
        self.timer.setInterval(settings.UI_TICK_MS)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._intro_done = False
        self.log.add("INFO", "Application started. Enter the ESP32 IP and press Connect (or use Simulation).")

    def showEvent(self, e):
        super().showEvent(e)
        if not self._intro_done:                       # one entrance: a short staggered fade of the four zones
            self._intro_done = True
            for i, w in enumerate((self.conn, self.ctrl, self.teng_panel, self.scope_panel, self.imu_panel,
                                   self.graphs, self.log)):
                fade_in(w, delay_ms=i * 50)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        self.conn = ConnectionPanel()
        self.ctrl = ControlPanel()
        self.teng_panel = TengPanel()
        self.imu_panel = ImuPanel()
        self.scope_panel = ScopePanel()
        self.graphs = GraphPanel()
        self.log = LogPanel()

        lay.addWidget(self.conn)
        lay.addWidget(self.ctrl)

        def scrolled(widgets, min_w):
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(12)
            for w in widgets:
                bl.addWidget(w)
            bl.addStretch(1)
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            sc.setWidget(box)
            sc.setStyleSheet('QScrollArea{background:transparent;} '
                             'QScrollArea > QWidget > QWidget{background:transparent;}')
            sc.setMinimumWidth(min_w)
            return sc

        top = QSplitter(Qt.Horizontal)
        top.addWidget(scrolled([self.teng_panel, self.scope_panel], 500))     # TENG + scope
        top.addWidget(scrolled([self.imu_panel], 440))                         # BNO08x
        top.addWidget(self.graphs)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 0)
        top.setStretchFactor(2, 1)
        top.setSizes([520, 460, 770])

        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(self.log)
        main.setStretchFactor(0, 1)
        main.setSizes([740, 150])
        lay.addWidget(main, 1)

        self.conn.connect_clicked.connect(self.on_connect)
        self.conn.disconnect_clicked.connect(self.on_disconnect)
        self.scope_panel.window_changed.connect(lambda s: setattr(self.pipeline.scope, "window_s", s))
        self.scope_panel.threshold_changed.connect(lambda v: setattr(self.pipeline.scope, "threshold", v))
        self.scope_panel.hold_reset.connect(self.pipeline.scope.reset_hold)
        self.ctrl.pause_toggled.connect(self.on_pause)
        self.ctrl.reset_clicked.connect(self.on_reset)
        self.ctrl.restart_clicked.connect(self.on_restart)
        self.ctrl.save_clicked.connect(lambda: self.save_flow())

    # ------------------------------------------------------------ connection
    def _target(self):
        if self.conn.simulation:
            return settings.SIM_HOST, settings.SIM_PORT
        text = self.conn.ip.text().strip()
        if not text:
            raise ValueError("Enter the ESP32 IP address")
        if ":" in text:
            host, port = text.rsplit(":", 1)
            return host.strip(), int(port)
        return text, settings.WS_PORT

    def on_connect(self):
        try:
            host, port = self._target()
        except ValueError as e:
            QMessageBox.warning(self, "Connect", str(e))
            return
        mode = "SIMULATION" if self.conn.simulation else "Live"
        if self.store.has_data and self.store.conn.mode != mode:
            if not self._confirm_new_session(f"Switching to {mode} mode starts a new session "
                                             f"(simulated and live data are never mixed)."):
                return
            self._new_session()
        if self.conn.simulation:
            if self.sim_server is None:
                self.sim_server = FakeESP32Server(settings.SIM_HOST, settings.SIM_PORT)
                self.sim_server.start()
            self.log.add("WARN", "SIMULATION MODE: data comes from a built-in fake ESP32, not hardware.")
        else:
            self.qs.setValue("ip", self.conn.ip.text().strip())
        self._start_client(host, port, mode)

    def _start_client(self, host, port, mode):
        c = self.store.conn
        c.mode, c.host, c.port = mode, host, port
        c.url = f"ws://{host}:{port}{settings.WS_PATH}"
        self.ctrl.sim_badge.setVisible(mode == "SIMULATION")
        self.setWindowTitle(settings.APP_NAME + ("  [SIMULATION]" if mode == "SIMULATION" else ""))
        self.want_connection = True
        self.client = ESP32Client(host, port)
        self.conn.set_state("connecting", host)
        self.log.add("INFO", f"Connecting to {c.url} …")
        self.client.start()

    def _stop_client(self):
        """Stop the thread and process everything it still had queued (nothing is lost)."""
        cl = self.client
        if cl is None:
            return
        cl.stop()
        cl.wait(4000)
        self._drain(cl, budget=None)
        self.client = None

    def on_disconnect(self):
        self.want_connection = False
        self._stop_client()
        self._set_state("disconnected")
        self.log.add("INFO", "Disconnected by user.")

    def _set_state(self, state):
        prev = self.conn_state
        self.conn_state = state
        self.conn.set_state(state)
        if state == "connected":
            self.connected_since = time.monotonic()
        elif prev == "connected":
            self.store.conn.total_connected_s += time.monotonic() - (self.connected_since or time.monotonic())
            self.connected_since = None
        if state == "disconnected" and not self.want_connection:
            self.conn.set_state("disconnected")

    # ---------------------------------------------------------------- events
    def _drain(self, client, budget=0.008):
        t0 = time.perf_counter()
        q = client.events
        while True:
            try:
                ev = q.popleft()
            except IndexError:
                return
            self._handle(ev)
            if budget is not None and time.perf_counter() - t0 > budget:
                return

    def _handle(self, ev):
        kind = ev[0]
        c = self.store.conn
        if kind == "packet":
            pkt = ev[1]
            if not self.first_frame_logged:
                self.first_frame_logged = True
                self.log.add("OK", f"Sensor data received (first frame, {len(pkt.v)} TENG samples).", pkt.rx_wall)
                if pkt.raw is None:
                    self.log.add("WARN", "Firmware does not send raw ADC / frame counter (stock firmware): "
                                         "duplicate detection uses ESP32 time; debug sheet raw column is sparse.")
            ok, notes = self.pipeline.process(pkt)
            for lvl, msg in notes:
                self.log.add(lvl, msg, pkt.rx_wall)
            if ok:
                self._check_flags(pkt)
                now = time.monotonic()
                if now - self._last_heartbeat > 10:
                    self._last_heartbeat = now
                    self.log.add("INFO", f"Receiving data: {self.pipeline.rx_rate_hz():.0f} frames/s, "
                                         f"{pkt.sr:.0f} TENG samples/s", pkt.rx_wall)
        elif kind == "state":
            _, state, wall = ev
            if state == "connected":
                c.connect_count += 1
                self.pipeline.on_connected()
                self.first_frame_logged = False
                self.log.add("OK", f"Connection established with {c.host}:{c.port}", wall)
            elif state == "disconnected" and self.conn_state == "connected":
                c.disconnect_count += 1
                self.log.add("ERROR", "Connection lost. Retrying automatically…", wall)
            self._set_state(state)
        elif kind == "log":
            self.log.add(ev[1], ev[2], ev[3])
        elif kind == "bad":
            c.frames_invalid += 1
            _, reason, snippet, wall = ev
            now = time.monotonic()
            if now - self._last_bad_log > 0.5:
                extra = f" (+{self._bad_suppressed} more since last line)" if self._bad_suppressed else ""
                self._bad_suppressed = 0
                self._last_bad_log = now
                self.log.add("ERROR", f"Invalid packet: {reason} | {snippet!r}{extra}", wall)
            else:
                self._bad_suppressed += 1

    def _check_flags(self, p):
        flags = (p.ads_ok, p.imu_ok, p.calibrating, p.clipped)
        prev, self._prev_flags = self._prev_flags, flags
        if prev is None or prev == flags:
            return
        names = ("ADS1115 (TENG) found", "BNO08x found", "TENG baseline calibrating", "TENG input clipped")
        for n, a, b in zip(names, prev, flags):
            if a != b:
                bad = (n.endswith("found") and not b) or (n.endswith("clipped") and b)
                self.log.add("WARN" if bad else "INFO", f"{n}: {'yes' if b else 'no'}", p.rx_wall)

    # ------------------------------------------------------------------ tick
    def _tick(self):
        if self.client is not None:
            self._drain(self.client)
        self._tick_n += 1
        pl = self.pipeline
        rate = pl.rx_rate_hz()
        self.conn.update_info(self.connected_since, pl.last_wall, rate)

        # keep the link indicator honest if frames stop although the socket is still open
        un, ut = self.store.unsaved_counts
        self.ctrl.set_saved(not self.store.has_unsaved, un, ut)

        if self.paused:
            self.ctrl.set_paused(True, self.store.teng.n - self.paused_at_teng)
            return
        if pl.imu.latest is not None:
            self.imu_panel.update_packet(pl.imu.latest)
            self.teng_panel.update_from(pl.teng)
            self.scope_panel.update_from(pl.scope, pl.teng)
            if self._tick_n % settings.PLOT_EVERY_N_TICKS == 0:
                self.graphs.refresh(pl.teng, pl.imu, pl.scope)

    # --------------------------------------------------------------- controls
    def on_pause(self, on):
        self.paused = on
        self.graphs.set_paused(on)
        if on:
            self.paused_at_teng = self.store.teng.n
            self.log.add("INFO", "Display paused. Data is still being received and recorded.")
        else:
            self.log.add("INFO", f"Display resumed ({self.store.teng.n - self.paused_at_teng:,} TENG samples "
                                 "were recorded while paused).")
            self._tick_n = 0
            if self.pipeline.imu.latest is not None:
                self.graphs.refresh(self.pipeline.teng, self.pipeline.imu, self.pipeline.scope)

    def _clear_display(self):
        self.pipeline.reset_display()
        self.graphs.clear()
        self.imu_panel.clear()
        self.teng_panel.clear()
        self.scope_panel.clear()
        self.log.clear()
        self._prev_flags = None

    def on_reset(self):
        self._clear_display()
        self.store.next_segment()
        self.paused_at_teng = self.store.teng.n
        self.log.add("INFO", f"Display reset (graphs, statistics, log). Recorded data kept; "
                             f"new display segment #{self.store.segment}.")

    def _new_session(self):
        self.store.reset_session()
        self.pipeline.new_session()
        self._clear_display()
        self.first_frame_logged = False
        self.paused_at_teng = 0

    def on_restart(self):
        reconnect = self.want_connection or self.conn_state != "disconnected"
        host_port = (self.store.conn.host, self.store.conn.port, self.store.conn.mode)
        if reconnect:
            self._stop_client()      # drains everything received so far into the old session
            self._set_state("disconnected")
        if self.store.has_unsaved and not self._confirm_new_session("Restart starts a new session."):
            if reconnect:
                self._start_client(*host_port)
            return
        self._new_session()
        self.log.add("INFO", "Session restarted (ESP32 was not rebooted).")
        if reconnect:
            self._start_client(*host_port)

    # ----------------------------------------------------------- save / discard
    def _confirm_new_session(self, why: str) -> bool:
        """True if it is OK to drop the current data (saved it, or user chose to discard)."""
        if not self.store.has_unsaved:
            return True
        un, ut = self.store.unsaved_counts
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Unsaved data")
        box.setText(f"{why}\n\nUnsaved: {un:,} IMU frames and {ut:,} TENG samples.")
        b_save = box.addButton("Save first…", QMessageBox.AcceptRole)
        b_disc = box.addButton("Discard data", QMessageBox.DestructiveRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is b_save:
            return self.save_flow()
        return box.clickedButton() is b_disc

    def save_flow(self) -> bool:
        if not self.store.has_data:
            QMessageBox.information(self, "Save", "No data has been recorded yet.")
            return False
        default = os.path.join(os.path.expanduser("~"), time.strftime("imu_teng_%Y%m%d_%H%M%S.xlsx"))
        path, _ = QFileDialog.getSaveFileName(self, "Save session to Excel", default, "Excel workbook (*.xlsx)")
        if not path:
            return False
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        imu_n, teng_n = self.store.imu.n, self.store.teng.n
        # the exporter reads the first n rows only; rows arriving meanwhile stay "unsaved"
        t = ExportThread(self.store, path, {"sr": self.pipeline.teng.sr,
                                  "conn_state": {"connected": "Connected", "connecting": "Connecting",
                                                 "disconnected": "Disconnected"}[self.conn_state],
                                  "scope": f"{self.pipeline.scope.window_s:g} s / {self.pipeline.scope.threshold:g} V"}, self)
        dlg = QProgressDialog("Writing Excel file…", None, 0, 100, self)
        dlg.setWindowTitle("Saving")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        loop = QEventLoop()
        t.finished.connect(loop.quit)
        poll = QTimer(self)
        poll.timeout.connect(lambda: dlg.setValue(int(100 * t.progress[0] / max(1, t.progress[1]))))
        poll.start(100)
        t.start()
        dlg.show()
        loop.exec()
        poll.stop()
        dlg.close()
        if t.error:
            self.log.add("ERROR", f"Excel export failed: {t.error}")
            QMessageBox.critical(self, "Save failed",
                                 f"Could not write\n{path}\n\n{t.error}\n\nYour data is still in memory. "
                                 "(If the file is open in Excel, close it and try again.)")
            return False
        self.store.mark_saved(imu_n, teng_n)
        self.log.add("OK", f"Excel export completed: {path} ({imu_n:,} IMU rows, {teng_n:,} TENG rows)")
        return True

    # ------------------------------------------------------------------ close
    def closeEvent(self, e):
        if self.store.has_unsaved and not self._confirm_new_session("Closing the application."):
            e.ignore()
            return
        self.timer.stop()
        self.want_connection = False
        self._stop_client()
        if self.sim_server:
            self.sim_server.stop()
        e.accept()

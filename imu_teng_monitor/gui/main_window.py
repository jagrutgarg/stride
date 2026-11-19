from __future__ import annotations

import json
import os
import time
from collections import deque

from PySide6.QtCore import QEventLoop, QSettings, Qt, QThread, QTimer
from PySide6.QtWidgets import (QFileDialog, QFrame, QMainWindow, QMessageBox, QProgressDialog, QScrollArea,
                               QSplitter, QVBoxLayout, QWidget)

from communication.esp32_client import ESP32Client
from config import channels as chcfg
from config import settings
from gui.channel_cards import ChannelCard, VppBar
from gui.connection_panel import ConnectionPanel
from gui.control_panel import ControlPanel
from gui.graph_panel import GraphPanel
from gui.imu_panel import ImuPanel
from gui.log_panel import LogPanel
from gui.widgets import fade_in
from sensors.pipeline import Pipeline
from simulation.fake_esp32 import FakeESP32Server
from storage.excel_exporter import export_csv, export_xlsx
from storage.session import SessionStore


class ExportThread(QThread):
    def __init__(self, store, path, extra, csv=False, parent=None):
        super().__init__(parent)
        self.store, self.path, self.extra, self.csv = store, path, extra, csv
        self.error: str | None = None
        self.result: dict | None = None
        self.progress = (0, 1)

    def run(self):
        try:
            fn = export_csv if self.csv else export_xlsx
            self.result = fn(self.store, self.path, self.extra, lambda d, t: setattr(self, "progress", (d, t)))
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"


class MainWindow(QMainWindow):
    def __init__(self, qs: QSettings | None = None):
        super().__init__()
        self.setWindowTitle(settings.APP_NAME)
        self.resize(1850, 1000)
        self.qs = qs if qs is not None else QSettings("imu_teng_monitor", "gui")   # tests pass a temp file

        self.store = SessionStore()
        self.pipeline = Pipeline(self.store)
        self.client: ESP32Client | None = None
        self.sim_server: FakeESP32Server | None = None
        self.paused = False
        self.paused_at_teng = 0
        self.conn_state = "disconnected"
        self.want_connection = False
        self._ui_keys: list = []
        self.cards: list = []
        self._rate_hist: deque = deque()
        self._intro_done = False

        self._build_ui()
        self._build_cards(chcfg.CHANNELS)
        self.conn.ip.setText(self.qs.value("ip", settings.DEFAULT_IP))

        # the UI thread only draws: charts at ~25 fps, numbers and header at ~15 Hz
        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(round(1000 / settings.PLOT_FPS))
        self.plot_timer.timeout.connect(self._plot_tick)
        self.text_timer = QTimer(self)
        self.text_timer.setInterval(round(1000 / settings.TEXT_HZ))
        self.text_timer.timeout.connect(self._text_tick)
        self.plot_timer.start()
        self.text_timer.start()
        self.log.add("INFO", "Enter the ESP32 IP and press Connect (or use Simulation).")

    def showEvent(self, e):
        super().showEvent(e)
        if not self._intro_done:                       # one entrance: a short staggered fade of the zones
            self._intro_done = True
            for i, w in enumerate((self.conn, self.ctrl, self.col_left, self.graphs, self.log)):
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
        self.vpp_bar = VppBar()
        self.imu_panel = ImuPanel()
        self.graphs = GraphPanel()
        self.log = LogPanel()
        lay.addWidget(self.conn)
        lay.addWidget(self.ctrl)

        box = QWidget()
        self.left_layout = QVBoxLayout(box)
        self.left_layout.setContentsMargins(0, 0, 0, 0)
        self.left_layout.setSpacing(12)
        self.left_layout.addWidget(self.vpp_bar)
        self.left_layout.addWidget(self.imu_panel)       # cards are inserted between the two
        self.left_layout.addStretch(1)
        self.col_left = QScrollArea()
        self.col_left.setWidgetResizable(True)
        self.col_left.setFrameShape(QFrame.NoFrame)
        self.col_left.setWidget(box)
        self.col_left.setStyleSheet('QScrollArea{background:transparent;} '
                                    'QScrollArea > QWidget > QWidget{background:transparent;}')
        self.col_left.setMinimumWidth(420)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.col_left)
        top.addWidget(self.graphs)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setSizes([440, 1380])

        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(self.log)
        main.setStretchFactor(0, 1)
        main.setSizes([860, 90])
        self.log.setMinimumHeight(90)
        lay.addWidget(main, 1)

        self.conn.connect_clicked.connect(self.on_connect)
        self.conn.disconnect_clicked.connect(self.on_disconnect)
        self.vpp_bar.window_changed.connect(self.pipeline.set_window)
        self.ctrl.pause_toggled.connect(self.on_pause)
        self.ctrl.reset_clicked.connect(self.on_reset)
        self.ctrl.restart_clicked.connect(self.on_restart)
        self.ctrl.save_clicked.connect(lambda: self.save_flow())

    def _build_cards(self, cfgs):
        """One card and one plot per channel: Front (A0), Middle (A1), Back (A2) for the foot, one for stock firmware."""
        for c in self.cards:
            self.left_layout.removeWidget(c)
            c.deleteLater()
        self.cards = []
        for i, cfg in enumerate(cfgs):
            card = ChannelCard(cfg)
            self.left_layout.insertWidget(1 + i, card)          # after the Vpp bar, before the IMU panel
            self.cards.append(card)
        self.graphs.set_channels(cfgs)
        self._ui_keys = [c.label for c in cfgs]

    def _sync_channel_ui(self):
        """Match the UI to the channels the firmware really sends (3 for the foot, 1 for the stock firmware)."""
        chans = list(self.pipeline.channels)
        if chans and [c.cfg.label for c in chans] != self._ui_keys:
            self._build_cards([c.cfg for c in chans])

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
            self.log.add("WARN", "Simulation mode: data comes from a built-in fake ESP32, not hardware.")
        else:
            self.qs.setValue("ip", self.conn.ip.text().strip())
        self._start_client(host, port, mode)

    def _count_bad(self):
        self.store.conn.frames_invalid += 1

    def _start_client(self, host, port, mode):
        c = self.store.conn
        c.mode, c.host, c.port = mode, host, port
        c.url = f"ws://{host}:{port}{settings.WS_PATH}"
        self.ctrl.sim_badge.setVisible(mode == "SIMULATION")
        self.setWindowTitle(settings.APP_NAME + ("  [SIMULATION]" if mode == "SIMULATION" else ""))
        self.want_connection = True
        self.client = ESP32Client(host, port, self.pipeline.process, self.pipeline.on_connected, self._count_bad,
                                  self.pipeline.on_info)
        self.conn.set_state("connecting")
        self.log.add("INFO", f"Connecting to {c.url}")
        self.client.start()

    def _stop_client(self):
        cl = self.client
        if cl is None:
            return
        cl.stop()
        cl.wait(4000)
        self._drain(cl)
        self.client = None

    def on_disconnect(self):
        self.want_connection = False
        self._stop_client()
        self._set_state("disconnected")
        self.log.add("INFO", "Disconnected.")

    def _set_state(self, state):
        self.conn_state = state
        self.conn.set_state(state)

    # ---------------------------------------------------------------- events
    def _drain(self, client):
        """Only connection state changes and errors arrive here (a handful per second at most)."""
        q = client.events
        c = self.store.conn
        while True:
            try:
                ev = q.popleft()
            except IndexError:
                return
            if ev[0] == "state":
                _, state, wall = ev
                if state == "connected":
                    c.connect_count += 1
                    self.log.add("OK", f"Connected to {c.host}:{c.port}", wall)
                elif state == "disconnected" and self.conn_state == "connected":
                    c.disconnect_count += 1
                    self.log.add("ERROR", "Connection lost, retrying", wall)
                self._set_state(state)
            else:
                self.log.add(ev[1], ev[2], ev[3])

    # ------------------------------------------------------------------ timers
    def _frame_rate(self, now):
        h = self._rate_hist
        h.append((now, self.pipeline.frames_total))
        while len(h) > 2 and now - h[0][0] > 1.0:
            h.popleft()
        (t0, n0), (t1, n1) = h[0], h[-1]
        return (n1 - n0) / (t1 - t0) if t1 > t0 else 0.0

    def _text_tick(self):
        if self.client is not None:
            self._drain(self.client)
        pl = self.pipeline
        self.conn.update_info(pl.last_wall, self._frame_rate(time.monotonic()))
        un, ut = self.store.unsaved_counts
        self.ctrl.set_saved(not self.store.has_unsaved, un, ut)
        if self.paused:
            self.ctrl.set_paused(True, self.store.teng.n - self.paused_at_teng)
            return
        self._sync_channel_ui()
        chans, imu, _, range_v = pl.readout()
        for card, (v, vpp, clipped) in zip(self.cards, chans):
            card.show_values(v, vpp, clipped)
            card.show_clip(clipped, range_v)
        self.imu_panel.show_values(imu)

    def _plot_tick(self):
        """Skip drawing while paused, minimised or hidden. The chart itself skips when no new data arrived."""
        if self.paused or self.isMinimized() or not self.isVisible():
            return
        self.graphs.refresh(self.pipeline)

    # --------------------------------------------------------------- controls
    def on_pause(self, on):
        self.paused = on
        self.graphs.set_paused(on)
        if on:
            self.paused_at_teng = self.store.teng.n

    def _clear_display(self):
        self.pipeline.reset_display()
        self.graphs.clear()
        self.imu_panel.clear()
        for c in self.cards:
            c.clear()
        self.log.clear()

    def on_reset(self):
        if self.client is not None:
            self.client.send_text("zero")         # ask the ESP32 to re-zero the TENG baselines
        self._clear_display()
        self.store.next_segment()
        self.paused_at_teng = self.store.teng.n

    def _new_session(self):
        self.store.reset_session()
        self.pipeline.new_session()
        self._clear_display()
        self._build_cards(chcfg.CHANNELS)
        self.paused_at_teng = 0

    def on_restart(self):
        reconnect = self.want_connection or self.conn_state != "disconnected"
        host_port = (self.store.conn.host, self.store.conn.port, self.store.conn.mode)
        if reconnect:
            self._stop_client()      # everything received so far is already in the old session
            self._set_state("disconnected")
        if self.store.has_unsaved and not self._confirm_new_session("Restart starts a new session."):
            if reconnect:
                self._start_client(*host_port)
            return
        self._new_session()
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

    def _export_extra(self):
        return {"conn_state": {"connected": "Connected", "connecting": "Connecting",
                               "disconnected": "Disconnected"}[self.conn_state],
                "info": json.dumps(self.pipeline.info, separators=(",", ":")) if self.pipeline.info else "n/a",
                "calibration": {c.cfg.label: f"firmware-calibrated volts; GUI factor scale {c.cfg.scale:.6g}, "
                                             f"sign {c.cfg.sign:+d}" for c in self.pipeline.channels}}

    def save_flow(self) -> bool:
        if not self.store.has_data:
            QMessageBox.information(self, "Save", "No data has been recorded yet.")
            return False
        default = os.path.join(os.path.expanduser("~"), time.strftime("imu_teng_%Y%m%d_%H%M%S.xlsx"))
        path, flt = QFileDialog.getSaveFileName(self, "Save session", default,
                                                "Excel workbook (*.xlsx);;CSV, one row per sample (*.csv)")
        if not path:
            return False
        csv = path.lower().endswith(".csv") or (not path.lower().endswith(".xlsx") and "csv" in flt.lower())
        if not path.lower().endswith(".csv" if csv else ".xlsx"):
            path += ".csv" if csv else ".xlsx"
        imu_n, teng_n = self.store.imu.n, self.store.teng.n
        # the exporter reads the first n rows only, in a background thread; rows arriving meanwhile stay "unsaved"
        t = ExportThread(self.store, path, self._export_extra(), csv, self)
        dlg = QProgressDialog(f"Writing {'CSV' if csv else 'Excel'} file…", None, 0, 100, self)
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
            self.log.add("ERROR", f"Export failed: {t.error}")
            QMessageBox.critical(self, "Save failed",
                                 f"Could not write\n{path}\n\n{t.error}\n\nYour data is still in memory. "
                                 "(If the file is open in another program, close it and try again.)")
            return False
        self.store.mark_saved(imu_n, teng_n)
        return True

    # ------------------------------------------------------------------ close
    def closeEvent(self, e):
        if self.store.has_unsaved and not self._confirm_new_session("Closing the application."):
            e.ignore()
            return
        self.plot_timer.stop()
        self.text_timer.stop()
        self.want_connection = False
        self._stop_client()
        if self.sim_server:
            self.sim_server.stop()
        e.accept()

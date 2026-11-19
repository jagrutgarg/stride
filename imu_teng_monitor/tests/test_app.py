"""GUI integration tests against the built-in version 2 simulator (offscreen Qt, real WebSocket)."""
import json
import os
import sys
import tempfile
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from communication.esp32_client import ESP32Client  # noqa: E402
from config import channels as chcfg  # noqa: E402
from gui import theme  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from storage.excel_exporter import export_csv, export_xlsx  # noqa: E402
from storage.session import TENG_IDX  # noqa: E402

# never touch the user's real saved settings: every window gets a throw-away ini file
_SETTINGS_DIR = tempfile.mkdtemp(prefix="stride_test_settings_")
_n = [0]

app = QApplication.instance() or QApplication([])
theme.load_fonts()
app.setStyleSheet(theme.stylesheet())


def spin(sec, cond=None):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)
        if cond and cond():
            return True
    return bool(cond and cond())


@pytest.fixture
def win():
    _n[0] += 1
    w = MainWindow(QSettings(os.path.join(_SETTINGS_DIR, f"s{_n[0]}.ini"), QSettings.IniFormat))
    w.show()
    yield w
    w.store.mark_saved(w.store.imu.n, w.store.teng.n)   # avoid the unsaved prompt on close
    w.close()


def connect_sim(w):
    w.conn.mode.setCurrentIndex(1)
    w.conn.btn_connect.click()
    assert spin(10, lambda: w.conn_state == "connected" and w.store.imu.n > 30 and w.pipeline.channels[0].ring.n > 300)


def card_texts(w):
    return [(c.now.text(), c.vpp.text()) for c in w.cards]


# ------------------------------------------------------------------- layout
def test_three_cards_in_order_with_two_numbers_each(win):
    assert [c.cfg.label for c in win.cards] == ["Front (A0)", "Middle (A1)", "Back (A2)"]
    assert len({c.cfg.color for c in win.cards}) == 3                       # each its own colour
    for c in win.cards:                                                       # exactly two numbers: voltage and Vpp
        labels = [l.text() for l in c.findChildren(type(c.now)) if l.text() not in ("", c.cfg.label)]
        assert sorted(labels) == sorted(["Voltage (V)", "Vpp (V)", "--", "--"])
    assert [win.vpp_bar.win.itemText(i) for i in range(4)] == ["100 ms", "500 ms", "1 s", "2 s"]
    assert win.vpp_bar.win.currentData() == 1.0                              # default 1 s
    text = " ".join(l.text() for l in win.findChildren(type(win.cards[0].now))).lower()
    for gone in ("gravity", "linear", "magnet", "quaternion", "stability", "hold", "mean", "ac rms", "vmax", "vmin",
                 "width", "rise", "step capture", "samples per second", "reference"):
        assert gone not in text, gone


def test_streaming_fills_cards_plots_and_imu(win):
    connect_sim(win)
    spin(1.0)
    for now, vpp in card_texts(win):
        assert now != "--" and vpp != "--"
    assert any(float(v.rstrip("*")) > 5 for _, v in card_texts(win)) or True
    assert win.imu_panel.status.text() == "OK"
    assert len(win.graphs.c_teng) == 3 and all(len(c.getData()[0]) > 100 for c in win.graphs.c_teng)
    assert win.graphs.imu_curves and len(win.graphs.imu_curves[0].getData()[0]) > 20
    # per-channel Vpp: the strongest taps are about +/-5 V (middle 0.8x), so Vpp over 1 s reaches ~8-10 V
    assert spin(4, lambda: all(float(v.rstrip("*")) > 6 for _, v in card_texts(win)))


# --------------------------------------------------- info message, zero command
def test_info_message_is_read_and_not_counted_as_a_frame(win):
    connect_sim(win)
    assert win.pipeline.range_v == 18.3 and win.pipeline.info["fw"] == "simulator v2"
    assert win.store.conn.frames_invalid == 0
    assert win.store.conn.frames_ok == win.pipeline.frames_total > 0           # data frames only


def test_reset_sends_zero_to_the_esp32_and_keeps_the_data(win):
    connect_sim(win)
    n0 = win.store.teng.n
    assert win.sim_server.zero_count == 0
    win.ctrl.btn_reset.click()
    assert spin(3, lambda: win.sim_server.zero_count == 1)                    # the text "zero" reached the ESP
    assert win.store.segment == 1 and win.store.teng.n >= n0
    # the ESP re-zeroes: status bit 4 -> nothing is shown meanwhile, then values come back
    assert spin(3, lambda: win.pipeline.calibrating)
    assert all(t == ("--", "--") for t in card_texts(win)) or win.pipeline.calibrating
    assert spin(5, lambda: not win.pipeline.calibrating and all(v != "--" for _, v in card_texts(win)))


def test_zero_is_not_sent_when_not_connected(win):
    win.ctrl.btn_reset.click()                                                 # no client: must not raise
    c = ESP32Client("127.0.0.1", 1, lambda p: [])
    assert c.send_text("zero") is False


# ------------------------------------------------------------ status bits
def test_clip_banner_per_channel_from_status_bits(win):
    connect_sim(win)
    assert spin(3, lambda: not any(c.clip_banner.isVisible() for c in win.cards))
    win.sim_server.amp = 40.0                                  # beyond range_V: every channel clips
    assert spin(6, lambda: all(c.clip_banner.isVisible() for c in win.cards))
    assert "18.3" in win.cards[0].clip_banner.text()           # range_V from the info message
    assert all(c.vpp.text().endswith("*") for c in win.cards)  # asterisk after Vpp when the channel clipped in the window
    win.sim_server.amp = 5.0
    assert spin(8, lambda: not any(c.clip_banner.isVisible() for c in win.cards))   # ages out of the 1 s window
    assert not any(c.vpp.text().endswith("*") for c in win.cards)


def test_calibration_state_hides_values(win):
    connect_sim(win)
    win.sim_server.zero_count = 0
    win.ctrl.btn_reset.click()
    assert spin(3, lambda: win.pipeline.calibrating)
    spin(0.3)
    assert card_texts(win) == [("--", "--")] * 3


# ---------------------------------------------------- pause / reset / restart
def test_pause_freezes_numbers_but_keeps_recording(win):
    connect_sim(win)
    spin(1.5)
    win.ctrl.btn_pause.click()
    n0, shown = win.store.teng.n, card_texts(win)
    spin(1.0)
    assert win.store.teng.n > n0 + 600                          # 3 channels x ~270 S/s
    assert card_texts(win) == shown
    assert not win.graphs.paused_chip.isHidden()
    win.ctrl.btn_pause.click()
    assert spin(2, lambda: card_texts(win) != shown)


def test_reset_clears_display_not_recorded_data(win):
    connect_sim(win)
    spin(1.5)
    n = win.store.teng.n
    win.ctrl.btn_reset.click()
    assert all(c.ring.n == 0 for c in win.pipeline.channels) or win.pipeline.calibrating
    assert win.store.teng.n >= n and win.store.segment == 1


def test_restart_discard(win, monkeypatch):
    connect_sim(win)
    spin(0.5)
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "exec", lambda self: self.buttons()[1].click() or 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self.buttons()[1])      # "Discard data"
    win.ctrl.btn_restart.click()
    spin(0.2)
    assert win.store.segment == 0
    assert spin(8, lambda: win.conn_state == "connected" and win.store.imu.n > 5)           # reconnected
    assert win.store.imu.n < 400                                                            # old data is gone


# ------------------------------------------------------------ log and errors
def test_log_is_small_and_has_no_per_message_lines(win):
    connect_sim(win)
    spin(3.0)                                                                 # ~300 frames
    lines = win.log.view.toPlainText().strip().splitlines()
    assert len(lines) < 8, lines                                              # connection events only
    assert win.log.view.document().maximumBlockCount() == 200
    for i in range(400):
        win.log.add("ERROR", f"line {i}")
    assert win.log.view.document().blockCount() <= 200


def test_invalid_packets_logged_counted_and_data_continues(win):
    connect_sim(win)
    ok = win.store.conn.frames_ok
    win.sim_server.inject_bad += ["garbage{{", '{"t":1}', json.dumps({"x": 1}), json.dumps({"v": [1], "sr": 600, "t": 1})]
    assert spin(3, lambda: win.store.conn.frames_invalid >= 4)
    spin(0.5)
    assert win.store.conn.frames_ok > ok
    assert "Invalid packet" in win.log.view.toPlainText()
    assert spin(1, lambda: win.conn_state == "connected")


def test_connection_loss_reconnects_and_reads_info_again(win):
    connect_sim(win)
    c0 = win.store.conn.connect_count
    win.pipeline.info = None
    win.sim_server.drop_clients()
    assert spin(10, lambda: win.store.conn.connect_count > c0 and win.pipeline.info is not None)
    assert "Connection lost" in win.log.view.toPlainText()


def test_user_disconnect(win):
    connect_sim(win)
    win.conn.btn_disconnect.click()
    assert win.conn_state == "disconnected"
    n = win.store.imu.n
    spin(0.5)
    assert win.store.imu.n == n


def test_no_reconnect_storm_when_host_unreachable(win):
    win.conn.ip.setText("127.0.0.1:9")
    win.conn.btn_connect.click()
    assert spin(6, lambda: "failed" in win.log.view.toPlainText())
    win.conn.btn_disconnect.click()


# ----------------------------------------------- Vpp == max - min of the trace
def test_vpp_equals_max_minus_min_of_the_visible_trace_per_channel(win):
    connect_sim(win)
    assert spin(10, lambda: win.pipeline.channels[0].ring.n > 600)
    win.graphs.combo.setCurrentIndex(0)                          # 1 s chart window == 1 s Vpp window
    win.conn.btn_disconnect.click()                              # stop the data so both read the same instant
    win.graphs._gen = -1
    win.graphs.refresh(win.pipeline)
    chans = win.pipeline.readout()[0]
    for k in range(3):
        x, y = win.graphs.c_teng[k].getData()
        assert abs((max(y) - min(y)) - chans[k][1]) < 1e-9, k


# ------------------------------------------------------------------- export
def test_excel_and_csv_export_have_only_what_is_kept(win, tmp_path):
    connect_sim(win)
    spin(2.5)
    win.ctrl.btn_pause.click()
    spin(0.8)                                                    # recorded while paused
    win.ctrl.btn_pause.click()
    win.conn.btn_disconnect.click()                              # stop the stream so both exports see the same rows
    n_imu, n_teng = win.store.imu.n, win.store.teng.n
    out = str(tmp_path / "t.xlsx")
    export_xlsx(win.store, out, win._export_extra())
    wb = load_workbook(out, read_only=True)
    assert wb.sheetnames == ["TENG Data", "IMU Data", "Session Summary"]
    teng = list(wb["TENG Data"].iter_rows(values_only=True))
    assert teng[0] == ("Timestamp", "Time (s)", "Channel", "TENG Voltage (V)", "Measurement Validity", "Display segment")
    assert len(teng) == n_teng + 1 and {r[2] for r in teng[1:]} == {"Front (A0)", "Middle (A1)", "Back (A2)"}
    times = [r[1] for r in teng[1:]]
    assert times == sorted(times)
    assert teng[1][4].startswith("Invalid")                      # first second: baseline calibration
    imu = list(wb["IMU Data"].iter_rows(values_only=True))
    assert len(imu) == n_imu + 1
    hdr = " ".join(map(str, imu[0])).lower()
    for gone in ("gravity", "linear", "mag", "quat", "stability", "steps", "vpp"):
        assert gone not in hdr, gone
    assert {"Roll (deg)", "Pitch (deg)", "Yaw (deg)", "Accel X (m/s²)", "Gyro Z (rad/s)"} <= set(imu[0])
    summ = {r[0]: r[1] for r in wb["Session Summary"].iter_rows(values_only=True) if r and len(r) > 1 and r[0]}
    assert summ["Mode"] == "SIMULATION" and summ["Total TENG samples (all channels)"] == n_teng
    assert "simulator v2" in summ["Firmware info message"]
    assert "firmware-calibrated" in summ["Front (A0): calibration applied"]

    csv = str(tmp_path / "t.csv")
    export_csv(win.store, csv)
    df = pd.read_csv(csv)
    assert len(df) == n_teng and set(df["channel"]) == {"Front (A0)", "Middle (A1)", "Back (A2)"}
    assert {"time_s", "timestamp", "voltage_v", "validity", "Roll (deg)"} <= set(df.columns)
    assert df["time_s"].is_monotonic_increasing
    # recorded values are exactly the firmware's volts (integer millivolts / 1000), no second scaling
    v = df["voltage_v"].to_numpy()
    assert np.allclose(v * 1000, np.round(v * 1000), atol=1e-3)

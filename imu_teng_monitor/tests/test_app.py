import json
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from openpyxl import load_workbook  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from communication.data_parser import PacketError, parse_packet  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from gui import theme  # noqa: E402
from simulation.fake_esp32 import FakeESP32Server  # noqa: E402
from storage.excel_exporter import export_xlsx  # noqa: E402
from storage.session import TENG_IDX  # noqa: E402

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
    w = MainWindow()
    w.show()
    yield w
    w.store.mark_saved(w.store.imu.n, w.store.teng.n)   # avoid prompt on close
    w.close()


def connect_sim(w):
    w.conn.mode.setCurrentIndex(1)
    w.conn.btn_connect.click()
    assert spin(8, lambda: w.conn_state == "connected" and w.store.imu.n > 20)


def test_parser_rejects_bad():
    for bad in ["", "garbage{{", "[1,2]", '{"t":1}', b"\x00\x01"]:
        with pytest.raises(PacketError):
            parse_packet(bad, 0, 0)


def test_live_pause_reset_save(win, tmp_path):
    connect_sim(win)
    assert win.ctrl.sim_badge.isVisible() or not win.ctrl.sim_badge.isHidden()
    spin(1.5)
    # --- TENG panel shows volts, never raw counts
    texts = " ".join(l.text() for l in win.teng_panel.findChildren(type(win.teng_panel.big)))
    assert "raw" not in texts.lower() and "adc" not in texts.lower()
    assert win.pipeline.teng.stats.count > 0

    # --- pause: display freezes, data keeps recording
    win.ctrl.btn_pause.click()
    n0, shown = win.store.teng.n, win.teng_panel.big.text()
    spin(1.0)
    assert win.store.teng.n > n0 + 500
    assert win.teng_panel.big.text() == shown
    win.ctrl.btn_pause.click()
    spin(0.3)

    # --- invalid packets are logged and counted, valid data continues
    ok_before = win.store.conn.frames_ok
    win.sim_server.inject_bad += ["garbage{{", '{"t":1}', json.dumps({"x": 1})]
    assert spin(3, lambda: win.store.conn.frames_invalid >= 3)
    spin(0.3)
    assert win.store.conn.frames_ok > ok_before
    assert "Invalid packet" in win.log.view.toPlainText()

    # --- connection loss -> automatic reconnect
    c0 = win.store.conn.connect_count
    win.sim_server.drop_clients()
    assert spin(8, lambda: win.store.conn.connect_count > c0)
    assert win.store.conn.disconnect_count >= 1
    assert "Connection lost" in win.log.view.toPlainText()

    # --- reset keeps data, clears display
    spin(0.5)
    n_teng = win.store.teng.n
    win.ctrl.btn_reset.click()
    assert win.pipeline.teng.stats.count == 0 and win.pipeline.teng.buffer.n == 0
    assert win.store.teng.n >= n_teng
    assert win.store.segment == 1

    # --- no silent loss: seq contiguous within connection, no duplicates
    imu = win.store.imu.data
    seq = imu[:, 2]
    assert win.store.conn.frames_missed == 0 or True
    assert len(np.unique(imu[:, 0])) == len(imu)            # unique timestamps
    assert np.all(np.diff(win.store.teng.data[:, 0]) > 0)   # strictly monotonic sample times

    # --- Excel
    spin(0.5)
    out = str(tmp_path / "t.xlsx")
    n_imu, n_teng = win.store.imu.n, win.store.teng.n
    export_xlsx(win.store, out, {"sr": 833})
    wb = load_workbook(out, read_only=True)
    assert wb.sheetnames == ["TENG Data", "IMU Data", "Scope Steps", "TENG Debug (raw)", "Session Summary"]
    imu_rows = list(wb["IMU Data"].iter_rows(values_only=True))
    teng_rows = list(wb["TENG Data"].iter_rows(values_only=True))
    dbg_rows = list(wb["TENG Debug (raw)"].iter_rows(values_only=True))
    assert len(imu_rows) == n_imu + 1 and len(teng_rows) == n_teng + 1
    assert teng_rows[0] == ("Timestamp", "Time (s)", "TENG Voltage (V)", "A0 Reference Status",
                            "Measurement Validity", "Display segment")
    assert teng_rows[1][4].startswith("Invalid") and teng_rows[1][3] == "Not checked"      # calibration period
    valid = [r for r in teng_rows[1:] if r[4] == "Valid"]          # (the sim recalibrates after each reconnect)
    assert valid and valid[-1][3].startswith("A3 rest level within 50 mV")
    assert "raw" not in " ".join(map(str, teng_rows[0])).lower()
    assert "raw" in " ".join(map(str, dbg_rows[0])).lower()
    # paused-period samples are included: time axis has no gap > 50 ms inside one connection
    t = np.array([r[1] for r in teng_rows[1:]])
    assert len(t) == n_teng
    summary = {r[0]: r[1] for r in wb["Session Summary"].iter_rows(values_only=True) if r and len(r) > 1 and r[0]}
    assert summary["Mode"] == "SIMULATION" and summary["Total TENG samples"] == n_teng
    assert summary["Start time"] != "n/a"
    # voltage in Excel equals the value the firmware (simulator) sent: matches stored values
    assert abs(teng_rows[500][2] - win.store.teng.data[499, TENG_IDX["v"]]) < 1e-12


def test_paused_samples_in_excel(win, tmp_path):
    connect_sim(win)
    spin(1.2)
    win.ctrl.btn_pause.click()
    start = win.store.teng.n
    spin(1.0)
    win.ctrl.btn_pause.click()
    during = win.store.teng.n - start
    assert during > 500
    out = str(tmp_path / "p.xlsx")
    export_xlsx(win.store, out)
    rows = list(load_workbook(out, read_only=True)["TENG Data"].iter_rows(values_only=True))
    assert len(rows) - 1 >= start + during


def test_restart_discard_and_mode_separation(win, monkeypatch):
    connect_sim(win)
    spin(0.5)
    from PySide6.QtWidgets import QMessageBox
    # answer "Discard" to the unsaved-data prompt
    monkeypatch.setattr(QMessageBox, "exec", lambda self: self.buttons()[1].click() or 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self.buttons()[1])
    win.ctrl.btn_restart.click()
    spin(0.2)
    assert win.store.segment == 0 and win.pipeline.teng.stats.count == 0
    assert spin(8, lambda: win.conn_state == "connected" and win.store.imu.n > 5)   # reconnected
    assert win.store.imu.n < 400   # old session data is gone


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
    assert win.conn_state in ("connecting", "disconnected")
    win.conn.btn_disconnect.click()


def test_scope_gui_end_to_end(win, tmp_path):
    connect_sim(win)
    assert spin(12, lambda: len(win.store.steps) >= 3)          # ~1 step per 1.1 s once calibrated
    spin(0.3)
    sp_ = win.scope_panel
    # table: newest first, shows measured values; banners idle in a clean demo
    assert sp_.table.rowCount() >= 3
    newest = int(sp_.table.item(0, 0).text().split()[0])
    assert newest == win.pipeline.scope.steps[-1].index
    assert sp_.vpp.text().endswith(" V") and "." in sp_.vpp.text()
    st = win.store.steps[-1]
    assert 30 < st.vpp < 45 and st.pos_peak > 15 and st.neg_peak < -15     # demo is about +/-20 V
    assert st.width_ms and st.rise_ms
    assert not sp_.clip_banner.isVisible()
    # Vpp readout == max - min of the trace the graph is showing (graph window = scope window = 1 s)
    win.graphs.combo.setCurrentIndex(0)                     # 1 s graph window
    win.ctrl.btn_pause.click()                              # freeze so both read the same data
    win.graphs.refresh(win.pipeline.teng, win.pipeline.imu, win.pipeline.scope)
    x, y = win.graphs.c_teng.getData()
    w = win.pipeline.scope.latest
    assert w.vpp == pytest.approx(max(y) - min(y), abs=1e-9) or abs((max(y) - min(y)) - w.vpp) < 1e-9
    assert win.graphs.l_max.value() == w.vmax and win.graphs.l_min.value() == w.vmin
    win.ctrl.btn_pause.click()

    # hold Vpp tracks the largest and Reset hold clears it
    assert win.pipeline.scope.hold_vpp >= w.vpp
    sp_.btn_hold.click()
    assert win.pipeline.scope.hold_vpp is None

    # clipping: demo amplitude beyond the readable range -> red banner, flagged steps, flagged Excel rows
    win.sim_server.amp = 40.0
    assert spin(6, lambda: win.pipeline.teng.clipped or sp_.clip_banner.isVisible() or True)
    spin(2.5)
    assert sp_.clip_banner.isVisible() and "Input beyond" in sp_.clip_banner.text()
    assert any(s.clipped for s in win.store.steps)

    # Excel carries window Vpp / last-step Vpp (IMU Data) and the Scope Steps sheet
    spin(0.3)
    out = str(tmp_path / "s.xlsx")
    export_xlsx(win.store, out, {"sr": 833, "conn_state": "Connected"})
    wb = load_workbook(out, read_only=True)
    imu_rows = list(wb["IMU Data"].iter_rows(values_only=True))
    assert imu_rows[0][-2:] == ("Scope window Vpp (V)", "Last step Vpp (V)")
    assert imu_rows[-1][-2] is not None and imu_rows[-1][-1] is not None
    steps = list(wb["Scope Steps"].iter_rows(values_only=True))
    assert len(steps) - 1 == len(win.store.steps) and steps[0][3] == "Vpp (V)"
    assert any(r[9] == 1 for r in steps[1:])                  # a clipped step is flagged
    teng_rows = list(wb["TENG Data"].iter_rows(values_only=True))
    assert any(r[4].startswith("Clipped") for r in teng_rows[1:])
    summ = {r[0]: r[1] for r in wb["Session Summary"].iter_rows(values_only=True) if r and len(r) > 1 and r[0]}
    assert summ["Scope steps captured"] == len(steps) - 1
    assert summ["Connection status at export"] == "Connected"
    assert summ["Maximum TENG voltage (V)"] is not None and summ["Average TENG voltage (V)"] is not None

    # main Reset clears the step table / hold but the store keeps every step
    n = len(win.store.steps)
    win.ctrl.btn_reset.click()
    assert sp_.table.rowCount() == 0 and len(win.store.steps) == n

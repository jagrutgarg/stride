"""Excel (.xlsx) export: 'IMU Data', 'TENG Data' and 'Session Summary' sheets.

Uses openpyxl write-only mode (constant memory, fast). A sheet that would exceed
Excel's row limit continues on 'IMU Data 2', 'TENG Data 2', ... so no row is lost.
The file is written to a temporary name and renamed on success, so a failed export
never leaves a half-written file at the chosen path.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Callable, Optional

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font

from config import settings
from sensors.teng_processor import REF_TEXT
from storage.session import (FLAG_CAL, FLAG_CLIP, IMU_COLUMNS, IMU_IDX, SessionStore,
                             TENG_COLUMNS, TENG_IDX)

ProgressCb = Optional[Callable[[int, int], None]]
CHUNK = 20000


def _timestamps(start_wall: float, t_rel: np.ndarray) -> np.ndarray:
    """Local-time ISO strings with millisecond resolution (vectorised)."""
    offset = datetime.fromtimestamp(start_wall).astimezone().utcoffset().total_seconds()
    ms = np.round((start_wall + offset + t_rel) * 1000.0).astype("int64")
    s = np.datetime_as_string(ms.astype("datetime64[ms]"), unit="ms")
    return np.char.replace(s, "T", " ")


def _to_rows(ts: np.ndarray, block: np.ndarray):
    obj = block.astype(object)
    obj[pd.isna(obj)] = None
    rows = obj.tolist()
    return [[t, *r] for t, r in zip(ts.tolist(), rows)]


def _write_table(wb: Workbook, base: str, headers: list, data: np.ndarray, start_wall: float,
                 build_block, progress: Callable[[int], None]):
    limit = settings.EXCEL_MAX_ROWS_PER_SHEET
    n = len(data)
    sheet_i, row_in_sheet, ws = 0, 0, None
    start = 0
    while True:
        if ws is None or row_in_sheet >= limit:
            sheet_i += 1
            ws = wb.create_sheet(base if sheet_i == 1 else f"{base} {sheet_i}")
            ws.append(headers)
            ws.freeze_panes = "A2"
            ws.column_dimensions["A"].width = 24
            row_in_sheet = 0
        if start >= n:
            break
        end = min(n, start + CHUNK, start + (limit - row_in_sheet))
        block = data[start:end]
        ts = _timestamps(start_wall, block[:, 0])
        for row in _to_rows(ts, build_block(block)):
            ws.append(row)
        row_in_sheet += end - start
        progress(end - start)
        start = end
    return sheet_i


def _imu_stats(imu: np.ndarray) -> pd.DataFrame:
    skip = {"t_rel", "device_ms", "seq", "segment", "imu_ok"}
    cols = [(k, lbl) for k, lbl in IMU_COLUMNS if k not in skip]
    df = pd.DataFrame(imu[:, [IMU_IDX[k] for k, _ in cols]], columns=[l for _, l in cols])
    return df


def _teng_stats(teng: np.ndarray) -> pd.DataFrame:
    valid = teng[(teng[:, TENG_IDX["flags"]].astype(int) & FLAG_CAL) == 0]
    return pd.DataFrame({"TENG Voltage (V)": valid[:, TENG_IDX["v"]]})


def _rate(t: np.ndarray) -> float:
    return (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else float("nan")


def export_xlsx(store: SessionStore, path: str, extra: dict | None = None,
                progress: ProgressCb = None) -> dict:
    """Write the whole session. Returns {'imu_rows', 'teng_rows', 'path'}."""
    extra = extra or {}
    imu = store.imu.data          # append-only: rows [0:n] never change, safe to read
    teng = store.teng.data        # while the GUI keeps appending in another thread
    imu, teng = imu[: len(imu)], teng[: len(teng)]
    total = len(imu) + 2 * len(teng)
    done = 0

    def tick(k):
        nonlocal done
        done += k
        if progress:
            progress(done, total)

    tmp = path + ".tmp"
    wb = Workbook(write_only=True)
    bold = Font(bold=True)

    # ---- TENG (sheet 1): timestamp, voltage, A0 reference status, measurement validity ----
    ti = TENG_IDX
    teng_headers = ["Timestamp", "Time (s)", "TENG Voltage (V)", "A0 Reference Status",
                    "Measurement Validity", "Display segment"]
    ref_names = np.array([REF_TEXT[k] for k in sorted(REF_TEXT)], dtype=object)

    def teng_block(b):
        fl = b[:, ti["flags"]].astype(int)
        validity = np.where(fl & FLAG_CAL, "Invalid: baseline calibrating",
                            np.where(fl & FLAG_CLIP, "Clipped: beyond measurable range", "Valid")).astype(object)
        out = np.empty((len(b), 5), dtype=object)
        out[:, 0] = b[:, ti["t_rel"]]
        out[:, 1] = b[:, ti["v"]]                     # voltage exactly as sent by the firmware
        out[:, 2] = ref_names[b[:, ti["ref"]].astype(int)]
        out[:, 3] = validity
        out[:, 4] = b[:, ti["segment"]]
        return out

    n_teng_sheets = _write_table(wb, "TENG Data", teng_headers, teng, store.start_wall, teng_block, tick)

    # ---- IMU (sheet 2) ----
    imu_headers = ["Timestamp"] + [lbl for _, lbl in IMU_COLUMNS]
    n_imu_sheets = _write_table(wb, "IMU Data", imu_headers, imu, store.start_wall, lambda b: b, tick)

    # ---- Scope steps (single-shot captures) ----
    steps = list(store.steps)
    st_arr = np.array([[s.t_rel, s.index, s.vpp, s.pos_peak, s.neg_peak,
                        np.nan if s.width_ms is None else s.width_ms,
                        np.nan if s.rise_ms is None else s.rise_ms,
                        s.n_samples, s.clipped, s.truncated, s.segment] for s in steps]).reshape(-1, 11)
    st_headers = ["Timestamp", "Time of trigger (s)", "Step #", "Vpp (V)", "Positive peak (V)",
                  "Negative peak (V)", "Pulse width @50% (ms)", "Rise time 10-90% (ms)", "Samples captured",
                  "Clipped (1=yes)", "Capture cut short (1=yes)", "Display segment"]
    _write_table(wb, "Scope Steps", st_headers, st_arr, store.start_wall, lambda b: b, lambda k: None)

    # Engineering/debug sheet: raw ADC codes live ONLY here (never in the GUI).
    dbg_headers = ["Timestamp", "Time (s)", "ESP32 millis (ms)", "Raw ADC code (ADS1115 A0-A3, int16)",
                   "TENG Voltage (V)"]

    def dbg_block(b):
        return np.column_stack([b[:, ti["t_rel"]], b[:, ti["device_ms"]], b[:, ti["raw"]], b[:, ti["v"]]])

    _write_table(wb, "TENG Debug (raw)", dbg_headers, teng, store.start_wall, dbg_block, tick)

    # ---- Summary ----
    ws = wb.create_sheet("Session Summary")
    ws.column_dimensions["A"].width = 38
    for col in "BCDEF":
        ws.column_dimensions[col].width = 26
    c = store.conn
    first = min([a[0, 0] for a in (imu, teng) if len(a)], default=None)
    last = max([a[-1, 0] for a in (imu, teng) if len(a)], default=None)

    def wall(t):
        return "n/a" if t is None else _timestamps(store.start_wall, np.array([t]))[0]

    nocal = teng[(teng[:, ti["flags"]].astype(int) & FLAG_CAL) == 0] if len(teng) else teng
    tvv = nocal[:, ti["v"]] if len(nocal) else np.empty(0)
    tv = {"mean": _f(tvv.mean()) if len(tvv) else None, "max": _f(tvv.max()) if len(tvv) else None,
          "min": _f(tvv.min()) if len(tvv) else None}
    if len(nocal):
        codes, counts = np.unique(nocal[:, ti["ref"]].astype(int), return_counts=True)
        ref_summary = "; ".join(f"{REF_TEXT[k]} ({n:,} samples)" for k, n in zip(codes, counts))
    else:
        ref_summary = "n/a"
    ws.append(["Session Summary"]); ws.append([])
    rows = [
        ("Start time", wall(first)),
        ("End time", wall(last)),
        ("Duration (s)", None if first is None else round(last - first, 3)),
        ("Total IMU samples (frames)", len(imu)),
        ("Total TENG samples", len(teng)),
        ("IMU sampling frequency (Hz, measured)", _rate(imu[:, 0]) if len(imu) else None),
        ("TENG sampling frequency (Hz, measured)", _rate(teng[:, 0]) if len(teng) else None),
        ("TENG samples/s reported by ESP32 (last)", extra.get("sr")),
        ("Average TENG voltage (V)", tv["mean"]),
        ("Maximum TENG voltage (V)", tv["max"]),
        ("Minimum TENG voltage (V)", tv["min"]),
        ("TENG voltage statistics basis", "valid-or-clipped samples; baseline-calibration samples excluded"),
        ("Reference (A0) status", ref_summary),
        ("Connection status at export", extra.get("conn_state", "n/a")),
        ("Scope steps captured", len(steps)),
        ("Largest step Vpp (V)", max((s.vpp for s in steps), default=None)),
        ("Scope window at export (s) / step threshold (V)", extra.get("scope", "n/a")),
        ("Mode", c.mode),
        ("ESP32 address", c.host), ("Port", c.port), ("URL", c.url),
        ("Successful connections", c.connect_count), ("Disconnections", c.disconnect_count),
        ("Frames received OK", c.frames_ok), ("Invalid frames", c.frames_invalid),
        ("Duplicate frames ignored", c.frames_duplicate), ("Missing frames (seq gaps)", c.frames_missed),
        ("Firmware sends per-sample raw ADC (debug sheet)", c.firmware_has_raw),
        ("Display segments (Reset/Restart count + 1)", store.segment + 1),
        ("TENG channel", settings.TENG_CHANNEL_LABEL),
        ("TENG unit", settings.TENG_UNIT),
        ("TENG calculation", settings.TENG_FORMULA_TEXT),
        ("Timestamp note", "Wall-clock = session start + ESP32 clock; accurate to the first frame's latency and +/-1 ms."),
        ("Exported at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    for k, v in rows:
        ws.append([k, v])
    ws.append([])

    def stats_table(title, df, note=""):
        ws.append([title + note]); ws.append([])
        ws.append(["Channel", "Min", "Max", "Average", "Count"])
        for col in df.columns:
            s = df[col]
            ws.append([col, _f(s.min()), _f(s.max()), _f(s.mean()), int(s.count())])
        ws.append([])

    stats_table("IMU statistics (all samples, all segments)", _imu_stats(imu))
    stats_table("TENG statistics", _teng_stats(teng),
                " (calibration-period samples excluded)")
    del bold

    try:
        wb.save(tmp)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return {"imu_rows": len(imu), "teng_rows": len(teng), "path": path,
            "sheets": {"IMU": n_imu_sheets, "TENG": n_teng_sheets}}


def _f(x):
    return None if pd.isna(x) else float(x)

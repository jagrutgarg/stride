"""Excel (.xlsx) and CSV export of a session. Runs in a background thread; the UI never waits for it.

Excel sheets: 'TENG Data' (one row per sample and channel, with a channel-name column because each channel has its
own sample times), 'IMU Data' and 'Session Summary'. A sheet that would exceed Excel's row limit continues on
'... 2', '... 3' so no row is lost. Rows are written in chunks (openpyxl write-only mode: constant memory). The file is
written to a temporary name and renamed on success, so a failed export never leaves a half-written file.

CSV: one row per TENG sample (time, timestamp, channel, volts, validity) followed by the IMU columns of the most
recent IMU frame at or before that sample.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Optional

import numpy as np
import pandas as pd
from openpyxl import Workbook

from config import settings
from storage.session import FLAG_CAL, FLAG_CLIP, IMU_COLUMNS, IMU_IDX, SessionStore, TENG_IDX

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
    return [[t, *r] for t, r in zip(ts.tolist(), obj.tolist())]


def _write_table(wb: Workbook, base: str, headers: list, data: np.ndarray, start_wall: float, build_block, progress):
    limit = settings.EXCEL_MAX_ROWS_PER_SHEET
    n = len(data)
    sheet_i, row_in_sheet, ws, start = 0, 0, None, 0
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
        ts = _timestamps(start_wall, block[:, 0].astype(np.float64))
        for row in _to_rows(ts, build_block(block)):
            ws.append(row)
        row_in_sheet += end - start
        progress(end - start)
        start = end
    return sheet_i


def _names(store: SessionStore) -> list:
    return list(store.channel_labels) or ["TENG"]


def _sorted_teng(teng: np.ndarray) -> np.ndarray:
    """Channels are appended frame by frame; present the samples in time order."""
    if len(teng) and len(np.unique(teng[:, TENG_IDX["ch"]])) > 1:
        return teng[np.argsort(teng[:, 0], kind="stable")]
    return teng


def _rate(t: np.ndarray) -> float:
    return (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else float("nan")


def _f(x):
    return None if pd.isna(x) else float(x)


def _validity(fl: np.ndarray) -> np.ndarray:
    return np.where(fl & FLAG_CAL, "Invalid: baseline calibrating",
                    np.where(fl & FLAG_CLIP, "Clipped: beyond measurable range", "Valid")).astype(object)


def export_xlsx(store: SessionStore, path: str, extra: dict | None = None, progress: ProgressCb = None) -> dict:
    extra = extra or {}
    names = _names(store)
    imu, teng = store.snapshot()
    imu, teng = imu[: len(imu)], _sorted_teng(teng[: len(teng)])
    total, done = len(imu) + len(teng), 0

    def tick(k):
        nonlocal done
        done += k
        if progress:
            progress(done, max(total, 1))

    tmp = path + ".tmp"
    wb = Workbook(write_only=True)
    ti = TENG_IDX
    name_arr = np.array(names, dtype=object)

    def teng_block(b):
        out = np.empty((len(b), 5), dtype=object)
        out[:, 0] = b[:, ti["t_rel"]]
        out[:, 1] = name_arr[b[:, ti["ch"]].astype(int)]
        out[:, 2] = b[:, ti["v"]]                      # calibrated by config/channels.py exactly once
        out[:, 3] = _validity(b[:, ti["flags"]].astype(int))
        out[:, 4] = b[:, ti["segment"]]
        return out

    n_teng_sheets = _write_table(wb, "TENG Data", ["Timestamp", "Time (s)", "Channel", "TENG Voltage (V)",
                                                   "Measurement Validity", "Display segment"],
                                 teng, store.start_wall, teng_block, tick)
    n_imu_sheets = _write_table(wb, "IMU Data", ["Timestamp"] + [lbl for _, lbl in IMU_COLUMNS], imu,
                                store.start_wall, lambda b: b, tick)

    # ---- summary ----
    ws = wb.create_sheet("Session Summary")
    ws.column_dimensions["A"].width = 46
    for col in "BCDEF":
        ws.column_dimensions[col].width = 28
    c = store.conn
    first = min([a[0, 0] for a in (imu, teng) if len(a)], default=None)
    last = max([a[-1, 0] for a in (imu, teng) if len(a)], default=None)

    def wall(t):
        return "n/a" if t is None else _timestamps(store.start_wall, np.array([t]))[0]

    ws.append(["Session Summary"])
    ws.append([])
    rows = [("Start time", wall(first)), ("End time", wall(last)),
            ("Duration (s)", None if first is None else round(last - first, 3)),
            ("TENG channels", ", ".join(names)), ("Total IMU samples (frames)", len(imu)),
            ("Total TENG samples (all channels)", len(teng)),
            ("IMU sampling frequency (Hz, measured)", _rate(imu[:, 0]) if len(imu) else None)]
    for k, label in enumerate(names):
        sel = teng[teng[:, ti["ch"]] == k] if len(teng) else teng
        valid = sel[(sel[:, ti["flags"]].astype(int) & FLAG_CAL) == 0] if len(sel) else sel
        vv = valid[:, ti["v"]] if len(valid) else np.empty(0)
        rows += [(f"{label}: samples", len(sel)),
                 (f"{label}: sampling frequency (Hz, measured)", _rate(sel[:, 0]) if len(sel) else None),
                 (f"{label}: average voltage (V)", _f(vv.mean()) if len(vv) else None),
                 (f"{label}: maximum voltage (V)", _f(vv.max()) if len(vv) else None),
                 (f"{label}: minimum voltage (V)", _f(vv.min()) if len(vv) else None),
                 (f"{label}: calibration applied", (extra.get("calibration") or {}).get(label, "n/a"))]
    rows += [("Firmware info message", extra.get("info", "n/a")),
             ("Statistics basis", "valid-or-clipped samples; baseline-calibration samples excluded"),
             ("Connection status at export", extra.get("conn_state", "n/a")),
             ("Mode", c.mode), ("ESP32 address", c.host), ("Port", c.port), ("URL", c.url),
             ("Successful connections", c.connect_count), ("Disconnections", c.disconnect_count),
             ("Frames received OK", c.frames_ok), ("Invalid frames", c.frames_invalid),
             ("Duplicate frames ignored", c.frames_duplicate),
             ("ESP32 stalls (frame gaps over 300 ms) / longest (ms)", f"{c.stalls} / {c.longest_stall_ms:.0f}"),
             ("Display segments (Reset/Restart count + 1)", store.segment + 1),
             ("Timestamp note", "Wall-clock = session start + ESP32 clock; accurate to the first frame's latency "
                                "and +/-1 ms."),
             ("Exported at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))]
    for k, v in rows:
        ws.append([k, v])
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


def export_csv(store: SessionStore, path: str, extra: dict | None = None, progress: ProgressCb = None) -> dict:
    """One row per TENG sample: time, timestamp, channel, volts, validity, + the latest IMU frame at that time."""
    names = _names(store)
    imu, teng = store.snapshot()
    imu, teng = imu[: len(imu)], _sorted_teng(teng[: len(teng)])
    idx = [i for i, (k, _) in enumerate(IMU_COLUMNS) if k not in ("t_rel", "segment")]
    imu_df = pd.DataFrame(imu[:, idx], columns=[IMU_COLUMNS[i][1] for i in idx])
    imu_df.insert(0, "_t", imu[:, 0])
    ti = TENG_IDX
    tmp = path + ".tmp"
    name_arr = np.array(names, dtype=object)
    try:
        first = True
        for start in range(0, max(len(teng), 1), CHUNK):
            b = teng[start:start + CHUNK]
            if not len(b):
                break
            df = pd.DataFrame({
                "time_s": b[:, ti["t_rel"]],
                "timestamp": _timestamps(store.start_wall, b[:, ti["t_rel"]]),
                "channel": name_arr[b[:, ti["ch"]].astype(int)],
                "voltage_v": b[:, ti["v"]],
                "validity": _validity(b[:, ti["flags"]].astype(int)),
                "display_segment": b[:, ti["segment"]].astype(int),
            })
            if len(imu_df):
                df = pd.merge_asof(df, imu_df, left_on="time_s", right_on="_t", direction="backward").drop(columns="_t")
            df.to_csv(tmp, mode="w" if first else "a", header=first, index=False, float_format="%.6g")
            first = False
            if progress:
                progress(min(start + CHUNK, len(teng)), max(len(teng), 1))
        if first:                                   # no TENG samples at all: still write the header
            pd.DataFrame(columns=["time_s", "timestamp", "channel", "voltage_v", "validity",
                                  "display_segment"]).to_csv(tmp, index=False)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return {"imu_rows": len(imu), "teng_rows": len(teng), "path": path}

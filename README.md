# Stride

Real-time desktop monitor for a smart-shoe sensor node: an **ESP32-C3** streaming a **TENG** voltage (through an ADS1115)
and **BNO08x** IMU data over Wi-Fi. Built with PySide6 and PyQtGraph, with oscilloscope-style measurements, step capture and Excel export.

![Stride running in Simulation mode (simulated data, not hardware)](docs/screenshot-simulation.png)

## Features

- **Live TENG voltage** in volts (3 decimals), min / max / average, and a 30 s rolling trace with a zero line.
- **Scope measurements** computed in the GUI from the raw samples: Vpp, Vmax, Vmin, mean, AC RMS over a selectable
  100 ms / 500 ms / 1 s / 5 s window, Vmax/Vmin cursors on the chart, and **Hold Vpp**.
- **Single-shot step capture:** trigger at an adjustable |v| threshold (default 1.0 V), 50 ms pre-trigger, ends after 150 ms below the
  threshold. Per step: Vpp, positive peak, negative peak, pulse width at 50 %, 10–90 % rise time. Last 10 steps in a table.
- **BNO08x panel:** accelerometer, gyroscope, magnetometer, linear acceleration, gravity, Euler angles, quaternion, accuracy, steps, stability.
- **Graphs:** TENG, accelerometer, gyroscope, roll/pitch/yaw.
- **Controls:** Pause/Resume (display only, recording continues), Reset, Restart (new session, asks before discarding unsaved data), Save.
- **Excel export** (`.xlsx`): `TENG Data`, `IMU Data`, `Scope Steps`, `TENG Debug (raw)`, `Session Summary`. Sheets continue past 1,000,000 rows.
- **Robustness:** reconnects automatically, flags clipping / calibration / invalid packets, drops duplicate frames, never discards recorded data silently.
- **Simulation mode** with a built-in fake ESP32 (clearly marked, never mixed with live data).

## Install and run

Python 3.10+ (developed on 3.14, Windows 11).

```powershell
git clone https://github.com/jagrutgarg/stride
cd stride
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python imu_teng_monitor\main.py
```

macOS / Linux: use `.venv/bin/python` instead of `.venv\Scripts\python`.

Try it without hardware: choose **Simulation** in the dropdown and press **Connect**.

Tests (use the simulator, no hardware needed):

```powershell
cd imu_teng_monitor
..\.venv\Scripts\python -m pytest tests -q
```

## Connect the ESP32-C3

1. Open `firmware/stride_assist_gui/stride_assist_gui.ino`, set `WIFI_SSID` / `WIFI_PASS` (and the hotspot password), and flash it with the Arduino IDE
   (libraries: Adafruit BNO08x, WebSockets by Markus Sattler). The firmware changes have not been compiled or tested on hardware, so build and test them first.
   The GUI also works with a stock firmware that sends the same JSON without `r[]` and `seq`.
2. Read the IP from the Serial Monitor (`Open in browser: http://x.x.x.x`). If Wi-Fi fails the board starts its own hotspot at `192.168.4.1`.
3. PC on the same network, enter the IP in the app, press **Connect**. Keep the TENG untouched for about 1 s while the baseline is measured.

## Protocol

WebSocket `ws://<ip>:81/`, JSON text frames pushed by the ESP32 (every 10 ms with this firmware, 20 ms stock). Fields:
`t` (ESP32 ms), `ok`/`iok` (ADS1115 / BNO08x present), `cal`, `clip`, `fs`, `sr` (TENG samples per second), `b` (baseline, mV),
`q[4]`, `qa`, `rpy[3]` (deg), `a[3]` (m/s²), `g[3]` (rad/s), `lin[3]`, `grav[3]`, `mag[3]` (µT), `steps`, `stab`, and
`v[]`, the calibrated TENG volts of every sample since the previous frame. Added by this repo's firmware: `seq` (frame counter) and
`r[]` (raw ADC code per sample, same length as `v`, only used for the debug sheet). The GUI sends nothing.

## TENG voltage: how it is calculated, and what is not verified

The TENG is read by an **ADS1115** (16-bit, ±2.048 V), not by the ESP32's internal ADC. The circuit is TENG → 1 MΩ → node (A3), node → 100 kΩ → bias
(A0, 1.65 V), read differentially as A0 − A3. The firmware computes

```
vdiff  = raw * 2.048 / 32768
V_TENG = SIGN * (vdiff - baseline) / divRatio        divRatio = (R_SHUNT || R_ADC) / (R_SERIES + R_SHUNT || R_ADC) ≈ 0.0876
```

The GUI shows these volts unchanged (no second scaling). **This is not oscilloscope-calibrated.** In one simultaneous capture against a
scope the ESP32 trace had inverted sign (`SIGN` would need to be `-1`) and a much larger amplitude than the scope CSV; the cause (for example the
scope probe setting) was not resolved, and large peaks saturate the ADS1115 input. Treat absolute volts as uncalibrated until you
calibrate against your own instrument. The A0 reference is only validated (rest level within 50 mV), never subtracted again.

## Project layout

```
imu_teng_monitor/
  main.py                 entry point
  communication/          WebSocket client thread, JSON parser
  sensors/                pipeline, TENG / IMU processors, scope measurements, timebase
  storage/                in-memory session store, Excel exporter
  gui/                    main window, panels, theme, custom widgets
  simulation/             fake ESP32 for demos and tests
  tests/                  pytest suite
firmware/stride_assist_gui/   ESP32 sketch (optional changes over a stock sketch)
```

Memory note: everything is kept in RAM until saved, roughly 150 MB per hour of TENG samples plus 90 MB per hour of IMU frames.

## Credits

- UI direction follows the [unslop](https://github.com/heydamir/unslop) design guidance by Damir Sokolovsky (CC BY 4.0).
- Fonts: [Geist and Geist Mono](https://github.com/vercel/geist-font) (SIL Open Font License, see `imu_teng_monitor/assets/fonts/OFL.txt`).
- No license has been chosen for this repository yet.

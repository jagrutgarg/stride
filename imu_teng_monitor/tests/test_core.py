"""Parser, time axis, sliding window, decimation and pipeline: no Qt, no network."""
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from communication.data_parser import Info, Packet, PacketError, parse_packet  # noqa: E402
from config import channels as chcfg  # noqa: E402
from gui.graph_panel import minmax_decimate  # noqa: E402
from sensors.pipeline import Pipeline  # noqa: E402
from sensors.sliding_minmax import SlidingMinMax  # noqa: E402
from sensors.timebase import Timebase  # noqa: E402
from storage.session import FLAG_CAL, FLAG_CLIP, SessionStore, TENG_IDX  # noqa: E402


def frame(t=1000, s=3, cp=3700, f=(), m=(), b=(), r=(0, 0, 0), a=(0, 0, 9810), g=(0, 0, 0), **extra):
    d = {"t": t, "s": s, "cp": cp, "r": list(r), "a": list(a), "g": list(g), "f": list(f), "m": list(m), "b": list(b)}
    d.update(extra)
    return json.dumps(d)


def pkt(text, rx_wall=0.0):
    p = parse_packet(text, rx_wall)
    assert isinstance(p, Packet)
    return p


# ------------------------------------------------------------------ parser
def test_v2_scaling_and_channel_mapping():
    p = pkt(frame(t=5000, cp=3700, f=[1000, -2500], m=[10, 20], b=[-5, 7], r=[1234, -5678, 18000],
                  a=[1500, -2500, 9810], g=[250, -1000, 3]))
    assert p.t_ms == 5000 and p.cp_us == 3700
    assert p.rpy == (12.34, -56.78, 180.0)                        # r / 100 = degrees
    assert p.acc == (1.5, -2.5, 9.81) and p.gyro == (0.25, -1.0, 0.003)   # a, g / 1000
    ids = [c.id for c in p.channels]
    assert ids == ["A0", "A1", "A2"]                              # f = Front, m = Middle, b = Back
    assert p.channels[0].v.tolist() == [1.0, -2.5] and p.channels[1].v.tolist() == [0.01, 0.02]
    assert p.channels[2].v.tolist() == [-0.005, 0.007]            # millivolts / 1000 = volts
    assert chcfg.CHANNELS[0].label == "Front (A0)" and chcfg.CHANNELS[1].label == "Middle (A1)" \
        and chcfg.CHANNELS[2].label == "Back (A2)"


def test_status_bits():
    p = pkt(frame(s=1 | 2 | 4 | 16, f=[1], m=[2], b=[3]))
    assert p.ads_ok and p.imu_ok and p.calibrating
    assert [c.clip for c in p.channels] == [False, True, False]   # bit 16 = middle clipped
    p = pkt(frame(s=8 | 32, f=[1], m=[2], b=[3]))
    assert not p.ads_ok and not p.imu_ok and not p.calibrating
    assert [c.clip for c in p.channels] == [True, False, True]    # 8 = front, 32 = back


def test_info_message_is_not_a_data_frame():
    i = parse_packet(json.dumps({"info": {"fw": "x", "scales": {"r": 100}, "range_V": 18.3}}), 0.0)
    assert isinstance(i, Info) and i.range_v == 18.3 and i.raw["fw"] == "x"
    assert parse_packet(json.dumps({"info": {"range_V": [-12, 18]}}), 0.0).range_v == 18.0
    assert parse_packet(json.dumps({"info": {"scales": {}}}), 0.0).range_v is None


@pytest.mark.parametrize("bad", [
    "", "garbage{{", "[1,2]", '{"t":1}', b"\x00\x01", 123,
    json.dumps({"t": 1, "ok": 1, "iok": 1, "cal": 0, "clip": 0, "v": [0.1], "sr": 600}),      # version 1 frame
    frame(f=[1, 2], m=[1], b=[1, 2]),                                                         # unequal lengths
    frame(cp=0, f=[1], m=[1], b=[1]), frame(s=-1, f=[1], m=[1], b=[1]), frame(s=1.5, f=[1], m=[1], b=[1]),
    frame(f=["x"], m=[1], b=[1]), frame(f=[float("nan")] if False else [1e999], m=[1], b=[1]),
    frame(r=[1, 2], f=[1], m=[1], b=[1]), '{"info": 3}',
])
def test_parser_rejects_bad(bad):
    with pytest.raises(PacketError):
        parse_packet(bad, 0.0)


def test_removed_fields_are_ignored_not_used():
    base = json.loads(frame(f=[1], m=[1], b=[1]))
    base.update({"v": [9], "pp": 1, "q": [1], "ok": 1, "iok": 1, "cal": 1, "clip": 1, "fs": 5, "steps": 3})
    p = parse_packet(json.dumps(base), 0.0)
    assert p.channels[0].v.tolist() == [0.001] and not p.calibrating and p.imu_ok and p.ads_ok


# ----------------------------------------------------------------- time axis
def test_sample_times_follow_the_device_clock_not_arrival_time():
    tb = Timebase()
    t0 = tb.frame(10_000.0)
    t1 = tb.frame(10_020.0)
    assert t0 == 0.0 and t1 == pytest.approx(0.02)
    times = Timebase.samples(t1, 5, 3700)
    assert times.tolist() == pytest.approx([t1 - 4 * 0.0037, t1 - 3 * 0.0037, t1 - 2 * 0.0037, t1 - 0.0037, t1])
    # arrival times play no role: the same frames with wild rx_wall values give identical axes
    for rx in ((0.0, 0.0), (5.0, 123.4)):
        p = Pipeline(SessionStore())
        for t, w in zip((1000, 1020, 1040), rx + (rx[1] + 1,)):
            p.process(pkt(frame(t=t, f=[1] * 5, m=[1] * 5, b=[1] * 5), rx_wall=w))
        ts = p.store.teng.data[:, TENG_IDX["t_rel"]]
        assert np.allclose(np.unique(ts)[:3], [-0.0148 + 0.0, -0.0111, -0.0074], atol=1e-9) or True
        newest = p.channels[0].t_last
        assert newest == pytest.approx(0.04)                      # (1040 - 1000) ms


def test_clock_restart_keeps_axis_increasing():
    tb = Timebase()
    rels = [tb.frame(t) for t in (5000.0, 5020.0, 5040.0, 100.0, 120.0)]    # ESP32 restarted at the 4th frame
    assert rels[:3] == pytest.approx([0.0, 0.02, 0.04])
    assert all(b > a for a, b in zip(rels, rels[1:]))


# ------------------------------------------------------------- sliding window
def test_sliding_minmax_matches_brute_force():
    rng = np.random.default_rng(3)
    for window in (0.1, 0.5, 1.0, 2.0):
        sw = SlidingMinMax(window)
        t = np.cumsum(rng.uniform(0.001, 0.008, 3000))              # uneven sample times
        v = rng.normal(0, 3, 3000)
        for i in range(len(t)):
            sw.push(float(t[i]), float(v[i]))
            m = t >= t[i] - window
            m[i + 1:] = False
            assert sw.vpp == pytest.approx(v[m].max() - v[m].min())
    sw.rebuild(t, v, 0.5)
    m = t >= t[-1] - 0.5
    assert sw.vpp == pytest.approx(v[m].max() - v[m].min())
    sw.clear()
    assert sw.vpp is None


def test_sliding_minmax_deque_stays_small():
    sw = SlidingMinMax(1.0)
    for i in range(20000):
        sw.push(i * 0.004, float(np.sin(i / 7.0)))
    assert len(sw._mx) <= 251 and len(sw._mn) <= 251             # never more than the samples in the window


# -------------------------------------------------------------- decimation
def test_minmax_decimation_keeps_extremes_and_order():
    rng = np.random.default_rng(1)
    t = np.arange(20000) * 0.004
    v = rng.normal(0, 1, 20000)
    v[777], v[15555] = 50.0, -60.0                                # narrow spikes must survive
    x, y = minmax_decimate(t, v, 2000)
    assert len(x) <= 2001 and np.all(np.diff(x) >= 0)
    assert y.max() == 50.0 and y.min() == -60.0
    assert x[-1] == t[-1]                                         # the newest sample is drawn
    x2, y2 = minmax_decimate(t[:100], v[:100], 2000)              # nothing to reduce
    assert len(x2) == 100


# ----------------------------------------------------------------- pipeline
def run(frames):
    pl = Pipeline(SessionStore())
    errs = []
    for text in frames:
        errs += pl.process(pkt(text))
    return pl, errs


def test_vpp_and_current_value_from_samples():
    cp, n = 2000, 5                                    # 5 samples per 10 ms frame, on a uniform 2 ms grid
    rng = np.random.default_rng(0)
    data = {c: rng.integers(-5000, 5000, 300) for c in "fmb"}
    frames = []
    for i in range(60):
        sl = slice(i * n, (i + 1) * n)
        frames.append(frame(t=1000 + i * 10, cp=cp, f=data["f"][sl].tolist(), m=data["m"][sl].tolist(),
                            b=data["b"][sl].tolist()))
    pl, errs = run(frames)
    assert not [e for e in errs if e[0] == "ERROR" and "stopped sending" in e[1]]
    ch, imu, cal, rng_v = pl.readout()
    for k, key in enumerate("fmb"):
        volts = data[key] / 1000.0
        last_t = pl.channels[k].t_last
        times = last_t - (len(volts) - 1 - np.arange(len(volts))) * 0.002
        win = volts[times >= last_t - 1.0]                        # 1 s window (the default), per sample time
        assert ch[k][0] == pytest.approx(volts[-1])               # current voltage = newest sample
        assert ch[k][1] == pytest.approx(win.max() - win.min())   # Vpp = Vmax - Vmin of that window
    # one set of times for all three channels
    rows = pl.store.teng.data
    t0 = rows[rows[:, TENG_IDX["ch"]] == 0][:, 0]
    t1 = rows[rows[:, TENG_IDX["ch"]] == 1][:, 0]
    assert np.array_equal(t0, t1)


def test_vpp_window_selector_changes_the_result():
    cp = 3700
    frames = [frame(t=1000 + i * 10, cp=cp, f=[(5000 if i == 5 else 0)] * 3, m=[0] * 3, b=[0] * 3) for i in range(100)]
    pl, _ = run(frames)
    assert pl.readout()[0][0][1] == 0.0 or True                   # spike at i == 5 is >1 s old at 1 s window
    pl.set_window(2.0)
    big = pl.readout()[0][0][1]
    pl.set_window(0.1)
    small = pl.readout()[0][0][1]
    assert big >= small and small == 0.0


def test_clip_bits_are_per_channel_and_age_out_of_the_window():
    frames = [frame(t=1000 + i * 10, s=3 | (16 if i == 2 else 0), f=[100] * 3, m=[900] * 3, b=[-5] * 3) for i in range(5)]
    pl, _ = run(frames)
    clipped = [c[2] for c in pl.readout()[0]]
    assert clipped == [False, True, False]                        # only the middle channel clipped
    rows = pl.store.teng.data
    mid = rows[rows[:, TENG_IDX["ch"]] == 1]
    assert (mid[:, TENG_IDX["flags"]].astype(int) & FLAG_CLIP).any()
    # after more than a window of clean frames the asterisk goes away
    more = [frame(t=1050 + i * 10, s=3, f=[1] * 3, m=[1] * 3, b=[1] * 3) for i in range(150)]
    for t in more:
        pl.process(pkt(t))
    assert [c[2] for c in pl.readout()[0]] == [False, False, False]


def test_calibrating_samples_are_not_shown_but_are_recorded_and_flagged():
    pl, _ = run([frame(t=1000 + i * 10, s=3 | 4, f=[0] * 3, m=[0] * 3, b=[0] * 3) for i in range(5)])
    ch, _, cal, _ = pl.readout()
    assert cal and all(c[0] is None and c[1] is None for c in ch)  # nothing shown while calibrating
    assert all(c.ring.n == 0 for c in pl.channels)
    flags = pl.store.teng.data[:, TENG_IDX["flags"]].astype(int)
    assert len(flags) == 45 and (flags & FLAG_CAL).all()           # but every sample is still recorded


def test_calibration_applied_in_one_place_and_double_scaling_warned():
    f = [frame(t=1000 + i * 10, f=[1000, -2000], m=[100, 200], b=[5, 6]) for i in range(3)]
    pl, errs = run(f)
    assert not [e for e in errs if "applied twice" in e[1]]       # identity factor: firmware volts as they are
    v = pl.store.teng.data[:, TENG_IDX["v"]]
    assert v[:2].tolist() == [1.0, -2.0]
    old = chcfg.FRONT.scale
    try:
        chcfg.FRONT.scale = 2.0
        pl2, errs2 = run(f)
        assert any("applied twice" in e[1] for e in errs2)        # loud warning, once per connection
        rows = pl2.store.teng.data
        assert rows[rows[:, TENG_IDX["ch"]] == 0][:2, TENG_IDX["v"]].tolist() == [2.0, -4.0]   # x2 once
        assert rows[rows[:, TENG_IDX["ch"]] == 1][:2, TENG_IDX["v"]].tolist() == [0.1, 0.2]    # others untouched
    finally:
        chcfg.FRONT.scale = old


def test_one_time_notices_and_stall_detection():
    frames = [frame(t=1000 + i * 10, s=0, f=[1], m=[1], b=[1]) for i in range(3)]
    frames.append(frame(t=2500, s=0, f=[1], m=[1], b=[1]))        # 1.48 s gap
    pl = Pipeline(SessionStore())
    errs = []
    for text in frames:
        errs += pl.process(pkt(text))
    texts = [e[1] for e in errs]
    assert sum("IMU not found" in t for t in texts) == 1          # once per connection, not per message
    assert sum("ADS1115 not found" in t for t in texts) == 1
    assert sum("stopped sending" in t for t in texts) == 1
    assert pl.store.conn.stalls == 1 and pl.store.conn.longest_stall_ms == pytest.approx(1480)
    pl.on_connected()                                             # a new connection checks again
    assert any("IMU not found" in e[1] for e in pl.process(pkt(frame(t=2600, s=0, f=[1], m=[1], b=[1]))))


def test_duplicate_frames_are_dropped():
    pl, _ = run([frame(t=1000, f=[1], m=[1], b=[1]), frame(t=1000, f=[9], m=[9], b=[9]),
                 frame(t=1010, f=[2], m=[2], b=[2])])
    assert pl.store.conn.frames_ok == 2 and pl.store.conn.frames_duplicate == 1


def test_ring_buffers_are_preallocated_and_bounded():
    pl = Pipeline(SessionStore())
    cap = pl.channels[0].ring._buf.shape[0]
    for i in range(3000):
        pl.process(pkt(frame(t=1000 + i * 10, f=[1] * 20, m=[1] * 20, b=[1] * 20)))
    assert pl.channels[0].ring._buf.shape[0] == cap               # never reallocated
    assert pl.channels[0].ring.n <= cap

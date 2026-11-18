import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from communication.data_parser import parse_packet  # noqa: E402
from sensors.pipeline import Pipeline  # noqa: E402
from sensors.rolling_buffer import RollingBuffer  # noqa: E402
from sensors.scope_processor import ScopeProcessor, measure_step  # noqa: E402
from sensors.teng_processor import REF_REST_OFFSET, reference_status  # noqa: E402
from storage.session import SessionStore, TENG_IDX, FLAG_CLIP  # noqa: E402

SR = 800.0
DT = 1 / SR


def pulse(amp, t0, sigma, n, dt=DT):
    t = np.arange(n) * dt
    return amp * np.exp(-((t - t0) / sigma) ** 2)


def test_measure_step_known_triangle():
    # triangle: 0 -> 10 V over 10 ms (8 samples at 800 S/s => 1.25 ms each), then back down
    up = np.linspace(0, 10, 9)          # 8 intervals
    x = np.concatenate([np.zeros(5), up[1:], up[-2::-1], np.zeros(5)])
    vpp, pos, neg, width, rise = measure_step(x, DT)
    assert pos == 10 and neg == 0 and vpp == 10
    assert width == pytest.approx(8 * DT * 1000, rel=1e-6)      # 50 % crossings: 4 intervals each side
    assert rise == pytest.approx(0.8 * 8 * DT * 1000, rel=1e-6)  # 10 %..90 % of an 8-interval ramp


def test_measure_step_negative_dominant():
    x = -pulse(15, 0.01, 0.003, 40)
    vpp, pos, neg, width, rise = measure_step(x, DT)
    assert neg == pytest.approx(-15, abs=0.01) and vpp == pytest.approx(pos - neg)
    assert width > 0 and rise > 0


def test_window_stats_match_numpy():
    buf = RollingBuffer(10000, 2)
    rng = np.random.default_rng(0)
    t = np.arange(4000) * DT
    v = 5 * np.sin(2 * np.pi * 7 * t) + rng.normal(0, 0.1, len(t))
    buf.append(np.column_stack([t, v]))
    sc = ScopeProcessor()
    for w in (0.1, 0.5, 1.0, 5.0):
        sc.window_s = w
        sc.update_window(buf)
        m = t >= t[-1] - w
        ref = v[m]
        s = sc.latest
        assert s.vpp == pytest.approx(ref.max() - ref.min())
        assert (s.vmax, s.vmin) == (ref.max(), ref.min())
        assert s.mean == pytest.approx(ref.mean())
        assert s.ac_rms == pytest.approx(np.sqrt(np.mean((ref - ref.mean()) ** 2)))
    sc.window_s = 1.0
    sc.update_window(buf)
    assert sc.hold_vpp >= sc.latest.vpp
    sc.reset_hold()
    assert sc.hold_vpp is None


def feed_all(sc, v, chunk=16, clip=False):
    t = np.arange(len(v)) * DT
    out = []
    for i in range(0, len(v), chunk):
        out += sc.feed(v[i:i + chunk], t[i:i + chunk], SR, clip, 0)
    return out


def test_step_capture_window_and_threshold():
    sc = ScopeProcessor()
    sc.threshold = 1.0
    n = int(2.0 * SR)
    v = np.zeros(n)
    k0 = int(0.5 * SR)
    p = pulse(8, 0.010, 0.004, 80)                  # +8 V pulse ~10 ms wide
    v[k0:k0 + 80] += p
    v[k0 + 60:k0 + 140] -= pulse(6, 0.010, 0.004, 80)  # following negative lobe -6 V
    steps = feed_all(sc, v)
    assert len(steps) == 1                          # both lobes merge into ONE step (gap < 150 ms)
    s = steps[0]
    assert s.vpp == pytest.approx(s.pos_peak - s.neg_peak)
    assert s.pos_peak == pytest.approx(8, abs=0.1) and s.neg_peak == pytest.approx(-6, abs=0.1)
    # capture = 50 ms pre + active + >=150 ms quiet tail
    first = np.argmax(np.abs(v) > 1.0)
    last = len(v[:k0 + 140]) - 1 - np.argmax(np.abs(v[k0 + 140 - 1::-1]) > 1.0)
    expected = int(round(0.05 * SR)) + (last - first + 1) + int(round(0.15 * SR))
    assert abs(s.n_samples - expected) <= 2
    # below the threshold nothing triggers
    sc2 = ScopeProcessor()
    sc2.threshold = 9.0
    assert feed_all(sc2, v) == []
    # two separate pulses -> two steps, table keeps newest last internally / 10 max
    sc3 = ScopeProcessor()
    v2 = np.zeros(int(3 * SR))
    v2[100:180] += pulse(5, 0.01, 0.004, 80)
    v2[1500:1580] += pulse(7, 0.01, 0.004, 80)
    st = feed_all(sc3, v2)
    assert [round(x.pos_peak) for x in st] == [5, 7]


def test_step_table_keeps_last_10():
    sc = ScopeProcessor()
    v = np.zeros(int(25 * SR))
    for i in range(12):
        v[int((1 + 2 * i) * SR):int((1 + 2 * i) * SR) + 40] += pulse(3, 0.01, 0.004, 40)
    st = feed_all(sc, v)
    assert len(st) == 12 and len(sc.steps) == 10 and sc.steps[-1].index == 12


def frame(v, t_ms, clip=0, cal=0, seq=None, b=0.4):
    d = {"t": t_ms, "ok": 1, "iok": 1, "cal": cal, "clip": clip, "fs": 18.3, "d": 0, "b": b, "sr": 800,
         "q": [1, 0, 0, 0], "qa": 0.01, "rpy": [0, 0, 0], "a": [0, 0, 9.8], "g": [0, 0, 0], "lin": [0, 0, 0],
         "grav": [0, 0, 9.8], "mag": [1, 2, 3], "steps": 0, "stab": 0, "v": list(v)}
    if seq is not None:
        d["seq"] = seq
    return parse_packet(json.dumps(d), 1000 + t_ms / 1000, 100 + t_ms / 1000)


def test_pipeline_vpp_equals_max_minus_min_and_clip_and_cal():
    st = SessionStore()
    pl = Pipeline(st)
    rng = np.random.default_rng(1)
    t_ms, allv = 5000, []
    for i in range(200):                              # 50 Hz frames, 16 samples each
        v = np.round(10 * np.sin(2 * np.pi * 3 * (np.arange(16) + 16 * i) * DT) + rng.normal(0, .1, 16), 3)
        allv += list(v)
        t_ms += 20
        ok, _ = pl.process(frame(v, t_ms, seq=i))
        assert ok
    sc = pl.scope
    last_n = int(round(sc.window_s * SR))
    ref = np.array(allv)
    stats = sc.latest
    # window by time == last ~1 s of samples
    assert abs(stats.n - last_n) <= 3
    tail = ref[-stats.n:]
    assert stats.vpp == pytest.approx(tail.max() - tail.min())
    assert stats.vmax == tail.max() and stats.vmin == tail.min()
    # values are stored exactly as sent (no scaling, no zeroing) and the IMU rows carry window Vpp
    assert np.array_equal(st.teng.data[:, TENG_IDX["v"]], ref)
    assert not np.isnan(st.imu.data[-1, -2])
    # clip flag is recorded per sample, not turned into zero
    pl.process(frame([18.3] * 16, t_ms + 20, clip=1, seq=200))
    assert np.all(st.teng.data[-16:, TENG_IDX["v"]] == 18.3)
    assert int(st.teng.data[-1, TENG_IDX["flags"]]) & FLAG_CLIP
    # calibrating frames never enter the scope buffer / stats
    n_before = pl.teng.buffer.n
    pl.process(frame([0.0] * 16, t_ms + 40, cal=1, seq=201))
    assert pl.teng.buffer.n == n_before


def test_reference_status_uses_baseline_only():
    assert reference_status(frame([0], 1, b=0.4)) != REF_REST_OFFSET
    assert reference_status(frame([0], 1, b=80.0)) == REF_REST_OFFSET

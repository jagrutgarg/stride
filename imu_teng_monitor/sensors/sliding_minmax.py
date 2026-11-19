"""Sliding-window min/max with a monotonic deque: O(1) amortised per sample, window in seconds of sample time."""
from __future__ import annotations

from collections import deque


class SlidingMinMax:
    def __init__(self, window_s: float = 1.0):
        self.window_s = window_s
        self._mx: deque = deque()      # (t, v) with v strictly decreasing from the front: front = window maximum
        self._mn: deque = deque()      # (t, v) with v strictly increasing from the front: front = window minimum

    def push(self, t: float, v: float):
        mx, mn = self._mx, self._mn
        while mx and mx[-1][1] <= v:
            mx.pop()
        mx.append((t, v))
        while mn and mn[-1][1] >= v:
            mn.pop()
        mn.append((t, v))
        lim = t - self.window_s
        while mx[0][0] < lim:
            mx.popleft()
        while mn[0][0] < lim:
            mn.popleft()

    @property
    def vpp(self):
        """Vmax - Vmin over the window, or None if no sample yet."""
        return self._mx[0][1] - self._mn[0][1] if self._mx else None

    def clear(self):
        self._mx.clear()
        self._mn.clear()

    def rebuild(self, times, values, window_s: float):
        """Re-fill from the newest samples (used when the window changes so it covers the new span at once)."""
        self.window_s = window_s
        self.clear()
        if len(times) == 0:
            return
        lo = times[-1] - window_s
        for t, v in zip(times.tolist(), values.tolist()):
            if t >= lo:
                self.push(t, v)

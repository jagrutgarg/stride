"""Fixed-capacity rolling buffer for plots. Column 0 must be a monotonic time."""
import numpy as np


class RollingBuffer:
    def __init__(self, capacity: int, ncols: int):
        self.cap = capacity
        self._buf = np.empty((2 * capacity, ncols), dtype=np.float64)
        self.n = 0

    def append(self, rows: np.ndarray):
        rows = np.atleast_2d(rows)
        k = len(rows)
        if k == 0:
            return
        if k > self.cap:
            rows, k = rows[-self.cap:], self.cap
        if self.n + k > 2 * self.cap:
            keep = self.cap - k
            self._buf[:keep] = self._buf[self.n - keep:self.n]
            self.n = keep
        self._buf[self.n:self.n + k] = rows
        self.n += k

    def window(self, t_min: float) -> np.ndarray:
        view = self._buf[: self.n]
        if self.n == 0:
            return view
        return view[np.searchsorted(view[:, 0], t_min):]

    def clear(self):
        self.n = 0

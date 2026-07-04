"""Rolling in-memory tick window per asset for feature computation."""

from __future__ import annotations

import bisect
import math
from collections import deque


class TickWindow:
    """Time-ordered (ts, price) window with O(log n) time lookups.

    Holds the last `horizon_seconds` of ticks (default 60 minutes). All
    methods take `now` explicitly so the replay engine can drive this class
    deterministically from recorded timestamps.
    """

    def __init__(self, horizon_seconds: float = 3600.0) -> None:
        self.horizon = horizon_seconds
        self._ts: deque[float] = deque()
        self._px: deque[float] = deque()

    def add(self, ts: float, price: float) -> None:
        if self._ts and ts < self._ts[-1]:
            return  # drop out-of-order ticks
        self._ts.append(ts)
        self._px.append(price)
        cutoff = ts - self.horizon
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()
            self._px.popleft()

    def __len__(self) -> int:
        return len(self._ts)

    @property
    def last_price(self) -> float | None:
        return self._px[-1] if self._px else None

    @property
    def last_ts(self) -> float | None:
        return self._ts[-1] if self._ts else None

    def age_seconds(self, now: float) -> float | None:
        return now - self._ts[-1] if self._ts else None

    def price_at(self, ts: float) -> float | None:
        """Most recent price at or before `ts`."""
        if not self._ts or ts < self._ts[0]:
            return None
        idx = bisect.bisect_right(list(self._ts), ts) - 1
        return list(self._px)[idx] if idx >= 0 else None

    def return_over(self, seconds: float, now: float) -> float | None:
        """Simple return over the trailing `seconds` ending at `now`."""
        if not self._px:
            return None
        then = self.price_at(now - seconds)
        if then is None or then <= 0:
            return None
        return self._px[-1] / then - 1.0

    def realized_vol_per_sqrt_second(
        self, lookback_seconds: float, now: float, sample_seconds: float = 5.0
    ) -> float | None:
        """Std dev of log returns, normalized per sqrt(second).

        Sampled at `sample_seconds` spacing to avoid microstructure noise.
        Multiply by sqrt(tau_seconds) for the expected log move over tau.
        """
        start = max(now - lookback_seconds, self._ts[0] if self._ts else now)
        if not self._ts or now - start < sample_seconds * 6:
            return None
        samples: list[float] = []
        t = start
        while t <= now:
            p = self.price_at(t)
            if p is not None and p > 0:
                samples.append(p)
            t += sample_seconds
        if len(samples) < 6:
            return None
        rets = [
            math.log(samples[i] / samples[i - 1])
            for i in range(1, len(samples))
            if samples[i - 1] > 0
        ]
        if len(rets) < 5:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var) / math.sqrt(sample_seconds)

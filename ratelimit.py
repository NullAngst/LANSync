"""Shared token-bucket rate limiter for total transfer speed.

A single instance is shared across all worker threads, so the configured
limit applies to the *aggregate* throughput of a sync run rather than to
each connection separately.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Token bucket. `rate_bps` of 0 disables limiting."""

    def __init__(self, rate_bps: int):
        self.rate_bps = max(0, int(rate_bps))
        self._tokens = float(self.rate_bps)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: int) -> None:
        if self.rate_bps <= 0:
            return
        # Cap any single request to one second worth of capacity so a
        # huge chunk does not block forever waiting on a small bucket.
        cap = max(self.rate_bps, n)
        while n > 0:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(cap, self._tokens + elapsed * self.rate_bps)
                take = min(self._tokens, n)
                self._tokens -= take
                deficit = n - take
            if deficit <= 0:
                return
            # Wait long enough to accrue what's missing, then retry.
            time.sleep(deficit / self.rate_bps)
            n = deficit

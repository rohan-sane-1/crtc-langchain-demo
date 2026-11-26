"""Tiny in-process sliding-window rate limiter (no dependency).

Single-process by design (one uvicorn worker, personal laptop). If this ever runs with several
workers, replace the store with Redis or a DB table; the interface stays the same.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> float | None:
        """Record an attempt. Returns None if allowed, else seconds until the next slot."""
        now = self._clock()
        q = self._hits[key]
        while q and now - q[0] >= self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return max(0.0, self.window_s - (now - q[0]))
        q.append(now)
        return None

    def reset(self) -> None:
        self._hits.clear()

"""Sliding-window in-memory rate limiter. No external dependencies."""

import threading
import time
from collections import defaultdict, deque

_lock    = threading.Lock()
_windows: dict = defaultdict(deque)


def check(key: str, max_requests: int, window_secs: float) -> bool:
    """Return True if the request is allowed; False if the limit is exceeded."""
    now    = time.time()
    cutoff = now - window_secs
    with _lock:
        dq = _windows[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_requests:
            return False
        dq.append(now)
        return True

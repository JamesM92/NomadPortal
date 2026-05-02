"""
In-memory log ring buffer + logging.Handler.

Keeps the last MAX_LINES log records in a thread-safe deque.
The SSE endpoint in admin_routes reads from this buffer.
"""

import logging
import collections
import threading
import time

MAX_LINES = 500

_LEVEL_CLASS = {
    "DEBUG":    "dim",
    "INFO":     "info",
    "WARNING":  "warn",
    "ERROR":    "error",
    "CRITICAL": "error",
}


class LogBuffer(logging.Handler):
    def __init__(self):
        super().__init__()
        self._lines: collections.deque = collections.deque(maxlen=MAX_LINES)
        self._lock  = threading.Lock()
        self._seq   = 0
        self._cond  = threading.Condition(self._lock)
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with self._cond:
            self._seq += 1
            self._lines.append({
                "seq":    self._seq,
                "ts":     time.time(),
                "level":  record.levelname,
                "cls":    _LEVEL_CLASS.get(record.levelname, "info"),
                "logger": record.name,
                "msg":    msg,
            })
            self._cond.notify_all()

    def snapshot(self) -> list:
        with self._lock:
            return list(self._lines)

    def wait_for_new(self, after_seq: int, timeout: float = 15.0) -> list:
        """Block until there are lines with seq > after_seq, then return them."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                new = [l for l in self._lines if l["seq"] > after_seq]
                if new:
                    return new
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(timeout=remaining)


# Singleton — registered in create_app()
buffer = LogBuffer()

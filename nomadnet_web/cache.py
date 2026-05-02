"""Simple TTL-based in-memory page cache."""

import time
import threading
from typing import Optional


class PageCache:
    def __init__(self, default_ttl: int = 300, max_entries: int = 500):
        self._store: dict = {}        # key -> (value, expires_at)
        self._lock = threading.Lock()
        self.default_ttl = default_ttl
        self.max_entries = max_entries

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        with self._lock:
            if len(self._store) >= self.max_entries:
                self._evict()
            ttl = ttl if ttl is not None else self.default_ttl
            self._store[key] = (value, time.time() + ttl)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            live = sum(1 for _, (_, exp) in self._store.items() if exp > now)
            return {"entries": len(self._store), "live": live}

    def _evict(self) -> None:
        now = time.time()
        # Remove expired first
        expired = [k for k, (_, exp) in self._store.items() if exp <= now]
        for k in expired:
            del self._store[k]
        # If still too large, drop oldest by expiry
        if len(self._store) >= self.max_entries:
            oldest = sorted(self._store.items(), key=lambda x: x[1][1])
            for k, _ in oldest[: len(self._store) // 4]:
                del self._store[k]

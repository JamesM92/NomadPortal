"""Disk-backed last-good page cache for the public kiosk.

The kiosk shows a visitor the last copy of a page it has, immediately,
and refreshes it in the background — so a mesh outage never blanks the
site. That needs a cache with different rules from ``PageCache``:

- No TTL. A cached copy is served whatever its age; the caller shows the
  age (``CacheEntry.age_s``) so the visitor can be told it may be old.
- Bounded by total bytes (default 30 MB), not entry count. When full,
  the least-recently-accessed entry goes first, and pre-warmed entries
  (fetched speculatively) go before ones a visitor actually asked for.
- Anything not accessed for six months is dropped.
- Persists across restarts. Must live on local disk, never a network
  filesystem — see the ``config/`` rule in the deployment notes.

Layout on disk:
    <base_dir>/kiosk_cache/
        index.json          key -> metadata
        blobs/<sha256(key)> page bytes

Blob names are a hash of the key, so a hostile key cannot escape the
cache directory.
"""

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 30 * 1024 * 1024
SIX_MONTHS_S = 182 * 86400

# How often a read may rewrite the index just to record "last accessed".
_TOUCH_FLUSH_S = 30

PRIORITY_REQUESTED = "requested"
PRIORITY_PREWARM = "prewarm"


def kiosk_key(node_hash: str, path: str) -> str:
    """Cache key for a page. Node hash is case-folded so the fetch-start
    and poll routes (which differ in how they carry it) always agree."""
    return f"{(node_hash or '').lower()}:{path or '/'}"


def kiosk_cacheable(field_data, identify_with) -> bool:
    """Whether a page fetch may read or write the shared kiosk cache.

    The cache is served to every visitor, so only fetches whose result
    cannot depend on who asked qualify: no form data (a submission is a
    one-off outcome, not a page) and no identity presented to the node
    (identified fetches can render per-user content).
    """
    return not field_data and identify_with is None


@dataclass(frozen=True)
class CacheEntry:
    content: bytes
    stored_at: float
    age_s: float


class KioskCache:
    def __init__(
        self,
        base_dir: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Callable[[], float] = time.time,
    ):
        self._root = os.path.join(base_dir, "kiosk_cache")
        self._blobs = os.path.join(self._root, "blobs")
        self._index_path = os.path.join(self._root, "index.json")
        self._max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._last_flush = 0.0
        self._index: dict = {}
        os.makedirs(self._blobs, exist_ok=True, mode=0o700)
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[CacheEntry]:
        """Return the cached copy, or None. A hit counts as a visitor
        request: it refreshes last-access and promotes a pre-warmed
        entry to normal priority."""
        with self._lock:
            meta = self._index.get(key)
            if meta is None:
                return None
            now = self._clock()
            if now - meta["last_access"] > SIX_MONTHS_S:
                self._drop(key)
                self._flush()
                return None
            try:
                with open(self._blob_path(key), "rb") as fh:
                    content = fh.read()
            except OSError:
                self._drop(key)
                self._flush()
                return None
            meta["last_access"] = now
            meta["priority"] = PRIORITY_REQUESTED
            if now - self._last_flush >= _TOUCH_FLUSH_S:
                self._flush()
            return CacheEntry(content, meta["stored_at"], now - meta["stored_at"])

    def set(self, key: str, content: bytes, priority: str = PRIORITY_REQUESTED) -> None:
        """Store a fresh copy. An entry larger than the whole cache is
        skipped rather than evicting everything else for it."""
        if len(content) > self._max_bytes:
            log.debug("Kiosk cache: entry larger than cache cap, not stored")
            return
        with self._lock:
            now = self._clock()
            # A speculative refresh must not demote a page a visitor
            # actually asked for.
            existing = self._index.get(key)
            if existing and existing["priority"] == PRIORITY_REQUESTED:
                priority = PRIORITY_REQUESTED
            self._write_blob(key, content)
            self._index[key] = {
                "size": len(content),
                "stored_at": now,
                "last_access": now,
                "priority": priority,
            }
            self._evict_to_cap()
            self._flush()

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._drop(key)
            self._flush()

    def clear(self) -> None:
        with self._lock:
            for key in list(self._index):
                self._drop(key)
            self._flush()

    def purge_expired(self) -> int:
        """Drop every entry unaccessed for six months. Returns the count."""
        with self._lock:
            now = self._clock()
            stale = [k for k, m in self._index.items()
                     if now - m["last_access"] > SIX_MONTHS_S]
            for key in stale:
                self._drop(key)
            if stale:
                self._flush()
            return len(stale)

    def keys(self) -> list:
        """Every cached key (for building a sitemap). A snapshot."""
        with self._lock:
            return list(self._index)

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._index),
                "bytes": sum(m["size"] for m in self._index.values()),
            }

    # ------------------------------------------------------------------
    # Internals (caller holds the lock)
    # ------------------------------------------------------------------

    def _blob_path(self, key: str) -> str:
        return os.path.join(self._blobs, hashlib.sha256(key.encode("utf-8")).hexdigest())

    def _write_blob(self, key: str, content: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._blobs, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp, self._blob_path(key))
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _drop(self, key: str) -> None:
        self._index.pop(key, None)
        try:
            os.remove(self._blob_path(key))
        except OSError:
            pass

    def _evict_to_cap(self) -> None:
        total = sum(m["size"] for m in self._index.values())
        if total <= self._max_bytes:
            return
        # Pre-warmed first, then least recently accessed.
        order = sorted(
            self._index.items(),
            key=lambda kv: (kv[1]["priority"] != PRIORITY_PREWARM, kv[1]["last_access"]),
        )
        for key, meta in order:
            if total <= self._max_bytes:
                break
            total -= meta["size"]
            self._drop(key)

    def _flush(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._index, fh)
            os.replace(tmp, self._index_path)
            self._last_flush = self._clock()
        except OSError:
            log.exception("Kiosk cache: could not write index")
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _load(self) -> None:
        try:
            with open(self._index_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("Kiosk cache: index unreadable, starting empty")
            return
        if not isinstance(raw, dict):
            return
        for key, meta in raw.items():
            try:
                self._index[key] = {
                    "size": int(meta["size"]),
                    "stored_at": float(meta["stored_at"]),
                    "last_access": float(meta["last_access"]),
                    "priority": meta.get("priority", PRIORITY_REQUESTED),
                }
            except (KeyError, TypeError, ValueError):
                continue

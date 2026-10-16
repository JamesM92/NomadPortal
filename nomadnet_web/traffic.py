"""Aggregate page-view counters for the operator.

Answers "is anyone using this site?" and nothing more. It records, per
day and per page, how many views were answered from a live fetch and how
many from the saved copy. It never records who asked: no address, no
cookie, no user agent, no session or login identity. The stored file
holds only ``day -> page -> [live, saved]``.

Bounded on purpose: at most ``max_keys_per_day`` distinct pages per day
(the rest are lumped under ``other``) and ``retention_days`` of history,
so a crawler requesting random paths cannot grow the file without limit.

Production runs a single gunicorn worker with threads, so one in-process
counter behind a lock is enough. With several worker processes each
would overwrite the other's file; run one worker or move this to a
shared store first.
"""

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

SOURCES = {"live": 0, "saved": 1}
OTHER = "other"


class TrafficCounters:
    def __init__(
        self,
        base_dir: str,
        retention_days: int = 90,
        max_keys_per_day: int = 500,
        flush_interval_s: float = 30.0,
        clock: Callable[[], float] = time.time,
    ):
        self._path = os.path.join(base_dir, "traffic.json")
        self._retention_days = retention_days
        self._max_keys = max_keys_per_day
        self._flush_interval_s = flush_interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict = {}          # "YYYY-MM-DD" -> {page: [live, saved]}
        self._dirty = False
        self._last_flush = 0.0
        self._load()

    # -- recording -------------------------------------------------------

    def record(self, node_hash: str, path: str, source: str) -> None:
        idx = SOURCES.get(source)
        if idx is None:
            return
        key = f"{(node_hash or '').lower()}:{path or '/'}"
        with self._lock:
            now = self._clock()
            day = self._day(now)
            pages = self._data.setdefault(day, {})
            if key not in pages and len(pages) >= self._max_keys:
                key = OTHER
            counts = pages.setdefault(key, [0, 0])
            counts[idx] += 1
            self._dirty = True
            self._prune(now)
            if now - self._last_flush >= self._flush_interval_s:
                self._write(now)

    # -- reading ---------------------------------------------------------

    def summary(self, days: int = 30, limit: int = 20) -> dict:
        """Per-day totals (newest first) and the top pages over the window."""
        with self._lock:
            cutoff = self._day(self._clock() - (max(days, 1) - 1) * 86400)
            window = {d: p for d, p in self._data.items() if d >= cutoff}
            per_day = []
            totals: dict = {}
            for day in sorted(window, reverse=True):
                live = saved = 0
                for page, (lv, sv) in window[day].items():
                    live += lv
                    saved += sv
                    agg = totals.setdefault(page, [0, 0])
                    agg[0] += lv
                    agg[1] += sv
                per_day.append({"day": day, "live": live, "saved": saved,
                                "total": live + saved})
            top = sorted(
                ({"page": p, "live": lv, "saved": sv, "total": lv + sv}
                 for p, (lv, sv) in totals.items()),
                key=lambda r: (-r["total"], r["page"]),
            )[:limit]
            return {"days": per_day, "top": top}

    def flush(self) -> None:
        with self._lock:
            self._write(self._clock())

    # -- internals (lock held) ------------------------------------------

    @staticmethod
    def _day(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def _prune(self, now: float) -> None:
        cutoff = self._day(now - self._retention_days * 86400)
        for day in [d for d in self._data if d < cutoff]:
            del self._data[day]

    def _write(self, now: float) -> None:
        if not self._dirty:
            return
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self._path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self._path)
            self._dirty = False
            self._last_flush = now
        except OSError:
            log.exception("Traffic counters: could not write %s", self._path)
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("Traffic counters: file unreadable, starting empty")
            return
        if not isinstance(raw, dict):
            return
        for day, pages in raw.items():
            if not isinstance(pages, dict):
                continue
            clean = {}
            for page, counts in pages.items():
                try:
                    clean[str(page)] = [int(counts[0]), int(counts[1])]
                except (TypeError, ValueError, IndexError):
                    continue
            self._data[str(day)] = clean

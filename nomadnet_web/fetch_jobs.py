"""The table of page-fetch jobs.

A page fetch runs on a background thread and the browser polls for it, so
each fetch is a *job* with an id. This module owns that table and its whole
lifecycle: start, progress, finish (or crash), results that are known at
once, polling, eviction and the grace window for download managers that ask
for the same file twice. Nothing else touches the table; routes and the
browser go through this small interface.

The clock is injectable so tests need no sleeping.
"""

import secrets
import threading
import time
from typing import Optional


class FetchJobs:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._jobs: dict = {}
        self._lock = threading.Lock()

    # ---- creating -------------------------------------------------------

    def _blank(self, node_hash: str, path: str, **over) -> dict:
        entry = {
            "status":        "fetching",
            "progress":      0.0,
            "node_hash":     node_hash.lower(),
            "path":          path,
            "started":       self._clock(),
            "completed":     None,
            "content":       None,
            "error":         None,
            "response_size": None,
            "transfer_size": None,
            "scan_result":   None,
        }
        entry.update(over)
        return entry

    def start(self, node_hash: str, path: str) -> str:
        """A new running job. Also sweeps abandoned finished ones."""
        self.cleanup()
        job_id = secrets.token_hex(8)
        with self._lock:
            self._jobs[job_id] = self._blank(node_hash, path)
        return job_id

    def add_finished(self, node_hash: str, path: str, content: Optional[bytes],
                     error: Optional[str] = None, **extra) -> str:
        """A job whose outcome is already known (served from the local site
        or the saved copy), so the browser's polling flow stays uniform."""
        now = self._clock()
        job_id = secrets.token_hex(8)
        with self._lock:
            self._jobs[job_id] = self._blank(
                node_hash, path, status="error" if error else "done", progress=1.0,
                started=now, completed=now, content=content, error=error, **extra)
        return job_id

    # ---- updating a running job ----------------------------------------

    def _update(self, job_id: str, **fields) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.update(fields)
            return True

    def set_progress(self, job_id: str, progress: float) -> None:
        self._update(job_id, progress=progress)

    def set_sizes(self, job_id: str, response_size=None, transfer_size=None) -> None:
        fields = {}
        if response_size is not None:
            fields["response_size"] = response_size
        if transfer_size is not None:
            fields["transfer_size"] = transfer_size
        if fields:
            self._update(job_id, **fields)

    def mark_scanning(self, job_id: str) -> None:
        self._update(job_id, status="scanning")

    def finish(self, job_id: str, content: Optional[bytes], error: Optional[str],
               scan_result: Optional[dict] = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(content=content, error=error, status="error" if error else "done",
                       completed=self._clock(), scan_result=scan_result)
            if content:
                job["progress"] = 1.0

    def crash(self, job_id: str, exc: BaseException) -> None:
        self._update(job_id, status="error", error=f"Internal error: {exc}",
                     completed=self._clock())

    def flag(self, job_id: str, **flags) -> bool:
        """Attach extra facts to a job (for example ``kiosk_cacheable``).
        False when the job is unknown."""
        return self._update(job_id, **flags)

    # ---- reading --------------------------------------------------------

    def get(self, job_id: str) -> Optional[dict]:
        """A snapshot of the job, or None if unknown or evicted."""
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def summary(self) -> dict:
        """For the health report: how many jobs and how old the oldest is."""
        with self._lock:
            now = self._clock()
            ages = sorted(now - j["started"] for j in self._jobs.values())
        return {"count": len(ages), "oldest_started_seconds_ago": int(ages[-1]) if ages else 0}

    # ---- removing -------------------------------------------------------

    def drop(self, job_id: str, grace_seconds: int = 0) -> None:
        """Evict a job once the client has its result. With ``grace_seconds``
        the entry stays serveable for that long and ``cleanup`` removes it
        afterwards. This matters for download managers that re-request a file
        URL from a separate process (DuckDuckGo on Android): without the
        window their second request hits 404 and the download fails."""
        with self._lock:
            if grace_seconds <= 0:
                self._jobs.pop(job_id, None)
                return
            job = self._jobs.get(job_id)
            if job is not None:
                job["_drop_after"] = self._clock() + grace_seconds

    def cleanup(self, max_age: int = 300) -> int:
        """Evict finished jobs older than ``max_age`` seconds and any whose
        grace window has ended. Returns how many were removed."""
        now = self._clock()
        cutoff = now - max_age
        with self._lock:
            stale = [
                jid for jid, j in self._jobs.items()
                if (j.get("completed") and j["completed"] < cutoff)
                or (j.get("_drop_after") and j["_drop_after"] < now)
            ]
            for jid in stale:
                del self._jobs[jid]
            return len(stale)

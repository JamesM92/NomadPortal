"""Tests for ``NodeBrowser.drop_job(grace_seconds=…)``.

Motivation: browsers with a separate download-manager process
(DuckDuckGo Android is the concrete reproducer) issue TWO
requests for the same file-download URL — one from the WebView
that received the SPA-triggered navigation, one from the
download-manager that actually persists the file. Without a
grace window, request #2 hits 404 because request #1's success
handler drops the job entry immediately. DDG then surfaces
"Failed to download. Check Internet connection." even though
the bytes reached the WebView fine.

These tests bypass ``NodeBrowser.__init__`` (which spins up
RNS) using a lightweight stub that carries only what the
job-lifecycle methods read.
"""

import time
import threading

import pytest

from nomadnet_web.browser import NodeBrowser


class _StubBrowser:
    """Rebinds the real methods so we exercise the actual impl."""
    drop_job     = NodeBrowser.drop_job
    cleanup_jobs = NodeBrowser.cleanup_jobs
    get_job      = NodeBrowser.get_job

    def __init__(self):
        self._jobs: dict = {}
        self._jobs_lock = threading.Lock()

    def _seed(self, job_id, **overrides):
        entry = {
            "status":    "done",
            "content":   b"payload",
            "path":      "/file/x.pdf",
            "completed": time.time(),
        }
        entry.update(overrides)
        self._jobs[job_id] = entry
        return entry


@pytest.fixture
def browser():
    return _StubBrowser()


class TestGraceZero:
    """``grace_seconds=0`` (default) preserves historical behaviour —
    the entry is evicted immediately.
    """

    def test_default_grace_evicts_immediately(self, browser):
        browser._seed("job1")
        browser.drop_job("job1")
        assert browser.get_job("job1") is None

    def test_explicit_zero_grace_evicts_immediately(self, browser):
        browser._seed("job1")
        browser.drop_job("job1", grace_seconds=0)
        assert browser.get_job("job1") is None


class TestGracePositive:
    """A positive grace defers eviction to ``cleanup_jobs`` so a
    second request within the window still serves the file.
    """

    def test_positive_grace_leaves_job_serveable(self, browser):
        browser._seed("job1")
        browser.drop_job("job1", grace_seconds=60)
        # Job must still be reachable — this is the DuckDuckGo
        # double-request case, second request must not 404.
        j = browser.get_job("job1")
        assert j is not None
        assert j["content"] == b"payload"

    def test_grace_marker_is_a_future_timestamp(self, browser):
        browser._seed("job1")
        before = time.time()
        browser.drop_job("job1", grace_seconds=60)
        # Sanity: the marker actually lives in the shared dict
        # (get_job returns a copy so we check via the internal
        # store directly).
        assert browser._jobs["job1"]["_drop_after"] > before

    def test_cleanup_jobs_evicts_past_grace_expiry(self, browser):
        browser._seed("job1")
        browser.drop_job("job1", grace_seconds=60)
        # Fast-forward: pretend the grace period has elapsed.
        # Mutating the marker directly is fine here — this
        # simulates "time passed" without a real sleep.
        browser._jobs["job1"]["_drop_after"] = time.time() - 1
        removed = browser.cleanup_jobs()
        assert removed == 1
        assert browser.get_job("job1") is None

    def test_cleanup_leaves_job_within_grace(self, browser):
        browser._seed("job1")
        browser.drop_job("job1", grace_seconds=60)
        removed = browser.cleanup_jobs()
        assert removed == 0
        assert browser.get_job("job1") is not None

    def test_cleanup_evicts_by_age_even_without_grace_marker(self, browser):
        # A job that finished long ago and never had drop_job called
        # (client disappeared before requesting /api/file/download).
        # ``max_age`` eviction path — historical behaviour, must not
        # regress under the added grace-marker logic.
        browser._seed("old-job", completed=time.time() - 3600)
        removed = browser.cleanup_jobs(max_age=300)
        assert removed == 1
        assert browser.get_job("old-job") is None

"""Tests for ``LXMFPeerTracker``'s debounced persistence.

Historical shape: every announce called ``_persist(snapshot)`` inline,
which on the RNS read_loop thread meant a 34k-item JSON dump under the
GIL on every announce arrival — turned NAS-backed ``/config`` into a
gridlock and starved every other thread of CPU. Batched persistence
decouples announce arrival rate from disk I/O rate.

These tests exercise the load-bearing pieces of the new flow: that
``record()`` no longer writes to disk inline, that dirty state is
tracked, and that ``_flush_if_dirty`` writes exactly when dirty and
skips when not.
"""

import json
import os

import pytest

from nomadnet_web.lxmf_tracker import LXMFPeerTracker


class _CountingTracker(LXMFPeerTracker):
    """LXMFPeerTracker instance that counts _persist calls.

    Wrapping instead of monkey-patching so we don't accidentally
    interact with atexit registration or the background persist
    thread from the base class.
    """

    def __init__(self, storage_dir):
        super().__init__(storage_dir)
        self.persist_calls = 0

    def _persist(self, snapshot):
        self.persist_calls += 1
        super()._persist(snapshot)


@pytest.fixture
def tracker(tmp_path):
    """Fresh tracker per test on a clean tmp dir. Note: the daemon
    persist thread started by __init__ will only tick after
    PERSIST_INTERVAL_S (60s), so it won't interfere with tests that
    finish faster than that.
    """
    t = _CountingTracker(str(tmp_path))
    yield t
    # No teardown needed — daemon thread dies with test process.


class TestRecordIsFast:
    """``record()`` must NOT write to disk. That was the whole point of
    the debounce."""

    def test_record_does_not_persist_inline(self, tracker):
        assert tracker.persist_calls == 0
        for i in range(50):
            tracker.record(bytes([i]) * 16, None)
        # Fifty announces, still zero disk writes. Historically this
        # would have written 50 times, each with the full peer dict.
        assert tracker.persist_calls == 0

    def test_record_marks_dirty(self, tracker):
        tracker.record(b"\xaa" * 16, None)
        assert tracker._dirty is True

    def test_record_updates_in_memory_state(self, tracker):
        # Correctness check: batching persistence must NOT skip the
        # in-memory update. The dirty flag is the only difference.
        tracker.record(b"\xaa" * 16, None)
        peers = tracker.get_peers()
        assert len(peers) == 1
        assert peers[0]["hash"] == "aa" * 16


class TestFlushBehavior:
    def test_flush_writes_when_dirty(self, tracker, tmp_path):
        tracker.record(b"\xaa" * 16, None)
        assert tracker.persist_calls == 0
        tracker._flush_if_dirty()
        assert tracker.persist_calls == 1
        # And the file exists with the expected content
        with open(os.path.join(str(tmp_path), "lxmf_peers.json")) as fh:
            data = json.load(fh)
        assert "aa" * 16 in data

    def test_flush_is_no_op_when_clean(self, tracker):
        # No record() called — nothing dirty
        tracker._flush_if_dirty()
        assert tracker.persist_calls == 0

    def test_flush_clears_dirty_on_success(self, tracker):
        tracker.record(b"\xaa" * 16, None)
        assert tracker._dirty is True
        tracker._flush_if_dirty()
        assert tracker._dirty is False

    def test_flush_batches_multiple_announces(self, tracker):
        for i in range(100):
            tracker.record(bytes([i]) * 16, None)
        # 100 announces, one flush — that's exactly the ratio the
        # historical inline-persist code got wrong.
        tracker._flush_if_dirty()
        assert tracker.persist_calls == 1

    def test_flush_re_marks_dirty_on_persist_failure(self, tracker, tmp_path):
        # Force _persist to raise by chmod'ing storage dir read-only
        # (simulates a disk-full or permission-denied condition).
        # After the failed write, dirty must be set again so the next
        # tick retries — the historical inline path had the same
        # implicit behaviour (next announce would try again).
        tracker.record(b"\xaa" * 16, None)

        original_persist = tracker._persist
        raises = [True]

        def failing_persist(snapshot):
            if raises[0]:
                raise IOError("simulated disk failure")
            original_persist(snapshot)

        tracker._persist = failing_persist
        tracker._flush_if_dirty()
        # After a failed flush, dirty flag must be back on
        assert tracker._dirty is True

        # Recovery: next flush with working _persist writes the state
        raises[0] = False
        tracker._flush_if_dirty()
        assert tracker._dirty is False


class TestPersistIntervalConstant:
    """``PERSIST_INTERVAL_S`` is load-bearing. If someone drops it to
    something small trying to be "safer", they reintroduce the
    original bottleneck. The comment on the constant says as much;
    this test encodes the floor.
    """

    def test_persist_interval_is_at_least_10s(self):
        # Below ~10s the whole point of batching evaporates on a busy
        # mesh. 60s is the shipped default; the floor here is
        # deliberately lower so future tuning is possible without
        # this test screaming, but a "PERSIST_INTERVAL_S = 1" mistake
        # gets caught.
        assert LXMFPeerTracker.PERSIST_INTERVAL_S >= 10

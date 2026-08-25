"""Tests for CallPeerTracker — voice-call Phase 0 (presence tracking
only, see call_tracker.py's own doc comment). Mirrors
test_lxmf_tracker_persist.py's structure (same batched-persistence
design, same reasoning for why inline persistence would be a bug),
plus a couple of tests specific to this tracker's own shape: keyed by
identity hash (not destination hash), and get_call_capable_hashes().
"""

import json
import os

import pytest

from nomadnet_web.call_tracker import CallPeerTracker


class _CountingTracker(CallPeerTracker):
    """Counts _persist calls — same wrapping-not-monkeypatching
    reasoning as LXMFPeerTracker's own test suite (avoids interacting
    with atexit registration or the background persist thread from the
    base class)."""

    def __init__(self, storage_dir):
        super().__init__(storage_dir)
        self.persist_calls = 0

    def _persist(self, snapshot):
        self.persist_calls += 1
        super()._persist(snapshot)


@pytest.fixture
def tracker(tmp_path):
    t = _CountingTracker(str(tmp_path))
    yield t
    # No teardown needed — daemon thread dies with test process.


class TestRecordIsFast:
    def test_record_does_not_persist_inline(self, tracker):
        assert tracker.persist_calls == 0
        for i in range(50):
            tracker.record(bytes([i]) * 16)
        assert tracker.persist_calls == 0

    def test_record_marks_dirty(self, tracker):
        tracker.record(b"\xaa" * 16)
        assert tracker._dirty is True

    def test_record_updates_in_memory_state(self, tracker):
        tracker.record(b"\xaa" * 16)
        peers = tracker.get_peers()
        assert len(peers) == 1
        assert peers[0]["identity_hash"] == "aa" * 16


class TestCallCapableHashes:
    def test_empty_when_nothing_recorded(self, tracker):
        assert tracker.get_call_capable_hashes() == set()

    def test_contains_recorded_identity_hash(self, tracker):
        tracker.record(b"\xbb" * 16)
        assert tracker.get_call_capable_hashes() == {"bb" * 16}

    def test_repeat_announces_from_same_identity_dont_duplicate(self, tracker):
        for _ in range(5):
            tracker.record(b"\xcc" * 16)
        hashes = tracker.get_call_capable_hashes()
        assert hashes == {"cc" * 16}
        peers = tracker.get_peers()
        assert peers[0]["announce_count"] == 5


class TestFlushBehavior:
    def test_flush_writes_when_dirty(self, tracker, tmp_path):
        tracker.record(b"\xaa" * 16)
        tracker._flush_if_dirty()
        assert tracker.persist_calls == 1
        with open(os.path.join(str(tmp_path), "call_peers.json")) as fh:
            data = json.load(fh)
        assert "aa" * 16 in data

    def test_flush_is_no_op_when_clean(self, tracker):
        tracker._flush_if_dirty()
        assert tracker.persist_calls == 0

    def test_flush_clears_dirty_on_success(self, tracker):
        tracker.record(b"\xaa" * 16)
        assert tracker._dirty is True
        tracker._flush_if_dirty()
        assert tracker._dirty is False


class TestPersistedStateSurvivesReload:
    def test_reload_recovers_call_capable_hashes(self, tmp_path):
        first = CallPeerTracker(str(tmp_path))
        first.record(b"\xdd" * 16)
        first._flush_if_dirty()

        second = CallPeerTracker(str(tmp_path))
        assert second.get_call_capable_hashes() == {"dd" * 16}


class TestAnnounceHandler:
    """_CallAnnounceHandler — the RNS.Transport.register_announce_handler
    glue. aspect_filter must be exactly "lxst.telephony" (verified
    against LXST's own source, not guessed — see this module's own doc
    comment) or a real announce handler registration would silently
    listen for the wrong thing."""

    def test_aspect_filter_is_lxst_telephony(self, tracker):
        handler = tracker.register_announce_handler()
        assert handler.aspect_filter == "lxst.telephony"

    def test_received_announce_records_the_identity_hash(self, tracker):
        handler = tracker.register_announce_handler()

        class _FakeIdentity:
            hash = b"\xee" * 16

        handler.received_announce(
            destination_hash=b"\x11" * 16,
            announced_identity=_FakeIdentity(),
            app_data=None,
        )

        assert tracker.get_call_capable_hashes() == {"ee" * 16}

    def test_received_announce_with_no_identity_does_not_crash(self, tracker):
        handler = tracker.register_announce_handler()
        handler.received_announce(
            destination_hash=b"\x11" * 16,
            announced_identity=None,
            app_data=None,
        )
        assert tracker.get_call_capable_hashes() == set()


class TestPersistIntervalConstant:
    def test_persist_interval_is_at_least_10s(self):
        # Same floor/rationale as LXMFPeerTracker's own test.
        assert CallPeerTracker.PERSIST_INTERVAL_S >= 10

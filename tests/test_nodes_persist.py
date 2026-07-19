"""Tests for ``NodeBrowser``'s debounced ``nodes.json`` persistence.

Same pathology as the LXMFPeerTracker's inline persist: every
``nomadnetwork.node`` announce (and every fetch stat update) used to
call ``self._persist(snapshot)`` synchronously on the RNS read_loop
thread. On the mirror deployment (~2k nodes) on NAS-backed
``/config``, this held the GIL through ``json.dump`` and gridlocked
outbound LINK_PROOFs — which the mirror needs to send to accept
inbound page-browse handshakes from clients.

These tests bypass ``NodeBrowser.__init__`` (which spins up RNS
bookkeeping) using a stub that reuses the real methods.
"""

import json
import os
import threading

import pytest

from nomadnet_web.browser import NodeBrowser


class _StubBrowser:
    """Minimum viable NodeBrowser-shaped object. Rebinds the real
    methods so we exercise the actual implementation, not a copy.
    """
    _mark_nodes_dirty     = NodeBrowser._mark_nodes_dirty
    _flush_nodes_if_dirty = NodeBrowser._flush_nodes_if_dirty
    _persist              = NodeBrowser._persist

    def __init__(self, storage_dir):
        self._nodes_file = os.path.join(storage_dir, "nodes.json")
        self._lock = threading.Lock()
        self.nodes: dict = {}
        self._nodes_dirty = False
        self._nodes_dirty_lock = threading.Lock()
        # Track call counts so we can assert on batching.
        self.persist_calls = 0
        # Wrap _persist to count calls, delegating to the real impl.
        def counted(snapshot):
            self.persist_calls += 1
            NodeBrowser._persist(self, snapshot)
        self._persist = counted


@pytest.fixture
def browser(tmp_path):
    return _StubBrowser(str(tmp_path))


class TestMarkDirtyContract:
    """The hot-path callers (``_register_node``, ``_record_fetch``,
    ``_record_ping``, hop-refresh in ``get_nodes``) all call
    ``_mark_nodes_dirty``. That MUST NOT touch disk — the whole point
    of the refactor.
    """

    def test_mark_dirty_does_not_persist_inline(self, browser):
        assert browser.persist_calls == 0
        for i in range(100):
            browser._mark_nodes_dirty()
        # 100 marks, still zero disk writes.
        assert browser.persist_calls == 0

    def test_mark_dirty_sets_flag(self, browser):
        assert browser._nodes_dirty is False
        browser._mark_nodes_dirty()
        assert browser._nodes_dirty is True

    def test_multiple_marks_stay_dirty(self, browser):
        # Idempotent — many marks, still one dirty flag.
        for _ in range(5):
            browser._mark_nodes_dirty()
        assert browser._nodes_dirty is True


class TestFlushBehavior:
    def test_flush_writes_when_dirty(self, browser, tmp_path):
        browser.nodes["aa" * 16] = {"name": "test"}
        browser._mark_nodes_dirty()
        assert browser.persist_calls == 0
        browser._flush_nodes_if_dirty()
        assert browser.persist_calls == 1
        # File on disk contains the entry
        with open(os.path.join(str(tmp_path), "nodes.json")) as fh:
            data = json.load(fh)
        assert "aa" * 16 in data

    def test_flush_is_no_op_when_clean(self, browser):
        browser._flush_nodes_if_dirty()
        assert browser.persist_calls == 0

    def test_flush_clears_dirty_on_success(self, browser):
        browser._mark_nodes_dirty()
        browser._flush_nodes_if_dirty()
        assert browser._nodes_dirty is False

    def test_flush_batches_many_marks_into_one_write(self, browser):
        # This is the whole point: many hot-path mark_dirty calls,
        # one disk write. Historically this would have been 1000
        # writes; now it's 1.
        for i in range(1000):
            browser._mark_nodes_dirty()
        browser._flush_nodes_if_dirty()
        assert browser.persist_calls == 1

    def test_flush_re_marks_dirty_on_persist_failure(self, browser):
        # Simulate a disk failure — flush must set dirty back on so
        # the next tick retries. Matches the historical implicit
        # behaviour where a failed write just meant next announce
        # would try again.
        browser._mark_nodes_dirty()

        raises = [True]
        def failing_persist(snapshot):
            if raises[0]:
                raise IOError("simulated disk failure")

        browser._persist = failing_persist
        browser._flush_nodes_if_dirty()
        assert browser._nodes_dirty is True

        # Recovery: next flush works.
        raises[0] = False
        browser._flush_nodes_if_dirty()
        assert browser._nodes_dirty is False


class TestPersistIntervalConstant:
    """``NODES_PERSIST_INTERVAL_S`` is load-bearing — if someone drops
    it small trying to be "safer" they reintroduce the bottleneck.
    """

    def test_persist_interval_at_least_10s(self):
        from nomadnet_web.browser import NODES_PERSIST_INTERVAL_S
        # Below ~10s the whole point of batching evaporates on a busy
        # mesh. 60s is the shipped default; the floor here is
        # deliberately lower so future tuning is possible without
        # this test screaming, but a "NODES_PERSIST_INTERVAL_S = 1"
        # mistake gets caught.
        assert NODES_PERSIST_INTERVAL_S >= 10

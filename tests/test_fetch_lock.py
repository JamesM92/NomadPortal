"""Tests for ``NodeBrowser._get_fetch_lock`` — the per-destination
fetch mutex that prevents parallel Link handshakes to the same peer.

We were observing three concurrent Link requests firing to the same
destination in the same second whenever multiple ``fetch_page`` calls
were waiting on the same announce — three different waiters, one
announce, three simultaneous handshake attempts. Peers respond
poorly to that. The mutex serializes fetches per destination.

These tests bypass the full ``NodeBrowser.__init__`` (which spins up
threading and RNS bookkeeping) by using a lightweight stub that
carries only the two state fields ``_get_fetch_lock`` reads. The
method itself is pulled off the real class so we test the actual
implementation, not a copy.
"""

import threading
import time

from nomadnet_web.browser import NodeBrowser


DEST_A = b"\x49\xc4\x5a\x46" + b"\x00" * 12
DEST_B = b"\xde\xad\xbe\xef" + b"\x00" * 12


class _StubBrowser:
    """Minimum viable stand-in for NodeBrowser. Reuses the real
    ``_get_fetch_lock`` implementation so we're exercising the
    same code the running app does.
    """
    _get_fetch_lock = NodeBrowser._get_fetch_lock

    def __init__(self):
        self._inflight_fetches: dict = {}
        self._inflight_fetches_lock = threading.Lock()


class TestGetFetchLock:
    def test_returns_same_lock_for_same_destination(self):
        b = _StubBrowser()
        assert b._get_fetch_lock(DEST_A) is b._get_fetch_lock(DEST_A)

    def test_returns_different_locks_for_different_destinations(self):
        b = _StubBrowser()
        assert b._get_fetch_lock(DEST_A) is not b._get_fetch_lock(DEST_B)

    def test_lock_is_a_real_threading_lock(self):
        # If this ever drifts to a Semaphore or RLock, the fetch
        # serialization contract changes silently — RLock would
        # let the SAME thread re-enter, defeating "one handshake
        # at a time to the same peer." A plain Lock refuses.
        b = _StubBrowser()
        lock = b._get_fetch_lock(DEST_A)
        # threading.Lock() returns a `_thread.lock` internally; the
        # canonical way to assert its identity is via its acquire/
        # release interface plus a re-entry check.
        assert lock.acquire(blocking=False) is True
        try:
            # Re-entry from the same thread should be REJECTED —
            # RLock would allow it.
            assert lock.acquire(blocking=False) is False
        finally:
            lock.release()


class TestSerializationBehavior:
    def test_second_acquisition_blocks_until_first_released(self):
        b = _StubBrowser()
        lock = b._get_fetch_lock(DEST_A)

        first_started = threading.Event()
        second_finished = threading.Event()
        release_first = threading.Event()

        acquisition_times = []

        def hold_lock():
            with lock:
                first_started.set()
                release_first.wait(timeout=2.0)

        def wait_for_lock():
            first_started.wait(timeout=1.0)
            t0 = time.monotonic()
            with lock:
                acquisition_times.append(time.monotonic() - t0)
            second_finished.set()

        t_hold = threading.Thread(target=hold_lock, daemon=True)
        t_wait = threading.Thread(target=wait_for_lock, daemon=True)
        t_hold.start()
        t_wait.start()

        # Let the waiter accumulate blocked time.
        time.sleep(0.1)
        # Prove the waiter is still blocked (hasn't recorded acquisition).
        assert not acquisition_times
        # Now release the holder and let the waiter through.
        release_first.set()
        assert second_finished.wait(timeout=2.0)

        # The waiter should have blocked at least ~100ms (the sleep above).
        assert acquisition_times[0] >= 0.09

    def test_different_destinations_do_not_block(self):
        b = _StubBrowser()

        holder_ready = threading.Event()
        release = threading.Event()
        other_acquired = threading.Event()

        def hold_a():
            with b._get_fetch_lock(DEST_A):
                holder_ready.set()
                release.wait(timeout=2.0)

        def acquire_b():
            holder_ready.wait(timeout=1.0)
            with b._get_fetch_lock(DEST_B):
                other_acquired.set()

        t1 = threading.Thread(target=hold_a, daemon=True)
        t2 = threading.Thread(target=acquire_b, daemon=True)
        t1.start()
        t2.start()

        # DEST_B acquisition should complete promptly — it's not
        # contending with DEST_A's lock. If it doesn't fire within
        # a short window, the mutex is over-serialized (blocking
        # ALL destinations behind one lock).
        assert other_acquired.wait(timeout=1.0)
        release.set()

"""Tests for ``browser._DestinationAnnounceWaiter``.

The waiter is the per-fetch announce handler ``fetch_page`` registers
so its retry loop can wake up the moment a fresh announce for the
target destination arrives. It's a small class but has three
load-bearing pieces: a class attribute (``receive_path_responses``),
an aspect string that matches what NomadNet nodes announce as, and
the destination-hash filter inside ``received_announce``.

Historically, ``receive_path_responses = True`` was omitted at first
and the waiter silently missed every path response — the exact
announces we most needed to catch. This test file exists to make
that specific class of miss impossible to reintroduce silently.
"""

import threading
import time

import pytest

from nomadnet_web.browser import _DestinationAnnounceWaiter


TARGET = b"\x49\xc4\x5a\x46" + b"\x00" * 12
OTHER  = b"\xde\xad\xbe\xef" + b"\x00" * 12


class TestClassContract:
    """Class-level attributes RNS's Transport dispatch inspects.

    RNS's ``Transport`` announce-dispatch loop reads these off the
    handler *class* (not the instance) to decide whether to forward an
    announce. If any of them drifts, we lose announces silently — no
    log, no exception, just an event that never fires.
    """

    def test_aspect_filter_matches_nomadnet_node(self):
        # NomadNet node announces use the aspect "nomadnetwork.node".
        # A waiter with any other aspect string would silently miss
        # the announces the retry loop is waiting for.
        assert _DestinationAnnounceWaiter.aspect_filter == "nomadnetwork.node"

    def test_receive_path_responses_is_true(self):
        # Without this, RNS.Transport's dispatch loop drops PATH_RESPONSE
        # announces — the specific announces we need to catch, because a
        # NomadNet node's re-announce in reply to our request_path arrives
        # as a path response. Absent this flag the waiter never wakes on
        # the fresh-answer case that motivated the whole retry redesign.
        assert _DestinationAnnounceWaiter.receive_path_responses is True


class TestReceivedAnnounce:
    """The instance-level filter: only match the exact target hash."""

    def test_sets_event_on_matching_destination(self):
        waiter = _DestinationAnnounceWaiter(TARGET)
        assert not waiter.event.is_set()
        waiter.received_announce(TARGET, announced_identity=None, app_data=None)
        assert waiter.event.is_set()

    def test_ignores_non_matching_destination(self):
        waiter = _DestinationAnnounceWaiter(TARGET)
        waiter.received_announce(OTHER, announced_identity=None, app_data=None)
        assert not waiter.event.is_set()

    def test_ignores_extra_announces_after_first_match(self):
        # Once the event is set, further announces are effectively no-ops
        # until the caller resets. This matches the "waiter fires once
        # per wait cycle" contract that wait_and_reset assumes.
        waiter = _DestinationAnnounceWaiter(TARGET)
        waiter.received_announce(TARGET, None, None)
        waiter.received_announce(TARGET, None, None)
        assert waiter.event.is_set()


class TestWaitAndReset:
    """``wait_and_reset`` is the retry loop's actual entry point."""

    def test_returns_false_on_timeout(self):
        waiter = _DestinationAnnounceWaiter(TARGET)
        t0 = time.monotonic()
        result = waiter.wait_and_reset(timeout=0.05)
        elapsed = time.monotonic() - t0
        assert result is False
        assert 0.04 <= elapsed <= 0.5

    def test_returns_true_when_event_already_set(self):
        waiter = _DestinationAnnounceWaiter(TARGET)
        waiter.received_announce(TARGET, None, None)
        result = waiter.wait_and_reset(timeout=0.05)
        assert result is True

    def test_returns_true_when_event_arrives_during_wait(self):
        waiter = _DestinationAnnounceWaiter(TARGET)

        # Fire the announce from another thread partway through the wait.
        # This exercises the actual retry-loop path: the loop is blocked
        # on wait_and_reset when an announce arrives on RNS's dispatch
        # thread, and the wait needs to unblock promptly rather than
        # sitting through the full timeout.
        def fire_after_delay():
            time.sleep(0.05)
            waiter.received_announce(TARGET, None, None)

        threading.Thread(target=fire_after_delay, daemon=True).start()

        t0 = time.monotonic()
        result = waiter.wait_and_reset(timeout=5.0)
        elapsed = time.monotonic() - t0
        assert result is True
        # Woke well before the timeout — proves the event actually
        # unblocked the wait rather than the wait timing out coincident
        # with the event.
        assert elapsed < 1.0

    def test_clears_event_after_wake(self):
        # Between retry attempts the loop reuses the waiter. Without
        # this reset, an announce that arrived during attempt N would
        # falsely trigger an immediate retry between attempts N+1 and
        # N+2 without any new announce actually arriving.
        waiter = _DestinationAnnounceWaiter(TARGET)
        waiter.received_announce(TARGET, None, None)
        assert waiter.wait_and_reset(timeout=0.05) is True
        assert not waiter.event.is_set()
        # Second wait with no new announce should now time out.
        assert waiter.wait_and_reset(timeout=0.05) is False

    def test_clears_event_after_timeout_too(self):
        # Even on timeout the event is cleared. Not strictly load-bearing
        # for correctness (there's nothing to clear if it never fired),
        # but keeps the reset semantics symmetric so callers can rely
        # on "post-wait the flag is always fresh."
        waiter = _DestinationAnnounceWaiter(TARGET)
        waiter.wait_and_reset(timeout=0.05)
        assert not waiter.event.is_set()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

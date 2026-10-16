"""Tests for ``KioskCache`` — the disk-backed last-good page cache that
lets the public kiosk keep serving content when the mesh is down.

Policy under test (agreed in the 2026-10 roadmap): serve any cached copy
regardless of age, cap total stored bytes, evict least-recently-accessed
first (pre-warmed entries before visitor-requested ones), and drop
anything not accessed for 6 months. All tests use ``tmp_path`` so nothing
touches a real ``config/`` directory.
"""

import os

import pytest

from nomadnet_web.kiosk_cache import KioskCache, SIX_MONTHS_S, kiosk_cacheable, kiosk_key


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


def make(tmp_path, clock, max_bytes=1000):
    return KioskCache(str(tmp_path), max_bytes=max_bytes, clock=clock)


class TestRoundTrip:
    def test_set_then_get(self, tmp_path, clock):
        c = make(tmp_path, clock)
        c.set("node:/page/index.mu", b"hello")
        got = c.get("node:/page/index.mu")
        assert got is not None
        assert got.content == b"hello"

    def test_miss_returns_none(self, tmp_path, clock):
        assert make(tmp_path, clock).get("nope") is None

    def test_entry_reports_age_from_last_refresh(self, tmp_path, clock):
        c = make(tmp_path, clock)
        c.set("k", b"v")
        clock.advance(3600)
        assert c.get("k").age_s == 3600

    def test_set_again_refreshes_stored_at(self, tmp_path, clock):
        c = make(tmp_path, clock)
        c.set("k", b"old")
        clock.advance(500)
        c.set("k", b"new")
        got = c.get("k")
        assert got.content == b"new"
        assert got.age_s == 0

    def test_old_entries_are_still_served(self, tmp_path, clock):
        # No TTL: a 90-day-old copy is served as long as it was accessed
        # within the 6-month window.
        c = make(tmp_path, clock)
        c.set("k", b"v")
        clock.advance(90 * 86400)
        assert c.get("k").content == b"v"


class TestPersistence:
    def test_survives_restart(self, tmp_path, clock):
        make(tmp_path, clock).set("k", b"persisted")
        again = make(tmp_path, clock)
        assert again.get("k").content == b"persisted"

    def test_corrupt_index_starts_empty_without_crashing(self, tmp_path, clock):
        make(tmp_path, clock).set("k", b"v")
        index = os.path.join(str(tmp_path), "kiosk_cache", "index.json")
        with open(index, "w") as fh:
            fh.write("{not json")
        assert make(tmp_path, clock).get("k") is None

    def test_index_entry_with_missing_blob_is_a_miss(self, tmp_path, clock):
        c = make(tmp_path, clock)
        c.set("k", b"v")
        blobs = os.path.join(str(tmp_path), "kiosk_cache", "blobs")
        for name in os.listdir(blobs):
            os.remove(os.path.join(blobs, name))
        assert make(tmp_path, clock).get("k") is None


class TestSizeCap:
    def test_total_bytes_never_exceeds_cap(self, tmp_path, clock):
        c = make(tmp_path, clock, max_bytes=100)
        for i in range(10):
            clock.advance(1)
            c.set(f"k{i}", b"x" * 30)
        assert c.stats()["bytes"] <= 100

    def test_evicts_least_recently_accessed_first(self, tmp_path, clock):
        c = make(tmp_path, clock, max_bytes=100)
        c.set("a", b"x" * 40)
        clock.advance(1)
        c.set("b", b"x" * 40)
        clock.advance(1)
        c.get("a")                      # a is now newer than b
        clock.advance(1)
        c.set("c", b"x" * 40)           # forces one eviction
        assert c.get("b") is None
        assert c.get("a") is not None
        assert c.get("c") is not None

    def test_prewarmed_entries_go_before_requested_ones(self, tmp_path, clock):
        c = make(tmp_path, clock, max_bytes=100)
        c.set("requested", b"x" * 40)                    # oldest
        clock.advance(1)
        c.set("warm", b"x" * 40, priority="prewarm")     # newer, but low priority
        clock.advance(1)
        c.set("new", b"x" * 40)
        assert c.get("warm") is None
        assert c.get("requested") is not None

    def test_single_oversize_entry_is_not_stored(self, tmp_path, clock):
        c = make(tmp_path, clock, max_bytes=50)
        c.set("big", b"x" * 51)
        assert c.get("big") is None
        assert c.stats()["bytes"] == 0

    def test_replacing_an_entry_does_not_double_count(self, tmp_path, clock):
        c = make(tmp_path, clock, max_bytes=100)
        c.set("k", b"x" * 60)
        c.set("k", b"x" * 60)
        assert c.get("k") is not None
        assert c.stats()["bytes"] == 60


class TestExpiry:
    def test_unaccessed_for_six_months_is_dropped(self, tmp_path, clock):
        c = make(tmp_path, clock)
        c.set("k", b"v")
        clock.advance(SIX_MONTHS_S + 1)
        assert c.get("k") is None

    def test_access_resets_the_six_month_clock(self, tmp_path, clock):
        c = make(tmp_path, clock)
        c.set("k", b"v")
        clock.advance(SIX_MONTHS_S - 10)
        assert c.get("k") is not None
        clock.advance(SIX_MONTHS_S - 10)
        assert c.get("k") is not None

    def test_expired_blob_is_removed_from_disk(self, tmp_path, clock):
        c = make(tmp_path, clock)
        c.set("k", b"v")
        clock.advance(SIX_MONTHS_S + 1)
        c.purge_expired()
        assert os.listdir(os.path.join(str(tmp_path), "kiosk_cache", "blobs")) == []


class TestMaintenance:
    def test_invalidate_removes_one_entry(self, tmp_path, clock):
        c = make(tmp_path, clock)
        c.set("a", b"1")
        c.set("b", b"2")
        c.invalidate("a")
        assert c.get("a") is None
        assert c.get("b") is not None

    def test_clear_empties_everything(self, tmp_path, clock):
        c = make(tmp_path, clock)
        c.set("a", b"1")
        c.clear()
        assert c.stats() == {"entries": 0, "bytes": 0}

    def test_keys_with_path_separators_stay_inside_the_cache_dir(self, tmp_path, clock):
        c = make(tmp_path, clock)
        c.set("../../etc/passwd", b"nope")
        for root, _dirs, files in os.walk(str(tmp_path)):
            assert root.startswith(str(tmp_path))
        assert c.get("../../etc/passwd").content == b"nope"
        assert not os.path.exists(os.path.join(os.path.dirname(str(tmp_path)), "etc"))


class TestCacheEligibility:
    """The cache is shared across all visitors, so a fetch qualifies only
    if its result cannot depend on who made it."""

    def test_plain_anonymous_get_qualifies(self):
        assert kiosk_cacheable(None, None) is True
        assert kiosk_cacheable({}, None) is True

    def test_form_submission_never_qualifies(self):
        assert kiosk_cacheable({"name": "x"}, None) is False

    def test_identified_fetch_never_qualifies(self):
        assert kiosk_cacheable(None, object()) is False

    def test_key_is_case_insensitive_on_node_hash(self):
        assert kiosk_key("ABCDEF", "/page/a.mu") == kiosk_key("abcdef", "/page/a.mu")

    def test_key_separates_paths(self):
        assert kiosk_key("ab", "/a.mu") != kiosk_key("ab", "/b.mu")

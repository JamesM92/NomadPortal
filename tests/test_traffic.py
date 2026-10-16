"""Tests for ``TrafficCounters`` — aggregate page-view counts for the
operator. The privacy contract is part of the tests: nothing about a
visitor is recorded, only counts per page per day."""

import json
import os

import pytest

from nomadnet_web.traffic import TrafficCounters

NODE = "ab" * 16
DAY = 86400


class Clock:
    def __init__(self, now=1_760_000_000.0):   # a fixed UTC instant
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def make(tmp_path, clock, **kw):
    kw.setdefault("flush_interval_s", 0)
    return TrafficCounters(str(tmp_path), clock=clock, **kw)


class TestCounting:
    def test_counts_live_and_saved_per_page(self, tmp_path, clock):
        t = make(tmp_path, clock)
        t.record(NODE, "/page/index.mu", "live")
        t.record(NODE, "/page/index.mu", "saved")
        t.record(NODE, "/page/index.mu", "saved")
        top = t.summary(days=1)["top"]
        assert top == [{"page": f"{NODE}:/page/index.mu", "live": 1, "saved": 2, "total": 3}]

    def test_unknown_source_is_ignored(self, tmp_path, clock):
        t = make(tmp_path, clock)
        t.record(NODE, "/page/a.mu", "weird")
        assert t.summary(days=1)["top"] == []

    def test_node_hash_is_case_folded(self, tmp_path, clock):
        t = make(tmp_path, clock)
        t.record(NODE.upper(), "/p", "live")
        t.record(NODE, "/p", "live")
        assert t.summary(days=1)["top"][0]["total"] == 2

    def test_days_are_separate_and_newest_first(self, tmp_path, clock):
        t = make(tmp_path, clock)
        t.record(NODE, "/a", "live")
        clock.now += DAY
        t.record(NODE, "/a", "live")
        t.record(NODE, "/a", "saved")
        days = t.summary(days=7)["days"]
        assert [d["total"] for d in days] == [2, 1]
        assert days[0]["day"] > days[1]["day"]

    def test_summary_window_excludes_older_days(self, tmp_path, clock):
        t = make(tmp_path, clock)
        t.record(NODE, "/old", "live")
        clock.now += 10 * DAY
        t.record(NODE, "/new", "live")
        pages = [p["page"] for p in t.summary(days=3)["top"]]
        assert pages == [f"{NODE}:/new"]

    def test_top_pages_sorted_by_total_and_limited(self, tmp_path, clock):
        t = make(tmp_path, clock)
        for i in range(30):
            for _ in range(i + 1):
                t.record(NODE, f"/p{i}", "live")
        top = t.summary(days=1, limit=5)["top"]
        assert len(top) == 5
        assert [p["total"] for p in top] == [30, 29, 28, 27, 26]


class TestBoundedGrowth:
    def test_distinct_pages_per_day_are_capped_into_other(self, tmp_path, clock):
        t = make(tmp_path, clock, max_keys_per_day=3)
        for i in range(10):
            t.record(NODE, f"/p{i}", "live")
        pages = {p["page"]: p["total"] for p in t.summary(days=1, limit=50)["top"]}
        assert len(pages) == 4 and pages["other"] == 7

    def test_a_known_page_still_counts_after_the_cap_is_hit(self, tmp_path, clock):
        t = make(tmp_path, clock, max_keys_per_day=1)
        t.record(NODE, "/a", "live")
        t.record(NODE, "/b", "live")       # -> other
        t.record(NODE, "/a", "live")
        pages = {p["page"]: p["total"] for p in t.summary(days=1)["top"]}
        assert pages[f"{NODE}:/a"] == 2

    def test_old_days_are_pruned_past_retention(self, tmp_path, clock):
        t = make(tmp_path, clock, retention_days=90)
        t.record(NODE, "/a", "live")
        clock.now += 100 * DAY
        t.record(NODE, "/b", "live")
        stored = json.load(open(os.path.join(str(tmp_path), "traffic.json")))
        assert len(stored) == 1


class TestPersistence:
    def test_survives_restart(self, tmp_path, clock):
        make(tmp_path, clock).record(NODE, "/a", "saved")
        again = make(tmp_path, clock)
        assert again.summary(days=1)["top"][0]["saved"] == 1

    def test_corrupt_file_starts_empty(self, tmp_path, clock):
        (tmp_path / "traffic.json").write_text("{nope")
        assert make(tmp_path, clock).summary(days=1)["top"] == []

    def test_flush_is_throttled_but_explicit_flush_catches_up(self, tmp_path, clock):
        t = make(tmp_path, clock, flush_interval_s=60)
        path = os.path.join(str(tmp_path), "traffic.json")

        def stored_total():
            data = json.load(open(path))
            return sum(sum(v) for day in data.values() for v in day.values())

        t.record(NODE, "/a", "live")           # first write goes out at once
        assert stored_total() == 1
        t.record(NODE, "/a", "live")           # within the interval: held back
        assert stored_total() == 1
        t.flush()
        assert stored_total() == 2

    def test_stored_data_holds_only_counts_per_page_per_day(self, tmp_path, clock):
        make(tmp_path, clock).record(NODE, "/a", "live")
        raw = (tmp_path / "traffic.json").read_text()
        data = json.loads(raw)
        (day, pages), = data.items()
        assert len(day) == 10 and day.count("-") == 2            # YYYY-MM-DD
        assert pages == {f"{NODE}:/a": [1, 0]}

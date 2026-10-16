"""Tests for the kiosk pre-warmer: it walks a prescribed node's landing
page and its same-node links and stores them in the kiosk cache, quietly
and at low priority, so a first-time visitor rarely hits a cold load.

The fetcher, cache clock and sleeping are all injected, so no Reticulum
stack or real waiting is involved.
"""

import threading

import pytest

from nomadnet_web.kiosk_cache import KioskCache, kiosk_key
from nomadnet_web.prewarm import PreWarmer, extract_page_links

H = "ab" * 16
O = "cd" * 16


def link(label, dest):
    return f"`[{label}`{dest}]"


class TestExtractPageLinks:
    def test_relative_and_absolute_same_node_links(self):
        text = "\n".join([
            link("a", "a.mu"), link("b", "/page/b.mu"), link("c", ":/page/c.mu"),
            link("d", f"{H}:/page/d.mu"),
        ])
        assert extract_page_links(text, H, "/page/index.mu") == [
            "/page/a.mu", "/page/b.mu", "/page/c.mu", "/page/d.mu"]

    def test_other_nodes_files_web_and_lxmf_links_are_skipped(self):
        text = "\n".join([
            link("o", f"{O}:/page/x.mu"), link("f", f"{H}:/file/a.zip"),
            link("w", "https://example.com"), link("ok", "ok.mu"),
        ])
        assert extract_page_links(text, H, "/page/index.mu") == ["/page/ok.mu"]

    def test_links_with_a_query_or_form_fields_are_skipped(self):
        text = "\n".join([
            link("q", "a.mu?x=1"), "`[form`a.mu`name=1|field]", link("ok", "ok.mu"),
        ])
        assert extract_page_links(text, H, "/page/index.mu") == ["/page/ok.mu"]

    def test_duplicates_removed_keeping_first_seen_order(self):
        text = link("1", "b.mu") + link("2", "a.mu") + link("3", "b.mu")
        assert extract_page_links(text, H, "/page/index.mu") == ["/page/b.mu", "/page/a.mu"]

    def test_no_links(self):
        assert extract_page_links(">just text", H, "/page/index.mu") == []


class Site:
    """A fake node: path -> micron text. Records every fetch."""

    def __init__(self, pages, fail=()):
        self.pages = pages
        self.fail = set(fail)
        self.calls = []

    def fetch(self, node, path):
        self.calls.append((node, path))
        if (node, path) in self.fail or path not in self.pages:
            return None, "Link closed before response"
        return self.pages[path].encode(), None


@pytest.fixture
def cache(tmp_path):
    return KioskCache(str(tmp_path))


def warmer(site, cache, nodes=(H,), **kw):
    kw.setdefault("gap_s", 0)
    return PreWarmer(site.fetch, cache, lambda: list(nodes), **kw)


SITE = {
    "/page/index.mu": link("a", "a.mu") + link("b", "b.mu"),
    "/page/a.mu": link("c", "c.mu"),
    "/page/b.mu": ">leaf",
    "/page/c.mu": link("d", "d.mu"),
    "/page/d.mu": ">too deep",
}


class TestWalk:
    def test_stores_landing_and_linked_pages(self, cache):
        site = Site(SITE)
        warmer(site, cache, depth=2).run_once()
        for path in ("/page/index.mu", "/page/a.mu", "/page/b.mu", "/page/c.mu"):
            assert cache.get(kiosk_key(H, path)) is not None, path

    def test_depth_limits_how_far_links_are_followed(self, cache):
        site = Site(SITE)
        warmer(site, cache, depth=1).run_once()
        assert cache.get(kiosk_key(H, "/page/a.mu")) is not None
        assert cache.get(kiosk_key(H, "/page/c.mu")) is None

    def test_depth_two_does_not_reach_the_third_hop(self, cache):
        site = Site(SITE)
        warmer(site, cache, depth=2).run_once()
        assert cache.get(kiosk_key(H, "/page/d.mu")) is None

    def test_max_pages_caps_the_walk(self, cache):
        site = Site(SITE)
        stats = warmer(site, cache, depth=5, max_pages=3).run_once()
        assert len(site.calls) == 3 and stats["fetched"] == 3

    def test_each_page_is_fetched_once_even_if_linked_twice(self, cache):
        pages = {"/page/index.mu": link("a", "a.mu") + link("b", "b.mu"),
                 "/page/a.mu": link("b", "b.mu"), "/page/b.mu": ">x"}
        site = Site(pages)
        warmer(site, cache, depth=3).run_once()
        assert sorted(p for _n, p in site.calls) == ["/page/a.mu", "/page/b.mu", "/page/index.mu"]

    def test_entries_are_stored_at_low_priority(self, cache):
        # Fill the cache so one entry must go; the pre-warmed one goes first.
        site = Site({"/page/index.mu": ">x" * 10})
        small = KioskCache(cache._root.rsplit("/kiosk_cache", 1)[0] + "/small", max_bytes=45)
        small.set("visitor", b"v" * 20)
        warmer(site, small, depth=0).run_once()
        small.set("another", b"v" * 20)
        assert small.get("visitor") is not None
        assert small.get(kiosk_key(H, "/page/index.mu")) is None


class TestFailures:
    def test_failed_page_is_not_cached_and_the_walk_continues(self, cache):
        site = Site(SITE, fail={(H, "/page/a.mu")})
        stats = warmer(site, cache, depth=2).run_once()
        assert cache.get(kiosk_key(H, "/page/a.mu")) is None
        assert cache.get(kiosk_key(H, "/page/b.mu")) is not None
        assert stats["failed"] == 1

    def test_landing_failure_skips_that_nodes_walk(self, cache):
        site = Site(SITE, fail={(H, "/page/index.mu")})
        stats = warmer(site, cache, depth=2).run_once()
        assert site.calls == [(H, "/page/index.mu")]
        assert stats["fetched"] == 0

    def test_a_failing_node_does_not_stop_the_next_one(self, cache):
        site = Site(SITE, fail={(H, "/page/index.mu")})
        pages = dict(SITE)
        site.pages = pages
        w = warmer(site, cache, nodes=(H, O), depth=0)
        w.run_once()
        assert (O, "/page/index.mu") in site.calls

    def test_mesh_not_ready_stops_the_whole_run(self, cache):
        calls = []

        def fetch(node, path):
            calls.append((node, path))
            return None, "Reticulum transport is still coming up"
        PreWarmer(fetch, cache, lambda: [H, O], gap_s=0).run_once()
        assert len(calls) == 1


class TestExistingCopies:
    def test_does_not_demote_a_page_a_visitor_asked_for(self, tmp_path):
        small = KioskCache(str(tmp_path), max_bytes=45)
        small.set(kiosk_key(H, "/page/index.mu"), b"v" * 20)      # visitor-requested
        site = Site({"/page/index.mu": ">" + "n" * 19})
        warmer(site, small, depth=0).run_once()
        small.set("filler", b"f" * 20)                             # forces an eviction
        assert small.get(kiosk_key(H, "/page/index.mu")) is not None


class TestScheduling:
    def test_no_nodes_means_no_fetches(self, cache):
        site = Site(SITE)
        assert warmer(site, cache, nodes=()).run_once()["fetched"] == 0
        assert site.calls == []

    def test_gap_between_fetches_is_honoured_and_stoppable(self, cache):
        site = Site(SITE)
        waited = []
        w = warmer(site, cache, depth=2, gap_s=2.5)
        w._wait = lambda s: waited.append(s) or False   # type: ignore[attr-defined]
        w.run_once()
        assert waited and all(s == 2.5 for s in waited)
        assert len(waited) == len(site.calls) - 1       # none before the first

    def test_stop_interrupts_a_run(self, cache):
        site = Site(SITE)
        w = warmer(site, cache, depth=2, gap_s=0)
        w._wait = lambda s: True                         # type: ignore[attr-defined]
        w.run_once()
        assert len(site.calls) == 1

    def test_interval_zero_disables_the_background_thread(self, cache):
        before = threading.active_count()
        w = warmer(Site(SITE), cache, interval_s=0)
        w.start()
        assert threading.active_count() == before


class TestEnvInt:
    def test_prefers_the_app_config_then_the_environment(self, monkeypatch):
        from nomadnet_web import _env_int
        monkeypatch.setenv("X_KNOB", "5")
        assert _env_int({"X_KNOB": "9"}, "X_KNOB", 1) == 9
        assert _env_int({}, "X_KNOB", 1) == 5
        monkeypatch.delenv("X_KNOB")
        assert _env_int({}, "X_KNOB", 1) == 1

    def test_a_bad_value_falls_back_instead_of_breaking_startup(self, monkeypatch):
        from nomadnet_web import _env_int
        monkeypatch.setenv("X_KNOB", "lots")
        assert _env_int({}, "X_KNOB", 3) == 3


class TestPartialsAndRefreshLinks:
    def test_partial_sources_are_followed(self):
        text = "`{/page/news.mu`30`pid=news}\n`{ticker.mu}\n"
        assert extract_page_links(text, H, "/page/index.mu") == ["/page/news.mu", "/page/ticker.mu"]

    def test_a_partial_with_request_fields_is_not_a_page(self):
        text = "`{/page/search.mu`0`pid=s|q=cats}\n"
        assert extract_page_links(text, H, "/page/index.mu") == []

    def test_partial_refresh_links_are_not_pages(self):
        text = link("Reload", "p:news") + link("Both", "p:news|ticker") + link("ok", "ok.mu")
        assert extract_page_links(text, H, "/page/index.mu") == ["/page/ok.mu"]

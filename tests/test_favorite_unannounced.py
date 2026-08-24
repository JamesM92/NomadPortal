"""Tests for ``NodeBrowser.set_favorite()`` favoriting a node this device
has never actually heard announce.

Motivation: index-page favorites (path="/", the node-list star) used to
unconditionally require ``self.nodes.get(hash_hex)`` to already exist,
even though page favorites (path != "/") were already allowed to create
one on the fly. That meant a node reached only via a manually-typed or
QR/link-carried address couldn't be starred until it happened to announce
first. Ported from the NomadPortal-Android sister project, which found
and fixed the same gap in its own fork of this module.

These tests bypass ``NodeBrowser.__init__`` (which spins up RNS) using a
lightweight stub that carries only the state ``set_favorite`` and its
helpers actually read.
"""

import json
import threading

import pytest

from nomadnet_web.browser import NodeBrowser


class _StubBrowser:
    """Rebinds the real methods so we exercise the actual impl."""
    set_favorite        = NodeBrowser.set_favorite
    _ensure_node_record  = NodeBrowser._ensure_node_record
    # staticmethod(...) preserves the no-implicit-self binding — copying
    # the plain unwrapped function (what `NodeBrowser._new_node_record`
    # evaluates to) would silently make it a normal *instance* method on
    # this stub class, shifting every positional argument by one.
    _new_node_record     = staticmethod(NodeBrowser._new_node_record)
    _mark_nodes_dirty    = NodeBrowser._mark_nodes_dirty
    _persist_favorites   = NodeBrowser._persist_favorites

    def __init__(self, favorites_file):
        self._lock            = threading.Lock()
        self.nodes: dict       = {}
        self._hosted_hash      = ""
        self._favorites: dict  = {}
        self._favorites_file   = str(favorites_file)
        self._nodes_dirty      = False
        self._nodes_dirty_lock = threading.Lock()


@pytest.fixture
def browser(tmp_path):
    return _StubBrowser(tmp_path / "favorites.json")


UNANNOUNCED_HASH = "aa" * 16


class TestFavoritingCreatesRecord:
    def test_favoriting_unannounced_node_index_succeeds(self, browser):
        ok = browser.set_favorite(UNANNOUNCED_HASH, True, user_sub="u1", path="/")
        assert ok is True

    def test_favoriting_unannounced_node_creates_minimal_record(self, browser):
        browser.set_favorite(UNANNOUNCED_HASH, True, user_sub="u1", path="/", name="My Node")
        record = browser.nodes[UNANNOUNCED_HASH]
        assert record["hash"] == UNANNOUNCED_HASH
        assert record["announce_count"] == 0    # no real announce has happened
        assert record["favorited"] is False      # per-user favorite, not the legacy anon flag

    def test_favoriting_unannounced_node_uses_given_name(self, browser):
        browser.set_favorite(UNANNOUNCED_HASH, True, user_sub="u1", path="/", name="My Node")
        assert browser.nodes[UNANNOUNCED_HASH]["name"] == "My Node"

    def test_favoriting_unannounced_node_falls_back_to_hash_prefix_name(self, browser):
        browser.set_favorite(UNANNOUNCED_HASH, True, user_sub="u1", path="/")
        assert browser.nodes[UNANNOUNCED_HASH]["name"] == UNANNOUNCED_HASH[:16] + "…"

    def test_favoriting_unannounced_node_marks_it_dirty_for_persistence(self, browser):
        browser.set_favorite(UNANNOUNCED_HASH, True, user_sub="u1", path="/")
        assert browser._nodes_dirty is True

    def test_favorite_is_actually_recorded(self, browser):
        browser.set_favorite(UNANNOUNCED_HASH, True, user_sub="u1", path="/")
        favs = browser._favorites["u1"]
        assert any(f["hash"] == UNANNOUNCED_HASH and f["path"] == "/" for f in favs)


class TestUnfavoritingStillRequiresExistingRecord:
    def test_unfavoriting_unannounced_node_declines(self, browser):
        # Nothing to remove — must not silently create a record just to
        # immediately have "no favorite" recorded against it.
        ok = browser.set_favorite(UNANNOUNCED_HASH, False, user_sub="u1", path="/")
        assert ok is False
        assert UNANNOUNCED_HASH not in browser.nodes


class TestAnnouncedNodeBehaviourUnchanged:
    def test_favoriting_an_already_known_node_does_not_touch_its_record(self, browser):
        browser.nodes[UNANNOUNCED_HASH] = {
            "hash": UNANNOUNCED_HASH, "name": "Real Node", "announce_count": 3,
            "favorited": False,
        }
        browser.set_favorite(UNANNOUNCED_HASH, True, user_sub="u1", path="/")
        # Real announce data must survive untouched — only a *missing*
        # record gets synthesized, an existing one is never overwritten.
        assert browser.nodes[UNANNOUNCED_HASH]["announce_count"] == 3
        assert browser.nodes[UNANNOUNCED_HASH]["name"] == "Real Node"


class TestPersistenceWritesRealFile:
    def test_favoriting_persists_to_disk(self, browser):
        browser.set_favorite(UNANNOUNCED_HASH, True, user_sub="u1", path="/")
        with open(browser._favorites_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert any(f["hash"] == UNANNOUNCED_HASH for f in data.get("u1", []))

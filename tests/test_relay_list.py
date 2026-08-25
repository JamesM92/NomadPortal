"""Tests for PropagationSyncService.list_known_nodes().

Motivation: this data already existed (every lxmf.propagation announce
heard was tracked in _known_nodes for the sync-node picker), but was
only ever exposed as an aggregate count via snapshot() — no way to list
the individual relay nodes. The Network tab's Relays list needs that;
this is the new public accessor for it.

Same "inject the rns module, don't import it" testability pattern this
codebase already uses elsewhere (see call_manager.py's own start()) —
_on_propagation_announce only touches rns.Transport.hops_to, so a
minimal fake covers it without needing real RNS.
"""

from nomadnet_web.lxmf_sync import PropagationSyncService

HASH_A = bytes.fromhex("aa" * 16)
HASH_B = bytes.fromhex("bb" * 16)


class _FakeTransport:
    def __init__(self, hops_by_hash=None):
        self._hops_by_hash = hops_by_hash or {}

    def hops_to(self, destination_hash):
        return self._hops_by_hash.get(destination_hash, 3)


class _FakeRns:
    def __init__(self, hops_by_hash=None):
        self.Transport = _FakeTransport(hops_by_hash)


def _service(hops_by_hash=None):
    return PropagationSyncService(rns=_FakeRns(hops_by_hash), messaging_service=None)


def test_list_known_nodes_empty_before_any_announce():
    svc = _service()
    assert svc.list_known_nodes() == []


def test_list_known_nodes_reflects_a_single_announce():
    svc = _service({HASH_A: 2})
    svc._on_propagation_announce(HASH_A, announced_identity=None, app_data=b"")

    nodes = svc.list_known_nodes()
    assert len(nodes) == 1
    entry = nodes[0]
    assert entry["hash"] == HASH_A.hex()
    assert entry["hops"] == 2
    assert entry["announce_count"] == 1
    assert entry["first_seen"] == entry["last_seen"]
    assert entry["picked"] is False


def test_repeat_announce_increments_count_and_updates_hops():
    svc = _service({HASH_A: 5})
    svc._on_propagation_announce(HASH_A, announced_identity=None, app_data=b"")
    first_seen = svc.list_known_nodes()[0]["first_seen"]

    svc._rns.Transport._hops_by_hash[HASH_A] = 1
    svc._on_propagation_announce(HASH_A, announced_identity=None, app_data=b"")

    entry = svc.list_known_nodes()[0]
    assert entry["announce_count"] == 2
    assert entry["hops"] == 1
    # first_seen is set once and never overwritten by later announces.
    assert entry["first_seen"] == first_seen


def test_list_known_nodes_covers_multiple_relays():
    svc = _service({HASH_A: 1, HASH_B: 4})
    svc._on_propagation_announce(HASH_A, announced_identity=None, app_data=b"")
    svc._on_propagation_announce(HASH_B, announced_identity=None, app_data=b"")

    hashes = {entry["hash"] for entry in svc.list_known_nodes()}
    assert hashes == {HASH_A.hex(), HASH_B.hex()}


def test_picked_flag_marks_only_the_currently_selected_node():
    svc = _service({HASH_A: 1, HASH_B: 4})
    svc._on_propagation_announce(HASH_A, announced_identity=None, app_data=b"")
    svc._on_propagation_announce(HASH_B, announced_identity=None, app_data=b"")
    svc._picked = HASH_A

    by_hash = {e["hash"]: e["picked"] for e in svc.list_known_nodes()}
    assert by_hash[HASH_A.hex()] is True
    assert by_hash[HASH_B.hex()] is False

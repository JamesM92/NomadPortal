"""Choosing a propagation node, syncing on demand, and reporting how the
last sync went (MeshChat parity; NomadNet 1.2.0 has the same: a node picker,
manual entry, and last sync time).

By default the service picks the nearest fresh node itself ("automatic").
A user can pin one instead, by choosing a node it has heard or typing a
hash. The choice is per account and survives a restart.
"""

import json
import time

import pytest

from nomadnet_web import lxmf_sync
from nomadnet_web.lxmf_sync import (
    MANUAL_SYNC_COOLDOWN_S, PropagationSyncService, transfer_state_name,
)

NEAR = bytes.fromhex("aa" * 16)
FAR = bytes.fromhex("bb" * 16)
MINE = "cc" * 16           # a node typed in by hand, never heard
U1, U2 = "user-1", "user-2"


class FakeRouter:
    def __init__(self, fail_on_request=None):
        self.outbound = None
        self.requests = 0
        self.fail_on_request = fail_on_request
        self.propagation_transfer_state = 0
        self.propagation_transfer_progress = 0.0
        self.propagation_transfer_last_result = None
        self.propagation_transfer_last_duplicates = None

    def set_outbound_propagation_node(self, node):
        self.outbound = node

    def request_messages_from_propagation_node(self, identity, max_messages=0):
        if self.fail_on_request:
            raise self.fail_on_request
        self.requests += 1


class FakeMessaging:
    def __init__(self, routers):
        self.routers = routers          # user_sub -> [FakeRouter]

    def active_routers(self):
        return [(sub, {"router": r, "identity": object()})
                for sub, rs in self.routers.items() for r in rs]


class FakeRns:
    class Transport:
        @staticmethod
        def hops_to(_h):
            return 2


@pytest.fixture
def env(tmp_path):
    r1, r2 = FakeRouter(), FakeRouter()
    msg = FakeMessaging({U1: [r1], U2: [r2]})
    svc = PropagationSyncService(FakeRns(), msg, storage_dir=str(tmp_path))
    svc._on_propagation_announce(NEAR, None, b"")
    svc._on_propagation_announce(FAR, None, b"")
    svc._picked = NEAR
    return svc, r1, r2, tmp_path


class TestPreferredNode:
    def test_automatic_by_default(self, env):
        svc, *_ = env
        assert svc.preferred_for(U1) is None and svc.effective_node_for(U1) == NEAR

    def test_a_pinned_node_is_used_for_that_user_only(self, env):
        svc, r1, r2, _ = env
        svc.set_preferred(U1, FAR.hex())
        svc._tick()
        assert r1.outbound == FAR and r2.outbound == NEAR
        assert r1.requests == 1 and r2.requests == 1

    def test_a_node_typed_in_by_hand_is_accepted_even_if_never_heard(self, env):
        svc, r1, *_ = env
        svc.set_preferred(U1, MINE.upper())          # case does not matter
        assert svc.preferred_for(U1) == bytes.fromhex(MINE)
        svc._tick()
        assert r1.outbound == bytes.fromhex(MINE)

    def test_clearing_returns_to_automatic(self, env):
        svc, r1, *_ = env
        svc.set_preferred(U1, FAR.hex())
        svc.set_preferred(U1, None)
        svc.set_preferred(U1, "")                    # an empty string means the same
        svc._tick()
        assert svc.preferred_for(U1) is None and r1.outbound == NEAR

    @pytest.mark.parametrize("bad", ["zz" * 16, "aa" * 15, "aa" * 17, "not hex", "aa" * 16 + "0", "0x" + "aa" * 15, 5])
    def test_a_bad_hash_is_refused_and_changes_nothing(self, env, bad):
        svc, *_ = env
        svc.set_preferred(U1, FAR.hex())
        with pytest.raises(ValueError):
            svc.set_preferred(U1, bad)
        assert svc.preferred_for(U1) == FAR

    def test_a_pinned_node_works_before_any_node_has_been_heard(self, tmp_path):
        r = FakeRouter()
        svc = PropagationSyncService(FakeRns(), FakeMessaging({U1: [r]}), storage_dir=str(tmp_path))
        svc.set_preferred(U1, MINE)
        svc._tick()
        assert r.requests == 1

    def test_with_nothing_to_use_nobody_is_synced(self, tmp_path):
        r = FakeRouter()
        svc = PropagationSyncService(FakeRns(), FakeMessaging({U1: [r]}), storage_dir=str(tmp_path))
        svc._tick()
        assert r.requests == 0

    def test_the_choice_survives_a_restart(self, env):
        svc, *_ , tmp = env
        svc.set_preferred(U1, FAR.hex())
        again = PropagationSyncService(FakeRns(), FakeMessaging({}), storage_dir=str(tmp))
        assert again.preferred_for(U1) == FAR and again.preferred_for(U2) is None

    def test_a_damaged_preferences_file_is_ignored(self, tmp_path):
        (tmp_path / "propagation_prefs.json").write_text("{not json")
        svc = PropagationSyncService(FakeRns(), FakeMessaging({}), storage_dir=str(tmp_path))
        assert svc.preferred_for(U1) is None

    def test_junk_entries_in_the_file_are_dropped(self, tmp_path):
        (tmp_path / "propagation_prefs.json").write_text(json.dumps({U1: "nope", U2: FAR.hex(), "x": 7}))
        svc = PropagationSyncService(FakeRns(), FakeMessaging({}), storage_dir=str(tmp_path))
        assert svc.preferred_for(U1) is None and svc.preferred_for(U2) == FAR

    def test_works_without_a_storage_directory(self):
        svc = PropagationSyncService(FakeRns(), FakeMessaging({}))
        svc.set_preferred(U1, FAR.hex())
        assert svc.preferred_for(U1) == FAR


class TestSyncNow:
    def test_syncs_only_that_users_routers(self, env):
        svc, r1, r2, _ = env
        out = svc.sync_now(U1)
        assert out["ok"] is True and out["node"] == NEAR.hex()
        assert r1.requests == 1 and r2.requests == 0

    def test_uses_the_pinned_node(self, env):
        svc, r1, *_ = env
        svc.set_preferred(U1, FAR.hex())
        assert svc.sync_now(U1)["node"] == FAR.hex() and r1.outbound == FAR

    def test_a_second_press_straight_away_is_refused(self, env):
        svc, r1, *_ = env
        assert svc.sync_now(U1)["ok"] is True
        out = svc.sync_now(U1)
        assert out["ok"] is False and "wait" in out["error"].lower() and r1.requests == 1

    def test_the_wait_ends(self, env, monkeypatch):
        svc, r1, *_ = env
        t = [1000.0]
        monkeypatch.setattr(lxmf_sync.time, "time", lambda: t[0])
        svc.sync_now(U1)
        t[0] += MANUAL_SYNC_COOLDOWN_S + 1
        assert svc.sync_now(U1)["ok"] is True and r1.requests == 2

    def test_one_users_press_does_not_block_another(self, env):
        svc, *_ = env
        svc.sync_now(U1)
        assert svc.sync_now(U2)["ok"] is True

    def test_no_node_known_says_what_to_do(self, tmp_path):
        svc = PropagationSyncService(FakeRns(), FakeMessaging({U1: [FakeRouter()]}), storage_dir=str(tmp_path))
        out = svc.sync_now(U1)
        assert out["ok"] is False and "enter one" in out["error"]

    def test_a_user_with_no_delivery_identity(self, env):
        svc, *_ = env
        out = svc.sync_now("someone-else")
        assert out["ok"] is False and "identity" in out["error"].lower()

    def test_a_failing_router_reports_failure_not_an_exception(self, tmp_path):
        r = FakeRouter(fail_on_request=RuntimeError("link refused"))
        svc = PropagationSyncService(FakeRns(), FakeMessaging({U1: [r]}), storage_dir=str(tmp_path))
        svc._on_propagation_announce(NEAR, None, b"")
        svc._picked = NEAR
        out = svc.sync_now(U1)
        assert out["ok"] is False and "link refused" in out["error"]
        assert "link refused" in svc.status_for(U1)["last_error"]


class TestStatus:
    def test_automatic(self, env):
        svc, *_ = env
        s = svc.status_for(U1)
        assert (s["mode"], s["node"], s["picked"], s["preferred"]) == ("auto", NEAR.hex(), NEAR.hex(), None)

    def test_pinned(self, env):
        svc, *_ = env
        svc.set_preferred(U1, FAR.hex())
        s = svc.status_for(U1)
        assert (s["mode"], s["node"], s["preferred"]) == ("manual", FAR.hex(), FAR.hex())

    def test_lists_known_nodes_with_the_pinned_one_marked(self, env):
        svc, *_ = env
        svc.set_preferred(U1, FAR.hex())
        by = {n["hash"]: n for n in svc.status_for(U1)["nodes"]}
        assert by[FAR.hex()]["preferred"] is True and by[NEAR.hex()]["preferred"] is False
        assert by[NEAR.hex()]["picked"] is True and by[NEAR.hex()]["hops"] == 2

    def test_a_pinned_node_that_was_never_heard_is_still_listed(self, env):
        svc, *_ = env
        svc.set_preferred(U1, MINE)
        entry = {n["hash"]: n for n in svc.status_for(U1)["nodes"]}[MINE]
        assert entry["preferred"] is True and entry["hops"] is None and entry["last_seen"] is None

    def test_other_users_choices_are_not_revealed(self, env):
        svc, *_ = env
        svc.set_preferred(U2, MINE)
        assert MINE not in [n["hash"] for n in svc.status_for(U1)["nodes"]]

    def test_last_sync_is_reported(self, env):
        svc, *_ = env
        assert svc.status_for(U1)["last_synced_at"] is None
        before = time.time()
        svc.sync_now(U1)
        s = svc.status_for(U1)
        assert s["last_synced_at"] >= before and s["last_error"] is None

    def test_transfer_state_and_result_come_from_the_router(self, env):
        svc, r1, *_ = env
        r1.propagation_transfer_state = 5
        r1.propagation_transfer_progress = 0.4
        t = svc.status_for(U1)["transfer"]
        assert (t["state"], t["active"], t["progress"]) == ("receiving messages", True, 0.4)
        r1.propagation_transfer_state = 7
        r1.propagation_transfer_last_result = 3
        r1.propagation_transfer_last_duplicates = 1
        t = svc.status_for(U1)["transfer"]
        assert (t["state"], t["active"], t["last_result"], t["last_duplicates"]) == ("complete", False, 3, 1)

    def test_a_router_without_transfer_attributes_is_tolerated(self, env):
        svc, r1, *_ = env
        for a in ("propagation_transfer_state", "propagation_transfer_progress",
                  "propagation_transfer_last_result", "propagation_transfer_last_duplicates"):
            delattr(r1, a)
        assert svc.status_for(U1)["transfer"]["state"] == "idle"

    def test_a_user_with_no_router_still_gets_a_status(self, env):
        svc, *_ = env
        s = svc.status_for("nobody")
        assert s["transfer"]["state"] == "idle" and s["nodes"]


class TestStateNames:
    @pytest.mark.parametrize("code,name,active", [
        (0, "idle", False), (1, "finding a path", True), (2, "connecting", True), (5, "receiving messages", True),
        (7, "complete", False), (254, "failed", False), (240, "no path to the node", False),
        (244, "the node refused access", False), (999, "unknown", False), (None, "idle", False),
    ])
    def test_names(self, code, name, active):
        got, act = transfer_state_name(code)
        assert got == name and act is active

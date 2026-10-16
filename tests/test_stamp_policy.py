"""Required stamp cost for incoming messages (NomadNet's
``required_stamp_cost``).

A stamp is proof-of-work a sender computes before a peer will take their
message: a cheap anti-spam tool. Two separate things are controlled:

- the *cost* (advertised in the identity's announces, so senders know to
  compute a stamp), and
- *enforcement* (without it, a message with a missing or bad stamp is
  still delivered and only logged).

Off by default, so nothing changes until an operator turns it on.
"""

import types

import pytest

from nomadnet_web.messaging import MessagingService
from nomadnet_web.stamps import MAX_STAMP_COST
from nomadnet_web.ui_settings import UISettings


class _Dest:
    def __init__(self, hexhash):
        self.hexhash = hexhash
        self.hash = bytes.fromhex(hexhash)
        self.display_name = ""
        self.stamp_cost = None


class _Router:
    instances = []

    def __init__(self, storagepath=None):
        self.registered_with = None
        self.inbound = {}                    # dest hash -> cost
        self.enforcing = False
        self.calls = []
        _Router.instances.append(self)

    def register_delivery_identity(self, identity, display_name="", stamp_cost=None):
        self.registered_with = {"display_name": display_name, "stamp_cost": stamp_cost}
        dest = _Dest(identity.hexhash)
        dest.stamp_cost = stamp_cost
        return dest

    def register_delivery_callback(self, cb):
        pass

    def announce(self, dest_hash):
        pass

    def exit_handler(self):
        pass

    def set_inbound_stamp_cost(self, dest_hash, cost):
        self.calls.append(("cost", cost))
        self.inbound[dest_hash] = cost
        return True

    def enforce_stamps(self):
        self.calls.append(("enforce",))
        self.enforcing = True

    def ignore_stamps(self):
        self.calls.append(("ignore",))
        self.enforcing = False


class _Store:
    def __init__(self, entries):
        self._e = {e["id"]: e for e in entries}

    def load_rns_identity(self, i):
        return types.SimpleNamespace(hexhash=i) if i in self._e else None

    def list_identities(self):
        return list(self._e.values())

    def list_for_user(self, sub):
        return [e for e in self._e.values() if e.get("user_sub") == sub]

    def get_active_for_user(self, sub):
        owned = self.list_for_user(sub)
        return owned[0] if owned else None

    get_for_user = get_active_for_user


E1 = {"id": "aa" * 16, "user_sub": "u1", "name": "One", "created": 1.0}
E2 = {"id": "bb" * 16, "user_sub": "u2", "name": "Two", "created": 2.0}


@pytest.fixture(autouse=True)
def fake_router(monkeypatch):
    _Router.instances = []
    monkeypatch.setattr("LXMF.LXMRouter", _Router)


@pytest.fixture
def svc(tmp_path):
    s = MessagingService(storage_path=str(tmp_path))
    s._identity_store = _Store([E1, E2])
    return s


class TestNewRouters:
    def test_off_by_default_nothing_is_requested(self, svc):
        svc.activate_identity(E1)
        r = _Router.instances[0]
        assert r.registered_with["stamp_cost"] is None
        assert r.enforcing is False

    def test_a_cost_is_set_when_the_identity_is_registered(self, svc):
        svc.set_stamp_policy(12, False)
        svc.activate_identity(E1)
        assert _Router.instances[0].registered_with["stamp_cost"] == 12

    def test_enforcement_is_switched_on_for_a_new_router(self, svc):
        svc.set_stamp_policy(8, True)
        svc.activate_identity(E1)
        assert _Router.instances[0].enforcing is True

    def test_a_router_that_cannot_enforce_does_not_break_startup(self, svc, monkeypatch):
        class Old(_Router):
            enforce_stamps = None
            ignore_stamps = None
        monkeypatch.setattr("LXMF.LXMRouter", Old)
        svc.set_stamp_policy(8, True)
        assert svc.activate_identity(E1) is not None


class TestLiveChange:
    def test_applies_to_every_live_router(self, svc):
        svc.activate_identity(E1)
        svc.activate_identity(E2)
        svc.set_stamp_policy(10, True)
        for r, e in zip(_Router.instances, (E1, E2)):
            assert r.inbound[bytes.fromhex(e["id"])] == 10 and r.enforcing is True

    def test_turning_it_off_clears_the_cost_and_enforcement(self, svc):
        svc.set_stamp_policy(10, True)
        svc.activate_identity(E1)
        svc.set_stamp_policy(0, True)
        r = _Router.instances[0]
        assert r.inbound[bytes.fromhex(E1["id"])] is None and r.enforcing is False

    def test_enforcing_with_no_cost_is_a_no_op(self, svc):
        # There is nothing to enforce when no stamp is required.
        svc.activate_identity(E1)
        svc.set_stamp_policy(0, True)
        assert _Router.instances[0].enforcing is False


class TestValues:
    @pytest.mark.parametrize("given,expected", [
        (0, 0), (8, 8), (MAX_STAMP_COST, MAX_STAMP_COST), (MAX_STAMP_COST + 50, MAX_STAMP_COST),
        (-3, 0), ("12", 12), ("junk", 0), (None, 0), (3.9, 3),
    ])
    def test_cost_is_clamped_and_forgiving(self, svc, given, expected):
        svc.set_stamp_policy(given, False)
        assert svc.stamp_policy() == (expected, False)

    def test_the_cap_keeps_the_work_a_phone_can_do(self):
        assert 8 <= MAX_STAMP_COST <= 28


class TestSettings:
    def make(self, tmp_path):
        return UISettings(str(tmp_path))

    def test_defaults_are_off(self, tmp_path):
        d = self.make(tmp_path).get_all()
        assert d["lxmf_stamp_cost"] == 0 and d["lxmf_enforce_stamps"] is False

    def test_values_are_stored_clamped_and_persist(self, tmp_path):
        ui = self.make(tmp_path)
        ui.update({"lxmf_stamp_cost": "9", "lxmf_enforce_stamps": True})
        again = self.make(tmp_path).get_all()
        assert again["lxmf_stamp_cost"] == 9 and again["lxmf_enforce_stamps"] is True
        ui.update({"lxmf_stamp_cost": 9999})
        assert ui.get_all()["lxmf_stamp_cost"] == MAX_STAMP_COST

    def test_junk_leaves_the_last_good_value(self, tmp_path):
        ui = self.make(tmp_path)
        ui.update({"lxmf_stamp_cost": 8})
        ui.update({"lxmf_stamp_cost": "lots"})
        assert ui.get_all()["lxmf_stamp_cost"] == 8

    def test_the_access_preset_is_unaffected(self, tmp_path):
        ui = self.make(tmp_path)
        before = ui.get_all()["access_mode"]
        ui.update({"lxmf_stamp_cost": 8, "lxmf_enforce_stamps": True})
        assert ui.get_all()["access_mode"] == before

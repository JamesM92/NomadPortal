"""Tests for MessagingService.refresh_router_display_name().

Motivation: identity_store.rename() only ever persisted the new name to
disk — nothing updated the *live* LXMRouter's destination. Real bug,
found on the NomadPortal-Android sister project via a live on-device
report ("the announce is sending out with the hash and not the assigned
name"): LXMRouter.announce() reads delivery_destination.display_name
directly (set once at register_delivery_identity() time), never
Destination.default_app_data. Without a live refresh, a rename took
effect in this app's own UI immediately but any announce sent
afterward — including the bootstrap/reconnect announce added earlier
this session — kept broadcasting the old name to the mesh until the next
full process restart.

Same "stub LXMF.LXMRouter entirely" approach as test_bootstrap_announce.py
(matches this project's established testing philosophy of never touching
real RNS/LXMF machinery in a unit test).
"""

import types

import pytest

from nomadnet_web.messaging import MessagingService


class _FakeDestination:
    def __init__(self, hexhash):
        self.hexhash = hexhash
        self.hash = bytes.fromhex(hexhash)
        self.display_name = ""


class _FakeRouter:
    def __init__(self, storagepath=None):
        self.announced_hashes = []

    def register_delivery_identity(self, identity, display_name=""):
        dest = _FakeDestination(getattr(identity, "hexhash", "ab" * 16))
        dest.display_name = display_name
        return dest

    def register_delivery_callback(self, cb):
        pass

    def announce(self, dest_hash):
        self.announced_hashes.append(dest_hash)


class _StubIdentityStore:
    def __init__(self, identity):
        self._identity = identity

    def load_rns_identity(self, identity_id):
        return self._identity


@pytest.fixture(autouse=True)
def _fake_lxmf_router(monkeypatch):
    monkeypatch.setattr("LXMF.LXMRouter", _FakeRouter)


@pytest.fixture
def service(tmp_path):
    svc = MessagingService(storage_path=str(tmp_path))
    svc._identity_store = _StubIdentityStore(types.SimpleNamespace(hexhash="ab" * 16))
    return svc


ENTRY = {"id": "ab" * 16, "user_sub": "u1", "name": "Old Name"}


def test_refresh_updates_the_live_destinations_display_name(service):
    data = service._init_user_router(ENTRY)
    assert data["dest"].display_name == "Old Name"

    service.refresh_router_display_name("u1", "New Name")

    assert data["dest"].display_name == "New Name"


def test_refresh_is_a_noop_for_a_user_with_no_live_router(service):
    # Renaming an identity that isn't currently active (no login yet,
    # or process just restarted) — nothing live to refresh, must not
    # raise.
    service.refresh_router_display_name("someone-never-logged-in", "New Name")


def test_refresh_failure_is_swallowed_not_raised(service, monkeypatch):
    data = service._init_user_router(ENTRY)

    class _Unsettable:
        @property
        def display_name(self):
            return "whatever"

        @display_name.setter
        def display_name(self, value):
            raise RuntimeError("destination is frozen")

    # Swap in an object whose display_name assignment always raises —
    # refresh_router_display_name must log and move on, not propagate.
    # `data` is the same dict object _init_user_router stored internally
    # (not a copy), so mutating it here is visible to the service too.
    data["dest"] = _Unsettable()
    service.refresh_router_display_name("u1", "New Name")  # must not raise

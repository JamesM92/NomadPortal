"""Tests for MessagingService's multi-identity router lifecycle:
deactivate_user()/activate_user() (the real teardown/rebuild behind
switching which of an account's identities is "live"), setup_delivery()
only bringing up each account's *active* identity at boot, and
get_identity() (the identity behind an account's current router — used
by the rnsh terminal feature to authenticate).

Same "stub LXMF.LXMRouter entirely" approach as
test_refresh_router_display_name.py / test_bootstrap_announce.py —
never touches real RNS/LXMF machinery in a unit test.
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
    instances = []

    def __init__(self, storagepath=None):
        self.storagepath = storagepath
        self.announced_hashes = []
        self.exit_handler_called = False
        _FakeRouter.instances.append(self)

    def register_delivery_identity(self, identity, display_name=""):
        dest = _FakeDestination(getattr(identity, "hexhash", "ab" * 16))
        dest.display_name = display_name
        return dest

    def register_delivery_callback(self, cb):
        pass

    def announce(self, dest_hash):
        self.announced_hashes.append(dest_hash)

    def exit_handler(self):
        self.exit_handler_called = True


class _StubIdentityStore:
    """Fakes just enough of IdentityStore's real API for these tests:
    load_rns_identity() and list_active_identities()."""

    def __init__(self, entries):
        self._entries = {e["id"]: e for e in entries}

    def load_rns_identity(self, identity_id):
        entry = self._entries.get(identity_id)
        if entry is None:
            return None
        return types.SimpleNamespace(hexhash=identity_id)

    def list_active_identities(self):
        return list(self._entries.values())

    def get_for_user(self, user_sub):
        # Not exercised by any of these tests' real assertions -- only
        # here so _get_user_router()'s lazy-init fallback path doesn't
        # AttributeError when a test calls get_identity()/lxmf_address()
        # for an account with no cached router yet.
        return None


@pytest.fixture(autouse=True)
def _fake_lxmf_router(monkeypatch):
    _FakeRouter.instances = []
    monkeypatch.setattr("LXMF.LXMRouter", _FakeRouter)


ENTRY_1 = {"id": "aa" * 16, "user_sub": "u1", "name": "Identity One"}
ENTRY_2 = {"id": "bb" * 16, "user_sub": "u1", "name": "Identity Two"}
ENTRY_OTHER_USER = {"id": "cc" * 16, "user_sub": "u2", "name": "Other User"}


@pytest.fixture
def service(tmp_path):
    svc = MessagingService(storage_path=str(tmp_path))
    svc._identity_store = _StubIdentityStore([ENTRY_1, ENTRY_2, ENTRY_OTHER_USER])
    return svc


def test_deactivate_user_calls_exit_handler_and_drops_the_cached_router(service):
    data = service._init_user_router(ENTRY_1)
    router = data["router"]
    assert router.exit_handler_called is False

    service.deactivate_user("u1")

    assert router.exit_handler_called is True
    with service._lock:
        assert "u1" not in service._user_routers


def test_deactivate_user_is_a_noop_for_an_account_with_no_live_router(service):
    service.deactivate_user("nobody-logged-in")  # must not raise


def test_activate_user_builds_a_fresh_router_for_the_new_identity(service):
    service._init_user_router(ENTRY_1)
    service.deactivate_user("u1")

    data = service.activate_user(ENTRY_2)

    assert data is not None
    assert data["dest"].hexhash == ENTRY_2["id"]
    assert service.lxmf_address("u1") == ENTRY_2["id"]


def test_switching_identity_never_reuses_the_deactivated_routers_instance(service):
    first_data = service._init_user_router(ENTRY_1)
    service.deactivate_user("u1")
    second_data = service.activate_user(ENTRY_2)

    assert first_data["router"] is not second_data["router"]
    # The old router really was torn down, not silently kept alive
    # alongside the new one.
    assert first_data["router"].exit_handler_called is True


def test_get_identity_returns_the_identity_behind_the_live_router(service):
    service._init_user_router(ENTRY_1)

    identity = service.get_identity("u1")

    assert identity is not None
    assert identity.hexhash == ENTRY_1["id"]


def test_get_identity_is_none_for_an_account_with_no_live_router(service):
    assert service.get_identity("nobody-logged-in") is None


def test_setup_delivery_only_initialises_each_accounts_active_identity(service):
    # _StubIdentityStore.list_active_identities() returns one entry per
    # account (ENTRY_1 for u1, ENTRY_OTHER_USER for u2) -- ENTRY_2
    # deliberately excluded, standing in for "u1's second, inactive
    # identity", which setup_delivery() must NOT bring up a router for.
    identity_store = _StubIdentityStore([ENTRY_1, ENTRY_OTHER_USER])
    service._identity_store = None  # setup_delivery() sets this itself
    service.setup_delivery(identity_store)

    assert len(_FakeRouter.instances) == 2
    assert service.lxmf_address("u1") == ENTRY_1["id"]
    assert service.lxmf_address("u2") == ENTRY_OTHER_USER["id"]

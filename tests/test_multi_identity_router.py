"""Tests for MessagingService's multi-identity router lifecycle.

Real behavior under test: an account can own more than one identity,
and EVERY one of them gets its own live LXMRouter (so messages can be
received on any of them, not just the currently active one) — but only
the account's currently *active* identity actually announces (see
_init_identity_router()'s own doc comment for why). Switching which
identity is active (activate_identity()) never tears down another
identity's router; only deleting an identity outright
(deactivate_identity()) does that.

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
    load_rns_identity(), list_identities(), list_for_user(), and
    get_active_for_user() (a plain dict lookup here — real active-
    tracking logic is IdentityStore's own, already covered by
    test_identity_multi.py)."""

    def __init__(self, entries, active_by_user=None):
        self._entries = {e["id"]: e for e in entries}
        self._active_by_user = dict(active_by_user or {})

    def load_rns_identity(self, identity_id):
        entry = self._entries.get(identity_id)
        if entry is None:
            return None
        return types.SimpleNamespace(hexhash=identity_id)

    def list_identities(self):
        return list(self._entries.values())

    def list_for_user(self, user_sub):
        return [e for e in self._entries.values() if e.get("user_sub") == user_sub]

    def get_active_for_user(self, user_sub):
        active_id = self._active_by_user.get(user_sub)
        if active_id is not None:
            return self._entries.get(active_id)
        owned = self.list_for_user(user_sub)
        return owned[0] if owned else None

    def get_for_user(self, user_sub):
        return self.get_active_for_user(user_sub)


@pytest.fixture(autouse=True)
def _fake_lxmf_router(monkeypatch):
    _FakeRouter.instances = []
    monkeypatch.setattr("LXMF.LXMRouter", _FakeRouter)


ENTRY_1 = {"id": "aa" * 16, "user_sub": "u1", "name": "Identity One", "created": 1.0}
ENTRY_2 = {"id": "bb" * 16, "user_sub": "u1", "name": "Identity Two", "created": 2.0}
ENTRY_OTHER_USER = {"id": "cc" * 16, "user_sub": "u2", "name": "Other User", "created": 1.0}


@pytest.fixture
def service(tmp_path):
    svc = MessagingService(storage_path=str(tmp_path))
    svc._identity_store = _StubIdentityStore(
        [ENTRY_1, ENTRY_2, ENTRY_OTHER_USER],
        active_by_user={"u1": ENTRY_1["id"], "u2": ENTRY_OTHER_USER["id"]},
    )
    return svc


def _router_for(service, identity_id):
    with service._lock:
        return service._identity_routers.get(identity_id)


class TestActivateIdentity:
    def test_builds_a_router_and_announces_it(self, service):
        data = service.activate_identity(ENTRY_1)

        assert data is not None
        assert data["dest"].hexhash == ENTRY_1["id"]
        assert data["router"].announced_hashes  # a real bootstrap announce fired

    def test_reuses_an_already_live_router(self, service):
        first = service.activate_identity(ENTRY_1)
        second = service.activate_identity(ENTRY_1)

        assert first["router"] is second["router"]
        assert len(_FakeRouter.instances) == 1

    def test_activating_a_second_identity_leaves_the_first_ones_router_running(self, service):
        first = service.activate_identity(ENTRY_1)
        second = service.activate_identity(ENTRY_2)

        assert first["router"] is not second["router"]
        # The first one is NOT torn down just because a different
        # identity became active -- it must keep receiving.
        assert first["router"].exit_handler_called is False
        assert _router_for(service, ENTRY_1["id"]) is not None
        assert _router_for(service, ENTRY_2["id"]) is not None


class TestDeactivateIdentity:
    def test_calls_exit_handler_and_drops_the_cached_router(self, service):
        data = service.activate_identity(ENTRY_1)
        router = data["router"]
        assert router.exit_handler_called is False

        service.deactivate_identity(ENTRY_1["id"])

        assert router.exit_handler_called is True
        assert _router_for(service, ENTRY_1["id"]) is None

    def test_is_a_noop_for_an_identity_with_no_live_router(self, service):
        service.deactivate_identity("no-such-identity")  # must not raise

    def test_deactivating_one_identity_does_not_touch_another(self, service):
        service.activate_identity(ENTRY_1)
        second = service.activate_identity(ENTRY_2)

        service.deactivate_identity(ENTRY_1["id"])

        assert second["router"].exit_handler_called is False
        assert _router_for(service, ENTRY_2["id"]) is not None


class TestResetIdentityRouter:
    def test_drops_the_cached_entry_without_calling_exit_handler(self, service):
        data = service.activate_identity(ENTRY_1)
        router = data["router"]

        service.reset_identity_router(ENTRY_1["id"])

        assert router.exit_handler_called is False  # the old keypair is just gone, no clean handoff needed
        assert _router_for(service, ENTRY_1["id"]) is None

    def test_next_use_rebuilds_a_fresh_router(self, service):
        service.activate_identity(ENTRY_1)
        service.reset_identity_router(ENTRY_1["id"])

        rebuilt = service.activate_identity(ENTRY_1)

        assert rebuilt is not None
        assert len(_FakeRouter.instances) == 2


class TestGetIdentity:
    def test_returns_the_identity_behind_the_active_router(self, service):
        identity = service.get_identity("u1")

        assert identity is not None
        assert identity.hexhash == ENTRY_1["id"]  # ENTRY_1 is u1's active identity per the fixture

    def test_is_none_for_an_account_with_no_identities(self, service):
        assert service.get_identity("nobody-logged-in") is None

    def test_lazily_initialises_the_router_if_not_already_live(self, service):
        assert _router_for(service, ENTRY_1["id"]) is None

        identity = service.get_identity("u1")

        assert identity is not None
        assert _router_for(service, ENTRY_1["id"]) is not None


class TestSetupDelivery:
    def test_brings_up_every_identity_every_account_owns(self, service):
        identity_store = _StubIdentityStore(
            [ENTRY_1, ENTRY_2, ENTRY_OTHER_USER],
            active_by_user={"u1": ENTRY_1["id"], "u2": ENTRY_OTHER_USER["id"]},
        )
        service._identity_store = None  # setup_delivery() sets this itself

        service.setup_delivery(identity_store)

        # All three identities across both accounts got a router --
        # including ENTRY_2, u1's *inactive* second identity.
        assert len(_FakeRouter.instances) == 3
        for entry in (ENTRY_1, ENTRY_2, ENTRY_OTHER_USER):
            assert _router_for(service, entry["id"]) is not None

    def test_only_announces_each_accounts_active_identity(self, service):
        identity_store = _StubIdentityStore(
            [ENTRY_1, ENTRY_2, ENTRY_OTHER_USER],
            active_by_user={"u1": ENTRY_1["id"], "u2": ENTRY_OTHER_USER["id"]},
        )
        service._identity_store = None
        service.setup_delivery(identity_store)

        assert _router_for(service, ENTRY_1["id"])["router"].announced_hashes  # active -- announced
        assert _router_for(service, ENTRY_2["id"])["router"].announced_hashes == []  # inactive -- silent
        assert _router_for(service, ENTRY_OTHER_USER["id"])["router"].announced_hashes  # active -- announced


class TestSetupUser:
    def test_brings_up_every_identity_the_account_owns(self, service):
        service.setup_user("u1")

        assert _router_for(service, ENTRY_1["id"]) is not None
        assert _router_for(service, ENTRY_2["id"]) is not None
        # A different account's identity is untouched.
        assert _router_for(service, ENTRY_OTHER_USER["id"]) is None

    def test_only_announces_the_active_identity(self, service):
        service.setup_user("u1")

        assert _router_for(service, ENTRY_1["id"])["router"].announced_hashes  # active
        assert _router_for(service, ENTRY_2["id"])["router"].announced_hashes == []  # inactive

    def test_is_a_noop_for_an_account_with_no_identities(self, service):
        service.setup_user("nobody-registered")  # must not raise
        assert len(_FakeRouter.instances) == 0


class TestActiveRouters:
    def test_returns_one_entry_per_live_identity_not_per_account(self, service):
        service.activate_identity(ENTRY_1)
        service.activate_identity(ENTRY_2)  # same account (u1) as ENTRY_1
        service.activate_identity(ENTRY_OTHER_USER)

        routers = service.active_routers()

        assert len(routers) == 3
        user_subs = [user_sub for user_sub, _ in routers]
        assert user_subs.count("u1") == 2  # ENTRY_1 and ENTRY_2 both belong to u1
        assert user_subs.count("u2") == 1


class TestRefreshRouterDisplayName:
    def test_updates_the_active_identitys_live_display_name(self, service):
        data = service.activate_identity(ENTRY_1)
        assert data["dest"].display_name == "Identity One"

        service.refresh_router_display_name("u1", "New Name")

        assert data["dest"].display_name == "New Name"

    def test_does_not_touch_a_different_identitys_router(self, service):
        # ENTRY_2 belongs to the same account (u1) but is NOT the active
        # identity per the fixture -- a rename refresh for "u1" must
        # only ever touch the active one (ENTRY_1), never this one.
        other = service.activate_identity(ENTRY_2)
        service.activate_identity(ENTRY_1)

        service.refresh_router_display_name("u1", "New Name")

        assert other["dest"].display_name == "Identity Two"  # untouched

    def test_is_a_noop_when_no_router_is_live_yet(self, service):
        service.refresh_router_display_name("u1", "New Name")  # must not raise

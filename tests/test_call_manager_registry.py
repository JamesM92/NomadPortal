"""Tests for CallManagerRegistry — the web-specific "one CallManager per
logged-in account" wrapper (see call_manager.py's own top doc comment
for why this doesn't exist on the NomadPortal-Android sister project,
which has no real account concept).

Stubs the CallManager class itself (not a fake RNS module) — setup_user()
does a real ``import RNS`` internally and constructs a real
``RNS.Destination``, which needs a live ``RNS.Reticulum()`` instance to
succeed (confirmed: it fails with "Transport has no attribute owner"
without one) — exactly the kind of real-network-adjacent machinery this
project's own testing philosophy avoids touching in a unit test. Same
"stub the class, not the network" approach test_rnsh_client.py's own
RnshManager tests already established for RnshSession.
"""

import types

import pytest

from nomadnet_web.call_manager import CallManagerRegistry


class _FakeCallManager:
    """Stands in for a real CallManager -- records what it was asked to
    do instead of touching RNS at all."""

    def __init__(self):
        self._identity = None
        self._destination = None
        self.last_announce_at = None
        self.start_calls = []
        self.announce_calls = 0
        self.calls_enabled = None
        self.contacts_only = None
        self.contact_checker = None

    def start(self, rns_module, identity):
        self._identity = identity
        self._destination = object()  # just needs to be non-None
        self.start_calls.append((rns_module, identity))

    def announce(self):
        self.announce_calls += 1
        self.last_announce_at = 1.0

    def set_calls_enabled(self, enabled):
        self.calls_enabled = enabled

    def set_contacts_only(self, enabled):
        self.contacts_only = enabled

    def set_contact_checker(self, fn):
        self.contact_checker = fn


class _StubCallSettings:
    def __init__(self, enabled=False, contacts_only=True):
        self._enabled = enabled
        self._contacts_only = contacts_only

    def get_calls_enabled(self):
        return self._enabled

    def get_contacts_only(self):
        return self._contacts_only


class _StubCallSettingsManager:
    def __init__(self):
        self._per_user: dict = {}

    def for_user(self, user_sub):
        return self._per_user.setdefault(user_sub, _StubCallSettings())


class _StubContactStore:
    def __init__(self, known=None):
        self._known = known or set()

    def get(self, hash_hex):
        return {"hash": hash_hex} if hash_hex in self._known else None


class _StubContactStoreManager:
    def __init__(self):
        self._per_user: dict = {}

    def for_user(self, user_sub):
        return self._per_user.setdefault(user_sub, _StubContactStore())


@pytest.fixture(autouse=True)
def _fake_call_manager_class(monkeypatch):
    monkeypatch.setattr("nomadnet_web.call_manager.CallManager", _FakeCallManager)


def _identity(hash_byte=0x11):
    return types.SimpleNamespace(hash=bytes([hash_byte]) * 16)


def test_get_returns_none_for_an_account_with_no_manager_yet():
    registry = CallManagerRegistry()
    assert registry.get("u1") is None


def test_setup_user_creates_and_starts_a_manager():
    registry = CallManagerRegistry()
    mgr = registry.setup_user("u1", _identity())

    assert mgr is not None
    assert len(mgr.start_calls) == 1
    assert mgr.announce_calls == 1
    assert registry.get("u1") is mgr


def test_setup_user_with_no_identity_is_a_safe_no_op():
    registry = CallManagerRegistry()
    assert registry.setup_user("u1", None) is None
    assert registry.get("u1") is None


def test_setup_user_is_idempotent_for_the_same_identity():
    registry = CallManagerRegistry()
    identity = _identity()
    first = registry.setup_user("u1", identity)
    second = registry.setup_user("u1", identity)

    assert first is second
    # start()/announce() only ran once -- re-using an already-live
    # engine, not tearing it down and rebuilding on every call.
    assert len(first.start_calls) == 1
    assert first.announce_calls == 1


def test_setup_user_restarts_when_the_identity_actually_changed():
    registry = CallManagerRegistry()
    first_identity = _identity(0x11)
    second_identity = _identity(0x22)

    mgr = registry.setup_user("u1", first_identity)
    registry.setup_user("u1", second_identity)

    assert len(mgr.start_calls) == 2
    assert mgr._identity is second_identity


def test_two_accounts_get_independent_managers():
    registry = CallManagerRegistry()
    mgr1 = registry.setup_user("u1", _identity(0x11))
    mgr2 = registry.setup_user("u2", _identity(0x22))

    assert mgr1 is not mgr2
    assert registry.get("u1") is mgr1
    assert registry.get("u2") is mgr2


def test_setup_user_pushes_persisted_settings_into_the_manager():
    settings_mgr = _StubCallSettingsManager()
    settings_mgr.for_user("u1")._enabled = True
    settings_mgr.for_user("u1")._contacts_only = False
    registry = CallManagerRegistry(call_settings=settings_mgr)

    mgr = registry.setup_user("u1", _identity())

    assert mgr.calls_enabled is True
    assert mgr.contacts_only is False


def test_setup_user_wires_a_working_contact_checker():
    contact_mgr = _StubContactStoreManager()
    contact_mgr.for_user("u1")._known.add("aa" * 16)
    registry = CallManagerRegistry(contact_store=contact_mgr)

    mgr = registry.setup_user("u1", _identity())

    assert mgr.contact_checker is not None
    assert mgr.contact_checker("aa" * 16) is True
    assert mgr.contact_checker("bb" * 16) is False


def test_setup_user_without_contact_store_or_settings_still_works():
    # Both dependencies are optional -- a registry constructed with
    # neither must still start a manager cleanly.
    registry = CallManagerRegistry()
    mgr = registry.setup_user("u1", _identity())
    assert mgr is not None
    assert mgr.contact_checker is None


def test_start_announce_loop_is_idempotent():
    registry = CallManagerRegistry()
    assert registry._announce_loop_started is False
    registry.start_announce_loop()
    assert registry._announce_loop_started is True
    registry.start_announce_loop()  # must not raise / start a second thread
    assert registry._announce_loop_started is True

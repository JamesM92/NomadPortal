"""Tests for MessagingService._init_user_router()'s bootstrap/re-announce.

Motivation: RNS path discovery is announce-based with no other mechanism —
a destination that has never announced is unreachable by anyone, full
stop. A brand-new identity (or one whose last announce aged out of a
peer's routing table) sat unreachable until the user discovered and
clicked the manual Announce button themselves, with a message sent to
them in the meantime just failing after PATH_WAIT with no obvious reason
why. This guards the fix: every successful router registration announces
once, whether that's a genuinely new identity or an existing one being
rebuilt on process restart (routers aren't persisted across restarts —
setup_delivery()/setup_user() rebuild them in memory each boot, so this
also doubles as the periodic re-announce that keeps an existing user's
path fresh on their peers' end).

LXMF.LXMRouter itself is stubbed out entirely (not just the background
delivery thread, unlike test_messaging.py's approach) — router
construction and registration happen synchronously in
_init_user_router(), so there's no background-thread boundary to stub
past the way _send() has one. Matches this project's existing testing
philosophy (see test_drop_job_grace.py's own docstring) of never touching
real RNS/LXMF machinery in a unit test.
"""

import types

import pytest

from nomadnet_web.messaging import MessagingService


class _FakeDestination:
    def __init__(self, hexhash):
        self.hexhash = hexhash
        self.hash = bytes.fromhex(hexhash)


class _FakeRouter:
    """Stand-in for LXMF.LXMRouter — records what's called on it, no real
    RNS/LXMF network activity."""

    instances = []

    def __init__(self, storagepath=None):
        self.storagepath = storagepath
        self.announced_hashes = []
        self.delivery_callback = None
        _FakeRouter.instances.append(self)

    def register_delivery_identity(self, identity, display_name=""):
        return _FakeDestination(getattr(identity, "hexhash", "ab" * 16))

    def register_delivery_callback(self, cb):
        self.delivery_callback = cb

    def announce(self, dest_hash):
        self.announced_hashes.append(dest_hash)


class _StubIdentityStore:
    def __init__(self, identity):
        self._identity = identity

    def load_rns_identity(self, identity_id):
        return self._identity


@pytest.fixture(autouse=True)
def _fake_lxmf_router(monkeypatch):
    _FakeRouter.instances = []
    monkeypatch.setattr("LXMF.LXMRouter", _FakeRouter)


@pytest.fixture
def service(tmp_path):
    svc = MessagingService(storage_path=str(tmp_path))
    fake_identity = types.SimpleNamespace(hexhash="ab" * 16)
    svc._identity_store = _StubIdentityStore(fake_identity)
    return svc


ENTRY = {"id": "ab" * 16, "user_sub": "u1", "name": "Test User"}


def test_init_user_router_announces_once_on_success(service):
    data = service._init_user_router(ENTRY)
    assert data is not None
    assert data["router"].announced_hashes == [data["dest"].hash]


def test_announce_failure_does_not_prevent_router_registration(service, monkeypatch):
    def _boom(self, dest_hash):
        raise RuntimeError("no route to broadcast on")

    monkeypatch.setattr(_FakeRouter, "announce", _boom)

    data = service._init_user_router(ENTRY)
    # Router registration must still succeed even though announce blew up —
    # best-effort, shouldn't take down the whole registration.
    assert data is not None
    assert data["router"] is not None


def test_second_init_for_same_user_reuses_cached_router_no_double_announce(service):
    first = service._init_user_router(ENTRY)
    second = service._init_user_router(ENTRY)

    assert first is second
    # Only the first call actually constructed (and announced) a router —
    # the cache hit on the second call must short-circuit before that.
    assert len(_FakeRouter.instances) == 1
    assert len(_FakeRouter.instances[0].announced_hashes) == 1

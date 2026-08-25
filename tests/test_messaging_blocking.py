"""Tests for MessagingService._on_delivery()'s per-sender blocking
guard — the enforcement half of contact blocking (see
test_contact_blocking.py for the ContactStore storage half).

A blocked sender's message must be dropped before anything else in
_on_delivery runs: no message_store.save_received() call, no icon
extraction, nothing that would let the sender (or a UI reading the
result) tell blocking apart from a message that simply never arrived.
"""

import types

from nomadnet_web.contact_store import ContactStoreManager
from nomadnet_web.messaging import MessagingService

USER_SUB = "u1"
BLOCKED_HASH = "dd" * 16
ALLOWED_HASH = "ee" * 16


class _StubMessageStore:
    def __init__(self):
        self.saved = []

    def save_received(self, entry):
        self.saved.append(entry)


def _fake_message(source_hex, content="hello"):
    return types.SimpleNamespace(
        source_hash=bytes.fromhex(source_hex),
        hash=bytes.fromhex("11" * 16),
        title=b"",
        content=content.encode("utf-8"),
        fields={},
    )


def _service(tmp_path):
    contact_mgr = ContactStoreManager(str(tmp_path))
    msg_store = _StubMessageStore()
    svc = MessagingService(
        storage_path=str(tmp_path / "storage"),
        message_store=msg_store,
        contact_store=contact_mgr,
    )
    return svc, contact_mgr, msg_store


def test_message_from_blocked_sender_is_never_stored(tmp_path):
    svc, contact_mgr, msg_store = _service(tmp_path)
    contact_mgr.for_user(USER_SUB).set_blocked(BLOCKED_HASH, True)

    svc._on_delivery(_fake_message(BLOCKED_HASH), user_sub=USER_SUB)

    assert msg_store.saved == []


def test_message_from_an_unblocked_sender_is_unaffected(tmp_path):
    svc, contact_mgr, msg_store = _service(tmp_path)
    contact_mgr.for_user(USER_SUB).set_blocked(BLOCKED_HASH, True)

    svc._on_delivery(_fake_message(ALLOWED_HASH), user_sub=USER_SUB)

    assert len(msg_store.saved) == 1
    assert msg_store.saved[0]["source"] == ALLOWED_HASH


def test_unblocking_lets_future_messages_through_again(tmp_path):
    svc, contact_mgr, msg_store = _service(tmp_path)
    contact_mgr.for_user(USER_SUB).set_blocked(BLOCKED_HASH, True)
    svc._on_delivery(_fake_message(BLOCKED_HASH), user_sub=USER_SUB)
    assert msg_store.saved == []

    contact_mgr.for_user(USER_SUB).set_blocked(BLOCKED_HASH, False)
    svc._on_delivery(_fake_message(BLOCKED_HASH), user_sub=USER_SUB)

    assert len(msg_store.saved) == 1


def test_blocking_is_per_user_not_global(tmp_path):
    # Same sender hash, two different accounts on this instance -- one
    # blocks them, the other never hears about it. Contact records are
    # already scoped per-user (ContactStoreManager.for_user); this
    # just confirms the blocking check goes through that same scoping
    # rather than some shared/global lookup.
    svc, contact_mgr, msg_store = _service(tmp_path)
    contact_mgr.for_user("blocker").set_blocked(BLOCKED_HASH, True)

    svc._on_delivery(_fake_message(BLOCKED_HASH), user_sub="someone_else")

    assert len(msg_store.saved) == 1


def test_no_contact_manager_wired_does_not_crash(tmp_path):
    # MessagingService's own contact_store param is optional (tests
    # and older callers may construct without one) -- the blocking
    # guard must degrade to "allow" rather than raise.
    msg_store = _StubMessageStore()
    svc = MessagingService(storage_path=str(tmp_path), message_store=msg_store)

    svc._on_delivery(_fake_message(BLOCKED_HASH), user_sub=USER_SUB)

    assert len(msg_store.saved) == 1

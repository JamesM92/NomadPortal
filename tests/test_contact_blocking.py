"""Tests for ContactStore.set_blocked()/is_blocked() — the storage half
of per-sender message blocking (Columba-parity feature, ported from the
NomadPortal-Android sister project's BlockedContactsScreen/
MessagingRepository.setBlocked).

Unlike set_favorite(), set_blocked() must work even for a hash with no
existing contact record — you can legitimately want to block a sender
you've never explicitly added as a contact, whereas favoriting an
unknown sender doesn't make sense the same way. That asymmetry is the
main thing worth guarding here.
"""

from nomadnet_web.contact_store import ContactStore

HASH = "cc" * 16


def test_blocking_an_unknown_hash_creates_a_minimal_record(tmp_path):
    store = ContactStore(str(tmp_path))
    assert store.get(HASH) is None

    entry = store.set_blocked(HASH, True)

    assert entry["hash"] == HASH
    assert entry["blocked"] is True
    assert store.is_blocked(HASH) is True
    assert store.get(HASH)["blocked"] is True


def test_is_blocked_false_for_a_hash_with_no_record_at_all(tmp_path):
    store = ContactStore(str(tmp_path))
    assert store.is_blocked(HASH) is False


def test_unblocking_clears_the_flag_without_deleting_the_contact(tmp_path):
    store = ContactStore(str(tmp_path))
    store.upsert(HASH, name="Real Name")
    store.set_blocked(HASH, True)
    assert store.is_blocked(HASH) is True

    store.set_blocked(HASH, False)

    assert store.is_blocked(HASH) is False
    entry = store.get(HASH)
    assert entry is not None
    assert entry["name"] == "Real Name"  # untouched by the block/unblock cycle


def test_blocking_an_existing_contact_preserves_its_other_fields(tmp_path):
    store = ContactStore(str(tmp_path))
    store.upsert(HASH, name="Alice", note="met at a meetup")
    store.set_favorite(HASH, True)

    store.set_blocked(HASH, True)

    entry = store.get(HASH)
    assert entry["name"] == "Alice"
    assert entry["note"] == "met at a meetup"
    assert entry["favorited"] is True  # blocking doesn't implicitly unfavorite
    assert entry["blocked"] is True


def test_blocked_state_persists_across_a_reload(tmp_path):
    store = ContactStore(str(tmp_path))
    store.set_blocked(HASH, True)

    reloaded = ContactStore(str(tmp_path))
    assert reloaded.is_blocked(HASH) is True

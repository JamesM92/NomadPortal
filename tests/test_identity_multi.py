"""Tests for IdentityStore's multi-identity support: one web account
(user_sub) owning more than one identity, with exactly one active at a
time. See identity_store.py's own module docstring for the full model
and how it differs from the NomadPortal-Android sister project's own
single-user version of this module.
"""

from nomadnet_web.identity_store import IdentityStore

USER_A = "account-a"
USER_B = "account-b"


def test_first_identity_for_an_account_becomes_active_automatically(tmp_path):
    store = IdentityStore(str(tmp_path))
    entry = store.create_for_user(USER_A, "First")

    active = store.get_active_for_user(USER_A)
    assert active is not None
    assert active["id"] == entry["id"]


def test_second_identity_does_not_become_active_automatically(tmp_path):
    store = IdentityStore(str(tmp_path))
    first = store.create_for_user(USER_A, "First")
    store.create_for_user(USER_A, "Second")

    active = store.get_active_for_user(USER_A)
    assert active["id"] == first["id"]


def test_list_for_user_only_returns_that_accounts_own_identities(tmp_path):
    store = IdentityStore(str(tmp_path))
    store.create_for_user(USER_A, "A1")
    store.create_for_user(USER_A, "A2")
    store.create_for_user(USER_B, "B1")

    a_ids = {e["name"] for e in store.list_for_user(USER_A)}
    b_ids = {e["name"] for e in store.list_for_user(USER_B)}
    assert a_ids == {"A1", "A2"}
    assert b_ids == {"B1"}


def test_set_active_for_user_switches_which_identity_is_active(tmp_path):
    store = IdentityStore(str(tmp_path))
    store.create_for_user(USER_A, "First")
    second = store.create_for_user(USER_A, "Second")

    assert store.set_active_for_user(USER_A, second["id"]) is True
    assert store.get_active_for_user(USER_A)["id"] == second["id"]


def test_set_active_for_user_refuses_another_accounts_identity(tmp_path):
    store = IdentityStore(str(tmp_path))
    store.create_for_user(USER_A, "A1")
    b_entry = store.create_for_user(USER_B, "B1")

    assert store.set_active_for_user(USER_A, b_entry["id"]) is False
    # USER_A's own active identity is unaffected.
    assert store.get_active_for_user(USER_A)["name"] == "A1"


def test_delete_for_user_refuses_the_last_identity(tmp_path):
    store = IdentityStore(str(tmp_path))
    entry = store.create_for_user(USER_A, "Only")

    ok, message = store.delete_for_user(USER_A, entry["id"])

    assert ok is False
    assert "only" in message.lower()
    assert store.get(entry["id"]) is not None  # not actually deleted


def test_delete_for_user_refuses_another_accounts_identity(tmp_path):
    store = IdentityStore(str(tmp_path))
    store.create_for_user(USER_A, "A1")
    b_entry = store.create_for_user(USER_B, "B1")

    ok, _ = store.delete_for_user(USER_A, b_entry["id"])

    assert ok is False
    assert store.get(b_entry["id"]) is not None


def test_deleting_the_active_identity_reassigns_active_to_another(tmp_path):
    store = IdentityStore(str(tmp_path))
    first = store.create_for_user(USER_A, "First")
    second = store.create_for_user(USER_A, "Second")
    store.set_active_for_user(USER_A, first["id"])

    ok, _ = store.delete_for_user(USER_A, first["id"])

    assert ok is True
    assert store.get(first["id"]) is None
    assert store.get_active_for_user(USER_A)["id"] == second["id"]


def test_deleting_an_inactive_identity_leaves_active_untouched(tmp_path):
    store = IdentityStore(str(tmp_path))
    first = store.create_for_user(USER_A, "First")
    second = store.create_for_user(USER_A, "Second")

    ok, _ = store.delete_for_user(USER_A, second["id"])

    assert ok is True
    assert store.get_active_for_user(USER_A)["id"] == first["id"]


def test_get_for_user_is_a_back_compat_alias_for_the_active_identity(tmp_path):
    store = IdentityStore(str(tmp_path))
    first = store.create_for_user(USER_A, "First")
    second = store.create_for_user(USER_A, "Second")
    store.set_active_for_user(USER_A, second["id"])

    assert store.get_for_user(USER_A)["id"] == second["id"] != first["id"]


def test_ensure_for_user_creates_on_first_call_and_reuses_after(tmp_path):
    store = IdentityStore(str(tmp_path))
    assert store.list_for_user(USER_A) == []

    created = store.ensure_for_user(USER_A)
    again = store.ensure_for_user(USER_A)

    assert created["id"] == again["id"]
    assert len(store.list_for_user(USER_A)) == 1


def test_get_active_for_user_returns_none_for_an_unknown_account(tmp_path):
    store = IdentityStore(str(tmp_path))
    assert store.get_active_for_user("nobody") is None


def test_active_identity_persists_across_a_reload(tmp_path):
    store = IdentityStore(str(tmp_path))
    store.create_for_user(USER_A, "First")
    second = store.create_for_user(USER_A, "Second")
    store.set_active_for_user(USER_A, second["id"])

    reloaded = IdentityStore(str(tmp_path))
    assert reloaded.get_active_for_user(USER_A)["id"] == second["id"]


def test_list_active_identities_returns_one_entry_per_account(tmp_path):
    store = IdentityStore(str(tmp_path))
    store.create_for_user(USER_A, "A1")
    store.create_for_user(USER_A, "A2")
    b_active = store.create_for_user(USER_B, "B1")

    active = store.list_active_identities()

    by_name = {e["name"]: e for e in active}
    assert set(by_name) == {"A1", "B1"}  # A's active is A1 (first-created); B's is B1
    assert by_name["B1"]["id"] == b_active["id"]


def test_legacy_flat_store_file_loads_and_self_heals_active(tmp_path):
    """A store.yml written before multi-identity existed has no
    wrapper at all — just {hexhash: entry, ...} at the top level (see
    IdentityStore._load()'s own doc comment). Loading it must not
    crash, and the first get_active_for_user() call for its one
    existing account should self-heal an active identity."""
    import yaml

    identities_dir = tmp_path / "identities"
    identities_dir.mkdir()
    legacy_entry = {
        "id": "a" * 32,
        "name": "Legacy",
        "key_file": str(identities_dir / f"{'a' * 32}.id"),
        "nodes": [],
        "created": 1000.0,
        "user_sub": USER_A,
    }
    with open(identities_dir / "store.yml", "w", encoding="utf-8") as fh:
        yaml.dump({legacy_entry["id"]: legacy_entry}, fh)

    store = IdentityStore(str(tmp_path))

    active = store.get_active_for_user(USER_A)
    assert active is not None
    assert active["id"] == legacy_entry["id"]


def test_import_identity_rejects_a_keypair_owned_by_another_account(tmp_path):
    store = IdentityStore(str(tmp_path))
    entry = store.create_for_user(USER_A, "Mine")
    key_bytes = store.export_key_bytes(entry["id"])

    import pytest
    with pytest.raises(ValueError):
        store.import_identity(key_bytes, name="Stolen", user_sub=USER_B)


def test_import_identity_owned_by_the_same_account_is_a_no_op(tmp_path):
    store = IdentityStore(str(tmp_path))
    entry = store.create_for_user(USER_A, "Mine")
    key_bytes = store.export_key_bytes(entry["id"])

    reimported = store.import_identity(key_bytes, name="Renamed attempt", user_sub=USER_A)

    assert reimported["id"] == entry["id"]
    assert reimported["name"] == "Mine"  # unchanged, not overwritten by the re-import


def test_export_then_import_round_trips_into_a_second_account(tmp_path):
    store = IdentityStore(str(tmp_path))
    entry = store.create_for_user(USER_A, "Exported")
    key_bytes = store.export_key_bytes(entry["id"])
    assert key_bytes

    other_store = IdentityStore(str(tmp_path / "other"))
    imported = other_store.import_identity(key_bytes, name="Imported", user_sub=USER_B)

    assert imported["id"] == entry["id"]  # same keypair -> same hexhash
    assert other_store.get_active_for_user(USER_B)["id"] == imported["id"]


def test_get_dest_hash_hex_returns_a_stable_32_char_hex_string(tmp_path):
    store = IdentityStore(str(tmp_path))
    entry = store.create_for_user(USER_A, "Whatever")

    addr = store.get_dest_hash_hex(entry["id"])

    assert addr is not None
    assert len(addr) == 32
    int(addr, 16)  # doesn't raise -- valid hex
    # Genuinely different from the identity's own raw hash (the real bug
    # this helper's fix closes -- see _dest_hash_hex's own doc comment).
    assert addr != entry["id"]


def test_reset_keeps_the_new_identity_active_if_the_old_one_was(tmp_path):
    store = IdentityStore(str(tmp_path))
    entry = store.create_for_user(USER_A, "Original")

    fresh = store.reset(entry["id"])

    assert fresh["id"] != entry["id"]
    assert store.get_active_for_user(USER_A)["id"] == fresh["id"]

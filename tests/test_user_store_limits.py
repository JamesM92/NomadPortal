"""Limits on the names of local accounts (found by feeding the admin Users
page worst-case data: a username had no length limit at all)."""

import pytest

from nomadnet_web.user_store import MAX_USERNAME, UserStore


@pytest.fixture
def store(tmp_path):
    return UserStore(str(tmp_path / "users.yml"))


def test_a_username_at_the_limit_is_accepted(store):
    rec, err = store.create_local_user("n" * MAX_USERNAME, "pw12345678")
    assert err == "" and rec is not None


def test_a_username_over_the_limit_is_refused_with_the_reason(store):
    rec, err = store.create_local_user("n" * (MAX_USERNAME + 1), "pw12345678")
    assert rec is None and str(MAX_USERNAME) in err and "username" in err.lower()


def test_surrounding_spaces_do_not_count_toward_the_limit(store):
    rec, err = store.create_local_user(" " + "n" * MAX_USERNAME + " ", "pw12345678")
    assert err == "" and rec["name"] == "n" * MAX_USERNAME


@pytest.mark.parametrize("name", ["", "   ", "tab\tname", "new\nline", "nul\x00byte"])
def test_blank_and_control_character_usernames_are_refused(store, name):
    rec, err = store.create_local_user(name, "pw12345678")
    assert rec is None and err


def test_ordinary_names_with_markup_characters_are_still_allowed(store):
    # They are escaped wherever they are shown, so they are not a problem.
    rec, err = store.create_local_user("o'brien<b>", "pw12345678")
    assert err == "" and rec["name"] == "o'brien<b>"

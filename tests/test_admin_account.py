"""The stored admin account: first-run setup behind a one-time code, hashed
storage, verifying a sign-in, resetting the password from the console, and
what a reset does to sessions already signed in."""

import json
import os
import stat

import pytest

from nomadnet_web.admin_account import AdminAccount, MIN_PASSWORD

GOOD = "correct horse battery"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def acct(tmp_path, clock):
    return AdminAccount(str(tmp_path), clock=clock)


def code_of(acct):
    return open(acct.setup_code_path).read().strip()


class TestSetupCode:
    def test_a_fresh_install_needs_setup_and_writes_a_code_file(self, acct):
        assert acct.configured() is False
        assert acct.ensure_setup_code() is True
        code = code_of(acct)
        assert len(code.replace("-", "")) == 16 and code == code.upper()

    def test_the_code_file_is_private(self, acct):
        acct.ensure_setup_code()
        mode = stat.S_IMODE(os.stat(acct.setup_code_path).st_mode)
        assert mode & 0o077 == 0

    def test_asking_again_keeps_the_same_code(self, acct):
        acct.ensure_setup_code()
        first = code_of(acct)
        acct.ensure_setup_code()
        assert code_of(acct) == first

    def test_a_restart_gives_a_new_code(self, tmp_path, clock):
        a = AdminAccount(str(tmp_path), clock=clock)
        a.ensure_setup_code()
        first = code_of(a)
        b = AdminAccount(str(tmp_path), clock=clock)
        b.ensure_setup_code()
        assert code_of(b) != first

    def test_no_code_is_kept_once_an_account_exists(self, acct):
        acct.ensure_setup_code()
        assert acct.complete_setup(code_of(acct), "boss", GOOD) == ""
        assert not os.path.exists(acct.setup_code_path)
        assert acct.ensure_setup_code() is False


class TestCompleteSetup:
    def test_the_right_code_creates_the_account(self, acct):
        acct.ensure_setup_code()
        assert acct.complete_setup(code_of(acct), " boss ", GOOD) == ""
        assert acct.configured() and acct.username() == "boss"
        assert acct.verify("boss", GOOD) is True

    def test_the_code_is_forgiving_about_case_spaces_and_dashes(self, acct):
        acct.ensure_setup_code()
        messy = code_of(acct).lower().replace("-", " ")
        assert acct.complete_setup("  " + messy + " ", "boss", GOOD) == ""

    def test_a_wrong_code_is_refused_and_nothing_is_stored(self, acct):
        acct.ensure_setup_code()
        err = acct.complete_setup("AAAA-AAAA-AAAA-AAAA", "boss", GOOD)
        assert "code" in err.lower() and not acct.configured()

    def test_without_a_code_ever_issued_setup_is_refused(self, acct):
        assert "code" in acct.complete_setup("anything", "boss", GOOD).lower()
        assert not acct.configured()

    def test_five_wrong_codes_lock_setup_for_a_while(self, acct, clock):
        acct.ensure_setup_code()
        for _ in range(5):
            acct.complete_setup("WRONG-WRONG-WRONG-WR", "boss", GOOD)
        err = acct.complete_setup(code_of(acct), "boss", GOOD)       # even the right one
        assert "too many" in err.lower() and not acct.configured()
        clock.now += 16 * 60
        assert acct.complete_setup(code_of(acct), "boss", GOOD) == ""

    @pytest.mark.parametrize("name", ["", "   ", "has space", "semi;colon", "x" * 65, "ünï", "a/b"])
    def test_bad_usernames_are_refused(self, acct, name):
        acct.ensure_setup_code()
        err = acct.complete_setup(code_of(acct), name, GOOD)
        assert "username" in err.lower() and not acct.configured()

    @pytest.mark.parametrize("pw", ["", "short", "x" * (MIN_PASSWORD - 1), "x" * 300, "boss"])
    def test_bad_passwords_are_refused(self, acct, pw):
        acct.ensure_setup_code()
        err = acct.complete_setup(code_of(acct), "boss", pw)
        assert "password" in err.lower() and not acct.configured()

    def test_a_password_equal_to_the_username_is_refused(self, acct):
        acct.ensure_setup_code()
        name = "a-long-admin-name"
        assert "password" in acct.complete_setup(code_of(acct), name, name).lower()

    def test_setup_cannot_run_twice(self, acct):
        acct.ensure_setup_code()
        code = code_of(acct)
        acct.complete_setup(code, "boss", GOOD)
        err = acct.complete_setup(code, "intruder", "another long password")
        assert "already" in err.lower() and acct.username() == "boss"


class TestStorage:
    def test_the_password_is_stored_hashed_and_the_file_is_private(self, acct, tmp_path):
        acct.ensure_setup_code()
        acct.complete_setup(code_of(acct), "boss", GOOD)
        path = tmp_path / "admin_account.json"
        raw = path.read_text()
        assert GOOD not in raw and json.loads(raw)["password_hash"].startswith(("scrypt:", "pbkdf2:"))
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0

    def test_it_survives_a_restart(self, acct, tmp_path, clock):
        acct.ensure_setup_code()
        acct.complete_setup(code_of(acct), "boss", GOOD)
        again = AdminAccount(str(tmp_path), clock=clock)
        assert again.configured() and again.verify("boss", GOOD)

    def test_a_damaged_file_means_not_configured_never_open(self, acct, tmp_path):
        (tmp_path / "admin_account.json").write_text("{nope")
        assert acct.configured() is False and acct.verify("boss", GOOD) is False

    def test_a_file_without_a_hash_means_not_configured(self, acct, tmp_path):
        (tmp_path / "admin_account.json").write_text(json.dumps({"username": "boss"}))
        assert acct.configured() is False


class TestVerify:
    @pytest.fixture
    def ready(self, acct):
        acct.ensure_setup_code()
        acct.complete_setup(code_of(acct), "boss", GOOD)
        return acct

    def test_right_pair(self, ready):
        assert ready.verify("boss", GOOD)

    @pytest.mark.parametrize("name,pw", [("boss", GOOD + "x"), ("Boss", GOOD), ("other", GOOD), ("", ""), ("boss", "")])
    def test_any_other_pair(self, ready, name, pw):
        assert ready.verify(name, pw) is False

    def test_nothing_verifies_before_setup(self, acct):
        assert acct.verify("boss", GOOD) is False and acct.verify("", "") is False


class TestReset:
    @pytest.fixture
    def ready(self, acct):
        acct.ensure_setup_code()
        acct.complete_setup(code_of(acct), "boss", GOOD)
        return acct

    def test_the_console_reset_changes_the_password(self, ready):
        assert ready.set_password("a brand new passphrase") == ""
        assert ready.verify("boss", "a brand new passphrase") and not ready.verify("boss", GOOD)

    def test_a_reset_can_also_rename(self, ready):
        assert ready.set_password("a brand new passphrase", username="chief") == ""
        assert ready.username() == "chief" and ready.verify("chief", "a brand new passphrase")

    def test_weak_passwords_are_refused_at_reset_too(self, ready):
        assert "password" in ready.set_password("short").lower()
        assert ready.verify("boss", GOOD)

    def test_a_reset_made_by_another_process_is_seen_without_a_restart(self, ready, tmp_path, clock):
        other = AdminAccount(str(tmp_path), clock=clock)          # the console command
        clock.now += 60
        other.set_password("changed from the console")
        assert ready.verify("boss", "changed from the console") and not ready.verify("boss", GOOD)

    def test_a_reset_records_when_so_older_sessions_can_be_dropped(self, ready, clock):
        before = ready.changed_at()
        clock.now += 500
        ready.set_password("a brand new passphrase")
        assert ready.changed_at() == before + 500

    def test_a_reset_works_on_an_install_that_was_never_set_up(self, acct):
        assert acct.set_password(GOOD, username="boss") == ""
        assert acct.configured() and acct.verify("boss", GOOD)

    def test_a_reset_with_no_account_and_no_username_asks_for_one(self, acct):
        assert "username" in acct.set_password(GOOD).lower()

"""The console command that resets the admin password inside the container."""

import io

import pytest

from nomadnet_web.admin_account import AdminAccount
from nomadnet_web.admin_cli import main

GOOD = "correct horse battery"


def run(tmp_path, argv, answers=(), stdin=""):
    out, err = io.StringIO(), io.StringIO()
    it = iter(answers)
    code = main(["--config-dir", str(tmp_path), *argv], prompt=lambda _p: next(it),
                stdin=io.StringIO(stdin), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def ready(tmp_path):
    a = AdminAccount(str(tmp_path))
    assert a.set_password(GOOD, username="boss") == ""
    return a


def test_reset_with_two_matching_answers(tmp_path, ready):
    code, out, err = run(tmp_path, ["reset-password"], ["a brand new passphrase"] * 2)
    assert code == 0 and "changed" in out.lower() and err == ""
    assert AdminAccount(str(tmp_path)).verify("boss", "a brand new passphrase")


def test_the_password_is_never_printed(tmp_path, ready):
    code, out, err = run(tmp_path, ["reset-password"], ["a brand new passphrase"] * 2)
    assert "brand new" not in out + err and GOOD not in out + err


def test_mismatched_answers_change_nothing(tmp_path, ready):
    code, out, err = run(tmp_path, ["reset-password"], ["a brand new passphrase", "something else here"])
    assert code != 0 and "match" in err.lower()
    assert AdminAccount(str(tmp_path)).verify("boss", GOOD)


def test_a_weak_password_is_explained_and_changes_nothing(tmp_path, ready):
    code, out, err = run(tmp_path, ["reset-password"], ["short", "short"])
    assert code != 0 and "at least" in err
    assert AdminAccount(str(tmp_path)).verify("boss", GOOD)


def test_it_can_rename_the_admin(tmp_path, ready):
    code, *_ = run(tmp_path, ["reset-password", "--username", "chief"], ["a brand new passphrase"] * 2)
    assert code == 0 and AdminAccount(str(tmp_path)).verify("chief", "a brand new passphrase")


def test_reading_the_password_from_stdin_for_scripts(tmp_path, ready):
    code, *_ = run(tmp_path, ["reset-password", "--password-stdin"], stdin="a brand new passphrase\n")
    assert code == 0 and AdminAccount(str(tmp_path)).verify("boss", "a brand new passphrase")


def test_a_fresh_install_needs_a_username(tmp_path):
    code, out, err = run(tmp_path, ["reset-password"], [GOOD, GOOD])
    assert code != 0 and "username" in err.lower()
    code, *_ = run(tmp_path, ["reset-password", "--username", "boss"], [GOOD, GOOD])
    assert code == 0 and AdminAccount(str(tmp_path)).verify("boss", GOOD)


def test_a_reset_removes_a_pending_setup_code(tmp_path):
    a = AdminAccount(str(tmp_path))
    a.ensure_setup_code()
    run(tmp_path, ["reset-password", "--username", "boss"], [GOOD, GOOD])
    assert not (tmp_path / "admin_setup_code").exists()


def test_status_says_what_exists_without_secrets(tmp_path, ready):
    code, out, _ = run(tmp_path, ["status"])
    assert code == 0 and "boss" in out and "scrypt" not in out and GOOD not in out
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out, _ = run(empty, ["status"])
    assert "no admin account" in out.lower()


def test_unknown_commands_fail_with_usage(tmp_path):
    with pytest.raises(SystemExit):
        run(tmp_path, ["bogus"])

"""Sign-in: the local login (admin password and admin-created users), lockout
after repeated failures, the open-redirect guard, session lifetime and
revocation, and who counts as an admin. The real blueprint and stores run
in a small Flask app; nothing else is started."""

import os
import time

import pytest
from flask import Flask

from nomadnet_web import auth, csrf as csrf_mod
from nomadnet_web.admin_routes import admin_bp
from nomadnet_web.cache import PageCache
from nomadnet_web.user_store import UserStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def clean_state():
    auth._user_cache.clear()
    auth._failed_attempts.clear()
    yield
    auth._user_cache.clear()
    auth._failed_attempts.clear()


@pytest.fixture
def app(tmp_path):
    a = Flask(__name__, root_path=ROOT)
    a.config.update(TESTING=True, SECRET_KEY="t", ADMIN_USERNAME="root", ADMIN_PASSWORD="s3cret",
                    USER_STORE=UserStore(str(tmp_path / "users.yml")), CACHE=PageCache(),
                    RNS_CONFIG_DIR=str(tmp_path))
    auth.login_manager.login_view = "auth.login_page"
    auth.login_manager.init_app(a)
    csrf_mod.init_csrf(a)
    a.register_blueprint(auth.auth_bp)
    a.register_blueprint(admin_bp)
    a.add_url_rule("/", "index", lambda: "home")
    return a


def login(app, username, password, ip="1.2.3.4"):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = "tok"
    r = c.post("/auth/local", data={"username": username, "password": password, "_csrf_token": "tok"},
               environ_overrides={"REMOTE_ADDR": ip})
    return c, r


class TestSafeNext:
    @pytest.mark.parametrize("candidate", [
        "https://evil.example/x", "//evil.example/x", "http://evil.example", "javascript:alert(1)",
        "relative/path", "", None, 5, "/ok\r\nSet-Cookie: x=1", "/ok\nHost: evil", "\\\\evil\\x",
    ])
    def test_anything_that_could_leave_the_site_falls_back(self, candidate):
        assert auth._safe_next_or_default(candidate, "/admin/") == "/admin/"

    @pytest.mark.parametrize("candidate", ["/admin/users", "/admin/users?x=1#top", "/"])
    def test_same_site_paths_are_kept(self, candidate):
        assert auth._safe_next_or_default(candidate, "/admin/") == candidate


class TestLocalLogin:
    def test_the_admin_password_signs_in_a_super_admin(self, app):
        c, r = login(app, "root", "s3cret")
        assert r.status_code == 302 and r.headers["Location"].endswith("/admin/")
        sessions = auth.list_sessions()
        assert len(sessions) == 1 and sessions[0]["is_admin"] and sessions[0]["super_admin"]
        assert sessions[0]["sub"] == "local:root" and sessions[0]["login_ip"] == "1.2.3.4"

    def test_a_wrong_password_is_refused_and_no_session_exists(self, app):
        c, r = login(app, "root", "nope")
        assert r.status_code == 401 and b"Invalid username or password" in r.data
        assert auth.list_sessions() == []

    def test_a_wrong_username_is_refused(self, app):
        assert login(app, "admin2", "s3cret")[1].status_code == 401

    def test_login_is_off_when_no_admin_password_is_set(self, app):
        app.config["ADMIN_PASSWORD"] = ""
        r = login(app, "root", "")[1]
        assert r.status_code == 403 and b"disabled" in r.data

    def test_a_missing_csrf_token_is_refused(self, app):
        c = app.test_client()
        with c.session_transaction() as s:
            s["_csrf"] = "tok"
        r = c.post("/auth/local", data={"username": "root", "password": "s3cret"})
        assert r.status_code == 403 and auth.list_sessions() == []

    def test_admin_created_users_cannot_sign_in_either_without_an_admin_password(self, app):
        # Current behaviour, pinned: the "disabled" check comes before the user
        # store is consulted, so ADMIN_PASSWORD gates every local account.
        app.config["USER_STORE"].create_local_user("bob", "pw12345678")
        app.config["ADMIN_PASSWORD"] = ""
        assert login(app, "bob", "pw12345678")[1].status_code == 403

    def test_an_admin_created_user_signs_in_as_an_ordinary_user(self, app):
        app.config["USER_STORE"].create_local_user("bob", "pw12345678", is_admin=False)
        c, r = login(app, "bob", "pw12345678")
        assert r.status_code == 302 and r.headers["Location"].endswith("/")
        assert not r.headers["Location"].endswith("/admin/")
        s = auth.list_sessions()[0]
        assert s["sub"] == "local:bob" and not s["is_admin"] and not s["super_admin"]

    def test_an_admin_created_admin_goes_to_the_dashboard_but_is_not_super(self, app):
        app.config["USER_STORE"].create_local_user("amy", "pw12345678", is_admin=True)
        c, r = login(app, "amy", "pw12345678")
        assert r.headers["Location"].endswith("/admin/")
        s = auth.list_sessions()[0]
        assert s["is_admin"] and not s["super_admin"]

    def test_a_signed_in_admin_can_open_the_admin_area_and_a_user_cannot(self, app):
        c, _ = login(app, "root", "s3cret")
        assert c.get("/admin/cache").status_code == 200
        app.config["USER_STORE"].create_local_user("bob", "pw12345678", is_admin=False)
        c2, _ = login(app, "bob", "pw12345678", ip="9.9.9.9")
        assert c2.get("/admin/cache").status_code == 403


class TestLockout:
    def test_ten_failures_lock_that_address_and_username_out(self, app):
        for _ in range(auth._LOGIN_MAX):
            assert login(app, "root", "bad")[1].status_code == 401
        r = login(app, "root", "s3cret")[1]            # even the right password now
        assert r.status_code == 429 and auth.list_sessions() == []

    def test_the_lockout_is_per_address_and_username(self, app):
        for _ in range(auth._LOGIN_MAX):
            login(app, "root", "bad", ip="1.1.1.1")
        assert login(app, "root", "s3cret", ip="2.2.2.2")[1].status_code == 302
        assert login(app, "someone-else", "x", ip="1.1.1.1")[1].status_code == 401

    def test_old_failures_stop_counting(self, app):
        old = time.time() - auth._LOGIN_WINDOW - 5
        auth._failed_attempts[("1.2.3.4", "root")] = [old] * auth._LOGIN_MAX
        assert login(app, "root", "s3cret")[1].status_code == 302

    def test_an_address_hammering_many_usernames_is_slowed_down(self, app):
        codes = [login(app, f"u{i}", "x", ip="5.5.5.5")[1].status_code for i in range(22)]
        assert codes[:20] == [401] * 20 and codes[20:] == [429, 429]


class TestSessions:
    def test_a_session_older_than_the_limit_is_gone(self, app):
        c, _ = login(app, "root", "s3cret")
        auth._user_cache["local:root"]["login_at"] -= auth.SESSION_TTL + 1
        assert auth.list_sessions() == []
        assert auth.load_user("local:root") is None
        assert "local:root" not in auth._user_cache

    def test_revoking_one_session_signs_that_person_out(self, app):
        c, _ = login(app, "root", "s3cret")
        assert c.get("/admin/cache").status_code == 200
        assert auth.revoke_session("local:root") is True
        assert auth.revoke_session("local:root") is False
        assert c.get("/admin/cache").status_code in (302, 401)

    def test_revoking_all_reports_how_many(self, app):
        login(app, "root", "s3cret")
        app.config["USER_STORE"].create_local_user("bob", "pw12345678")
        login(app, "bob", "pw12345678", ip="3.3.3.3")
        assert auth.revoke_all_sessions() == 2 and auth.list_sessions() == []

    def test_unknown_users_load_as_nobody(self, app):
        assert auth.load_user("nobody") is None


class TestAdminResolution:
    def run(self, app, email, sub):
        with app.test_request_context("/"):
            return auth._is_admin(email, sub)

    def test_nobody_is_admin_by_default(self, app):
        assert self.run(app, "a@x.org", "sub1") is False

    def test_the_email_and_subject_allowlists_grant_admin(self, app):
        app.config.update(OIDC_ADMIN_EMAILS=["boss@x.org"], OIDC_ADMIN_SUBJECTS=["sub-9"])
        assert self.run(app, "boss@x.org", "other") is True
        assert self.run(app, "x@x.org", "sub-9") is True
        assert self.run(app, "x@x.org", "sub-1") is False

    def test_a_per_user_flag_beats_the_allowlist_in_both_directions(self, app):
        store = app.config["USER_STORE"]
        app.config.update(OIDC_ADMIN_EMAILS=["boss@x.org"])
        store.register_or_update("sub-boss", "boss@x.org", "Boss")
        store.set_admin("sub-boss", False)
        assert self.run(app, "boss@x.org", "sub-boss") is False        # demoted stays demoted
        store.register_or_update("sub-new", "new@x.org", "New")
        store.set_admin("sub-new", True)
        assert self.run(app, "new@x.org", "sub-new") is True

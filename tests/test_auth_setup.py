"""First-run setup and the stored admin account, through the real sign-in
routes: the page, the one-time code, signing in afterwards, the environment
override, and sessions ending after a console reset."""

import os
import time

import pytest
from flask import Flask

from nomadnet_web import auth, csrf as csrf_mod
from nomadnet_web.admin_account import AdminAccount
from nomadnet_web.admin_routes import admin_bp
from nomadnet_web.cache import PageCache
from nomadnet_web.user_store import UserStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOOD = "correct horse battery"


@pytest.fixture(autouse=True)
def clean_state():
    auth._user_cache.clear()
    auth._failed_attempts.clear()
    yield
    auth._user_cache.clear()
    auth._failed_attempts.clear()


@pytest.fixture
def app(tmp_path, monkeypatch):
    a = Flask(__name__, root_path=ROOT)
    acct = AdminAccount(str(tmp_path))
    a.config.update(TESTING=True, SECRET_KEY="t", ADMIN_USERNAME="admin", ADMIN_PASSWORD="",
                    ADMIN_ACCOUNT=acct, CACHE=PageCache(), RNS_CONFIG_DIR=str(tmp_path),
                    USER_STORE=UserStore(str(tmp_path / "users.yml")))
    monkeypatch.setattr(auth.login_manager, "login_view", "auth.login_page")
    auth.login_manager.init_app(a)
    csrf_mod.init_csrf(a)
    a.register_blueprint(auth.auth_bp)
    a.register_blueprint(admin_bp)
    a.add_url_rule("/", "index", lambda: "home")
    acct.ensure_setup_code()
    return a


def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = "tok"
    return c


def code(app):
    return open(app.config["ADMIN_ACCOUNT"].setup_code_path).read().strip()


def setup(app, c=None, **over):
    c = c or client(app)
    body = {"code": code(app) if os.path.exists(app.config["ADMIN_ACCOUNT"].setup_code_path) else "x",
            "username": "boss", "password": GOOD, "confirm": GOOD, "_csrf_token": "tok"}
    body.update(over)
    return c, c.post("/auth/setup", data=body, environ_overrides={"REMOTE_ADDR": "4.4.4.4"})


def login(app, username, password, ip="1.2.3.4"):
    c = client(app)
    return c, c.post("/auth/local", data={"username": username, "password": password, "_csrf_token": "tok"},
                     environ_overrides={"REMOTE_ADDR": ip})


class TestFirstRun:
    def test_the_login_page_sends_you_to_setup(self, app):
        r = client(app).get("/auth/login")
        assert r.status_code == 302 and r.headers["Location"].endswith("/auth/setup")

    def test_the_setup_page_asks_for_the_code_but_never_shows_it(self, app):
        r = client(app).get("/auth/setup")
        assert r.status_code == 200 and b'name="code"' in r.data and b'name="password"' in r.data
        assert code(app).encode() not in r.data
        assert b"admin_setup_code" in r.data              # tells the operator where to look

    def test_signing_in_before_setup_points_to_setup(self, app):
        r = login(app, "admin", "anything")[1]
        assert r.status_code == 302 and r.headers["Location"].endswith("/auth/setup")
        assert auth.list_sessions() == []

    def test_the_right_code_creates_the_account_and_signs_in(self, app):
        c, r = setup(app)
        assert r.status_code == 302 and r.headers["Location"].endswith("/admin/")
        s = auth.list_sessions()
        assert len(s) == 1 and s[0]["sub"] == "local:boss" and s[0]["super_admin"]
        assert c.get("/admin/cache").status_code == 200
        assert not os.path.exists(app.config["ADMIN_ACCOUNT"].setup_code_path)

    def test_a_wrong_code_makes_no_account(self, app):
        c, r = setup(app, code="AAAA-AAAA-AAAA-AAAA")
        assert r.status_code == 400 and b"not right" in r.data
        assert not app.config["ADMIN_ACCOUNT"].configured() and auth.list_sessions() == []

    def test_mismatched_passwords_make_no_account(self, app):
        c, r = setup(app, confirm="something else entirely")
        assert r.status_code == 400 and b"match" in r.data
        assert not app.config["ADMIN_ACCOUNT"].configured()

    def test_a_weak_password_is_explained(self, app):
        c, r = setup(app, password="short", confirm="short")
        assert r.status_code == 400 and b"at least" in r.data

    def test_setup_needs_the_csrf_token(self, app):
        c = client(app)
        r = c.post("/auth/setup", data={"code": code(app), "username": "boss",
                                         "password": GOOD, "confirm": GOOD})
        assert r.status_code == 403 and not app.config["ADMIN_ACCOUNT"].configured()

    def test_setup_is_closed_once_an_account_exists(self, app):
        setup(app)
        auth._user_cache.clear()
        r = client(app).get("/auth/setup")
        assert r.status_code == 302 and r.headers["Location"].endswith("/auth/login")
        c, r = setup(app, username="intruder")
        assert r.status_code in (302, 403) and app.config["ADMIN_ACCOUNT"].username() == "boss"

    def test_guessing_codes_is_slowed_down(self, app):
        codes = [setup(app, code=f"WRONG-WRONG-WRONG-{i:04d}")[1].status_code for i in range(7)]
        assert codes[:5] == [400] * 5 and codes[5:] == [429, 429]
        assert not app.config["ADMIN_ACCOUNT"].configured()


class TestAfterSetup:
    @pytest.fixture
    def ready(self, app):
        setup(app)
        auth._user_cache.clear()
        return app

    def test_the_login_page_is_the_normal_one(self, ready):
        assert client(ready).get("/auth/login").status_code == 200

    def test_the_stored_account_signs_in_as_super_admin(self, ready):
        c, r = login(ready, "boss", GOOD)
        assert r.status_code == 302 and r.headers["Location"].endswith("/admin/")
        assert auth.list_sessions()[0]["super_admin"]

    @pytest.mark.parametrize("name,pw", [("boss", "wrong"), ("admin", GOOD), ("", "")])
    def test_anything_else_is_refused(self, ready, name, pw):
        r = login(ready, name, pw)[1]
        assert r.status_code == 401 and auth.list_sessions() == []

    def test_repeated_failures_lock_out_as_before(self, ready):
        for _ in range(auth._LOGIN_MAX):
            login(ready, "boss", "bad")
        assert login(ready, "boss", GOOD)[1].status_code == 429

    def test_admin_created_users_work_with_no_environment_password(self, ready):
        ready.config["USER_STORE"].create_local_user("bob", "pw12345678")
        r = login(ready, "bob", "pw12345678")[1]
        assert r.status_code == 302 and not auth.list_sessions()[0]["super_admin"]

    def test_a_console_reset_ends_older_sessions_but_not_newer_ones(self, ready):
        login(ready, "boss", GOOD)
        with ready.test_request_context("/"):
            assert auth.load_user("local:boss") is not None
            time.sleep(0.01)
            ready.config["ADMIN_ACCOUNT"].set_password("a brand new passphrase")
            assert auth.load_user("local:boss") is None                    # signed out by the reset
        r = login(ready, "boss", "a brand new passphrase")[1]
        assert r.status_code == 302
        with ready.test_request_context("/"):
            assert auth.load_user("local:boss") is not None

    def test_the_old_password_stops_working_after_a_reset(self, ready):
        ready.config["ADMIN_ACCOUNT"].set_password("a brand new passphrase")
        assert login(ready, "boss", GOOD)[1].status_code == 401


class TestEnvironmentOverride:
    @pytest.fixture
    def envapp(self, app):
        app.config.update(ADMIN_PASSWORD="from-the-env", ADMIN_USERNAME="root")
        return app

    def test_with_the_variable_set_there_is_no_setup(self, envapp):
        assert client(envapp).get("/auth/login").status_code == 200
        r = client(envapp).get("/auth/setup")
        assert r.status_code == 302 and r.headers["Location"].endswith("/auth/login")
        c, r = setup(envapp)
        assert r.status_code in (302, 403) and not envapp.config["ADMIN_ACCOUNT"].configured()

    def test_the_variable_signs_in_a_super_admin(self, envapp):
        r = login(envapp, "root", "from-the-env")[1]
        assert r.status_code == 302 and auth.list_sessions()[0]["super_admin"]

    def test_the_variable_wins_over_a_stored_account(self, envapp):
        envapp.config["ADMIN_ACCOUNT"].set_password(GOOD, username="boss")
        assert login(envapp, "boss", GOOD)[1].status_code == 401
        assert login(envapp, "root", "from-the-env", ip="8.8.8.8")[1].status_code == 302

    def test_a_reset_does_not_end_environment_sessions(self, envapp):
        login(envapp, "root", "from-the-env")
        envapp.config["ADMIN_ACCOUNT"].set_password(GOOD, username="boss")
        with envapp.test_request_context("/"):
            assert auth.load_user("local:root") is not None

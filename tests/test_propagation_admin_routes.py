"""Admin API for the propagation node: only admins, CSRF enforced, the host
driven through its small interface, errors reported plainly."""

import os

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin

from nomadnet_web import csrf as csrf_mod
from nomadnet_web.admin_routes import admin_bp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class U(UserMixin):
    def __init__(self, uid, admin):
        self.id, self.name, self.is_admin = uid, uid, admin


class FakeHost:
    def __init__(self):
        self.patches = []
        self.announced = 0
        self.errors = []

    def status(self):
        return {"state": "off", "settings": {"enabled": False}}

    def update(self, patch):
        self.patches.append(patch)
        return {"errors": self.errors, "settings": {}, "status": self.status()}

    def announce(self):
        self.announced += 1
        return {"ok": True}


@pytest.fixture
def app():
    a = Flask(__name__, root_path=ROOT)
    a.config.update(TESTING=True, SECRET_KEY="t", PROPAGATION_HOST=FakeHost())
    lm = LoginManager(a)
    users = {"root": U("root", True), "bob": U("bob", False)}
    lm.user_loader(users.get)
    csrf_mod.init_csrf(a)
    a.register_blueprint(admin_bp)
    return a


def client_as(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        if uid:
            s["_user_id"] = uid
        s["_csrf"] = "tok"
    return c


H = {"X-CSRF-Token": "tok"}


def test_anonymous_is_turned_away(app):
    r = client_as(app, None).get("/admin/api/propagation")
    assert r.status_code in (302, 401)


def test_non_admin_is_forbidden(app):
    c = client_as(app, "bob")
    assert c.get("/admin/api/propagation").status_code == 403
    assert c.post("/admin/api/propagation", json={}, headers=H).status_code == 403
    assert c.post("/admin/api/propagation/announce", headers=H).status_code == 403


def test_admin_reads_status(app):
    r = client_as(app, "root").get("/admin/api/propagation")
    assert r.status_code == 200 and r.get_json()["state"] == "off"


def test_changes_need_the_csrf_token(app):
    r = client_as(app, "root").post("/admin/api/propagation", json={"enabled": True})
    assert r.status_code == 403 and app.config["PROPAGATION_HOST"].patches == []


def test_admin_updates_settings(app):
    r = client_as(app, "root").post("/admin/api/propagation", json={"enabled": True}, headers=H)
    assert r.status_code == 200
    assert app.config["PROPAGATION_HOST"].patches == [{"enabled": True}]


def test_validation_errors_come_back_as_400(app):
    app.config["PROPAGATION_HOST"].errors = ["storage_mb: must be a whole number from 1 to 1000000."]
    r = client_as(app, "root").post("/admin/api/propagation", json={"storage_mb": -1}, headers=H)
    assert r.status_code == 400 and "storage_mb" in r.get_json()["errors"][0]


def test_a_body_that_is_not_an_object_is_refused(app):
    r = client_as(app, "root").post("/admin/api/propagation", json=[1, 2], headers=H)
    assert r.status_code == 400 and app.config["PROPAGATION_HOST"].patches == []


def test_announce(app):
    r = client_as(app, "root").post("/admin/api/propagation/announce", headers=H)
    assert r.status_code == 200 and app.config["PROPAGATION_HOST"].announced == 1


def test_announce_refusal_is_429(app):
    app.config["PROPAGATION_HOST"].announce = lambda: {"ok": False, "retry_in_s": 30, "error": "soon"}
    r = client_as(app, "root").post("/admin/api/propagation/announce", headers=H)
    assert r.status_code == 429 and r.get_json()["retry_in_s"] == 30


def test_not_running_announce_is_409(app):
    app.config["PROPAGATION_HOST"].announce = lambda: {"ok": False, "error": "not running"}
    r = client_as(app, "root").post("/admin/api/propagation/announce", headers=H)
    assert r.status_code == 409


def test_without_a_host_it_says_so(app):
    app.config["PROPAGATION_HOST"] = None
    assert client_as(app, "root").get("/admin/api/propagation").status_code == 503


def test_the_admin_page_renders_for_admins_only(app):
    app.context_processor(lambda: {"app_title_html": "T"})
    app.add_url_rule("/logout", "auth.logout", lambda: "")
    app.add_url_rule("/admin-x", "admin.x", lambda: "")
    assert client_as(app, "bob").get("/admin/propagation").status_code == 403
    r = client_as(app, "root").get("/admin/propagation")
    assert r.status_code == 200 and b"admin-propagation.js" in r.data

"""The three propagation endpoints: status, pick a node, sync now."""

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin, login_user

from nomadnet_web import routes

NODE = "ab" * 16


class FakeSvc:
    def __init__(self):
        self.calls = []
        self.sync_result = {"ok": True, "error": None, "node": NODE}

    def status_for(self, user):
        return {"mode": "auto", "node": NODE, "nodes": [], "transfer": {"state": "idle"}}

    def set_preferred(self, user, node):
        self.calls.append(("set", user, node))
        if node == "bad":
            raise ValueError("A node hash is 32 hexadecimal characters")

    def sync_now(self, user):
        self.calls.append(("sync", user))
        return self.sync_result


def make_app(svc):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="t", PROP_SYNC=svc)
    lm = LoginManager(app)

    class User(UserMixin):
        id = "u1"

    user = User()
    lm.user_loader(lambda _uid: user)
    app.register_blueprint(routes.bp)

    @app.get("/__login")
    def _login():
        login_user(user)
        return "ok"

    return app


@pytest.fixture
def client():
    svc = FakeSvc()
    c = make_app(svc).test_client()
    c.get("/__login")
    c.svc = svc
    return c


def test_status_is_for_the_signed_in_account(client):
    r = client.get("/api/propagation")
    assert r.status_code == 200 and r.get_json()["node"] == NODE


def test_pinning_a_node(client):
    r = client.post("/api/propagation/node", json={"node": NODE})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.svc.calls == [("set", "u1", NODE)]


def test_going_back_to_automatic(client):
    assert client.post("/api/propagation/node", json={"node": ""}).status_code == 200
    assert client.svc.calls == [("set", "u1", "")]


def test_a_bad_hash_is_a_400_with_the_reason(client):
    r = client.post("/api/propagation/node", json={"node": "bad"})
    assert r.status_code == 400 and "32 hexadecimal" in r.get_json()["error"]


def test_sync_now(client):
    r = client.post("/api/propagation/sync")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.svc.calls == [("sync", "u1")]


def test_a_refused_sync_is_a_409_with_the_reason(client):
    client.svc.sync_result = {"ok": False, "error": "Please wait a few seconds before syncing again.", "node": NODE}
    r = client.post("/api/propagation/sync")
    assert r.status_code == 409 and "wait" in r.get_json()["error"]


@pytest.mark.parametrize("method,path", [("get", "/api/propagation"), ("post", "/api/propagation/node"),
                                         ("post", "/api/propagation/sync")])
def test_sign_in_is_required(method, path):
    svc = FakeSvc()
    r = getattr(make_app(svc).test_client(), method)(path)
    assert r.status_code in (302, 401) and svc.calls == []


def test_without_the_service_it_is_a_503():
    app = make_app(None)
    c = app.test_client()
    c.get("/__login")
    assert c.get("/api/propagation").status_code == 503

"""Cancel / retry endpoints, the announce hook that triggers automatic
re-sends, and the admin switch."""

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin, login_user

from nomadnet_web import routes
from nomadnet_web.lxmf_tracker import LXMFPeerTracker
from nomadnet_web.ui_settings import UISettings

PEER = "bb" * 16


class FakeMessaging:
    def __init__(self):
        self.calls = []
        self.result = (True, "")

    def cancel_send(self, msg_id, user):
        self.calls.append(("cancel", msg_id, user))
        return self.result

    def retry_send(self, msg_id, user):
        self.calls.append(("retry", msg_id, user))
        return self.result


@pytest.fixture
def client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "t"
    fake = FakeMessaging()
    app.config["MESSAGING"] = fake
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

    c = app.test_client()
    c.get("/__login")
    c.fake = fake
    c.app_ = app
    return c


class TestEndpoints:
    def test_cancel_passes_the_signed_in_user(self, client):
        r = client.post("/api/messages/m-123/cancel")
        assert r.status_code == 200 and r.get_json() == {"ok": True}
        assert client.fake.calls == [("cancel", "m-123", "u1")]

    def test_retry_passes_the_signed_in_user(self, client):
        r = client.post("/api/messages/m-123/retry")
        assert r.status_code == 200
        assert client.fake.calls == [("retry", "m-123", "u1")]

    @pytest.mark.parametrize("action", ["cancel", "retry"])
    def test_a_refusal_is_a_409_with_the_reason(self, client, action):
        client.fake.result = (False, "This message is already delivered")
        r = client.post(f"/api/messages/m1/{action}")
        assert r.status_code == 409
        assert r.get_json() == {"ok": False, "error": "This message is already delivered"}

    @pytest.mark.parametrize("action", ["cancel", "retry"])
    def test_sign_in_is_required(self, client, action):
        anon = Flask(__name__)
        anon.config.update(TESTING=True, SECRET_KEY="t", MESSAGING=FakeMessaging())
        LoginManager(anon).user_loader(lambda _u: None)
        anon.register_blueprint(routes.bp)
        r = anon.test_client().post(f"/api/messages/m1/{action}")
        assert r.status_code in (302, 401)
        assert anon.config["MESSAGING"].calls == []

    def test_no_messaging_service_is_a_503(self, client):
        client.app_.config["MESSAGING"] = None
        assert client.post("/api/messages/m1/cancel").status_code == 503


class TestAnnounceHook:
    def make(self, tmp_path):
        return LXMFPeerTracker(str(tmp_path))

    def test_listeners_hear_every_recorded_announce(self, tmp_path):
        t = self.make(tmp_path)
        heard = []
        t.add_announce_listener(heard.append)
        t.record(bytes.fromhex(PEER), None)
        t.record(bytes.fromhex(PEER), None)
        assert heard == [PEER, PEER]

    def test_a_failing_listener_does_not_stop_the_others_or_the_record(self, tmp_path):
        t = self.make(tmp_path)
        heard = []

        def boom(_h):
            raise RuntimeError("boom")
        t.add_announce_listener(boom)
        t.add_announce_listener(heard.append)
        t.record(bytes.fromhex(PEER), None)
        assert heard == [PEER] and any(p["hash"] == PEER for p in t.get_peers())

    def test_with_no_listeners_recording_is_unchanged(self, tmp_path):
        t = self.make(tmp_path)
        t.record(bytes.fromhex(PEER), None)
        assert len(t.get_peers()) == 1


class TestSetting:
    def test_on_by_default_and_persisted(self, tmp_path):
        ui = UISettings(str(tmp_path))
        assert ui.get_all()["lxmf_auto_resend"] is True
        ui.update({"lxmf_auto_resend": False})
        assert UISettings(str(tmp_path)).get_all()["lxmf_auto_resend"] is False

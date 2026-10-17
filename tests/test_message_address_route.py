"""POST /api/messages accepts the address forms people share, and learns
the public key from an lxma:// link only when it belongs to the address."""

import pytest
import RNS
from flask import Flask
from flask_login import LoginManager, UserMixin, login_user

from nomadnet_web import routes

HASH = "ab" * 16


class FakeMessaging:
    def __init__(self):
        self.sent = []

    def send_message(self, dest, content, title="", user_sub="", attachments=None):
        self.sent.append(dest)
        return True, "msg-id"


@pytest.fixture
def client():
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="t", MESSAGING=FakeMessaging())
    lm = LoginManager(app)

    class User(UserMixin):
        id = "u1"
        is_admin = True

    user = User()
    lm.user_loader(lambda _uid: user)
    app.register_blueprint(routes.bp)

    @app.get("/__login")
    def _login():
        login_user(user)
        return "ok"

    c = app.test_client()
    c.get("/__login")
    c.msg = app.config["MESSAGING"]
    return c


def post(c, dest, content="hi"):
    return c.post("/api/messages", json={"dest_hash": dest, "content": content})


@pytest.fixture
def person(monkeypatch):
    # Newer Reticulum's recall() notes the lookup on the running instance;
    # there is none in tests, so give it one that ignores the note.
    class _Instance:
        def _used_destination_data(self, _h):
            pass
    monkeypatch.setattr(RNS.Reticulum, "get_instance", staticmethod(lambda: _Instance()))
    i = RNS.Identity()
    dest = RNS.Destination.hash_from_name_and_identity("lxmf.delivery", i)
    yield i, dest
    RNS.Identity.known_destinations.pop(dest, None)


@pytest.mark.parametrize("form", [HASH, HASH.upper(), f"lxmf@{HASH}", f"<lxmf@{HASH}>", f"  {HASH}  "])
def test_every_accepted_form_reaches_the_sender_as_the_bare_hash(client, form):
    assert post(client, form).status_code == 200
    assert client.msg.sent == [HASH]


def test_a_qr_link_with_the_right_key_is_sent_and_the_identity_learned(client, person):
    i, dest = person
    assert RNS.Identity.recall(dest) is None
    r = post(client, f"lxma://{dest.hex()}:{i.get_public_key().hex()}")
    assert r.status_code == 200 and client.msg.sent == [dest.hex()]
    assert RNS.Identity.recall(dest) is not None


def test_a_qr_link_with_someone_elses_key_is_refused_and_plants_nothing(client, person):
    i, dest = person
    other = RNS.Identity()
    r = post(client, f"lxma://{dest.hex()}:{other.get_public_key().hex()}")
    assert r.status_code == 400 and "does not belong" in r.get_json()["error"]
    assert client.msg.sent == [] and RNS.Identity.recall(dest) is None


@pytest.mark.parametrize("bad,why", [
    ("not-an-address", "32 hexadecimal"), ("ab" * 15, "32 hexadecimal"),
    (f"lxma://{HASH}", "public key"), (f"lxma://{HASH}:{'zz' * 64}", "public key"),
])
def test_bad_addresses_get_a_helpful_400(client, bad, why):
    r = post(client, bad)
    assert r.status_code == 400 and why in r.get_json()["error"] and client.msg.sent == []


def test_a_key_is_not_learned_for_a_send_that_would_be_refused_anyway(client, person):
    i, dest = person
    r = post(client, f"lxma://{dest.hex()}:{i.get_public_key().hex()}", content="   ")
    assert r.status_code == 400 and RNS.Identity.recall(dest) is None


def test_an_oversize_address_is_still_refused(client):
    assert post(client, "a" * 300).status_code == 400


def test_the_multipart_path_normalises_too(client):
    r = client.post("/api/messages", data={"dest_hash": f"lxmf@{HASH}", "content": "hi"},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and client.msg.sent == [HASH]

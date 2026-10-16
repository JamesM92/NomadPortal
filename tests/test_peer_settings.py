"""Per-peer conversation settings (NomadNet 1.2.0's trusted/untrusted tabs,
pinned conversations and peer notes): stored on the contact record."""

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin, login_user

from nomadnet_web import routes
from nomadnet_web.contact_store import (
    MAX_NOTE_LEN, TRUST_LEVELS, ContactStore, ContactStoreManager,
)

PEER = "ab" * 16


@pytest.fixture
def store(tmp_path):
    return ContactStore(str(tmp_path))


class TestStore:
    def test_a_new_peer_is_unknown_unpinned_and_has_no_note(self, store):
        entry = store.set_peer_settings(PEER)
        assert entry["trust"] == "unknown" and entry["pinned"] is False and entry["note"] == ""

    def test_trust_levels(self, store):
        assert TRUST_LEVELS == ("unknown", "trusted", "untrusted")
        for level in TRUST_LEVELS:
            assert store.set_peer_settings(PEER, trust=level)["trust"] == level

    def test_unknown_trust_value_is_rejected(self, store):
        with pytest.raises(ValueError):
            store.set_peer_settings(PEER, trust="best-friend")

    def test_pin_and_unpin(self, store):
        assert store.set_peer_settings(PEER, pinned=True)["pinned"] is True
        assert store.set_peer_settings(PEER, pinned=False)["pinned"] is False

    def test_note_can_be_set_and_cleared(self, store):
        assert store.set_peer_settings(PEER, note="  met at the meetup  ")["note"] == "met at the meetup"
        assert store.set_peer_settings(PEER, note="")["note"] == ""

    def test_overlong_note_is_rejected(self, store):
        with pytest.raises(ValueError):
            store.set_peer_settings(PEER, note="x" * (MAX_NOTE_LEN + 1))

    def test_omitted_fields_are_left_alone(self, store):
        store.set_peer_settings(PEER, trust="trusted", pinned=True, note="hi")
        entry = store.set_peer_settings(PEER, pinned=False)
        assert (entry["trust"], entry["pinned"], entry["note"]) == ("trusted", False, "hi")

    def test_a_peer_with_no_contact_record_gets_one(self, store):
        assert store.get(PEER) is None
        store.set_peer_settings(PEER, trust="trusted")
        assert store.get(PEER)["hash"] == PEER

    def test_existing_fields_survive(self, store):
        store.upsert(PEER, name="Alice")
        store.set_blocked(PEER, True)
        entry = store.set_peer_settings(PEER, trust="trusted")
        assert entry["name"] == "Alice" and entry["blocked"] is True

    def test_persists_across_restarts(self, tmp_path):
        ContactStore(str(tmp_path)).set_peer_settings(PEER, trust="untrusted", pinned=True, note="spam?")
        again = ContactStore(str(tmp_path)).get(PEER)
        assert (again["trust"], again["pinned"], again["note"]) == ("untrusted", True, "spam?")

    def test_old_records_without_the_new_fields_read_as_defaults(self, store):
        store.upsert(PEER, name="Old")
        entry = store.get(PEER)
        assert "trust" not in entry                                   # not rewritten on load
        assert store.peer_view(PEER) == {"trust": "unknown", "pinned": False, "note": ""}


class TestRoute:
    @pytest.fixture
    def client(self, tmp_path):
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "t"
        mgr = ContactStoreManager(str(tmp_path))
        app.config["CONTACT_STORE"] = mgr
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
        c.mgr = mgr
        return c

    def post(self, client, body, peer=PEER):
        return client.post(f"/api/contacts/{peer}/settings", json=body)

    def test_sets_trust_pin_and_note(self, client):
        r = self.post(client, {"trust": "trusted", "pinned": True, "note": "friend"})
        assert r.status_code == 200
        c = r.get_json()["contact"]
        assert (c["trust"], c["pinned"], c["note"]) == ("trusted", True, "friend")
        assert client.mgr.for_user("u1").get(PEER)["trust"] == "trusted"

    def test_is_listed_with_the_contacts(self, client):
        self.post(client, {"pinned": True})
        listed = client.get("/api/contacts").get_json()["contacts"]
        assert [c["pinned"] for c in listed if c["hash"] == PEER] == [True]

    @pytest.mark.parametrize("body", [
        {"trust": "bff"}, {"trust": 5}, {"pinned": "yes"}, {"note": 5}, {"note": "x" * 2000},
    ])
    def test_bad_values_are_400(self, client, body):
        assert self.post(client, body).status_code == 400

    @pytest.mark.parametrize("peer", ["not-hex", "ab" * 100, "zz"])
    def test_bad_peer_hash_is_400(self, client, peer):
        assert self.post(client, {"pinned": True}, peer=peer).status_code == 400

    def test_requires_login(self, client):
        anon = Flask(__name__)
        anon.config.update(TESTING=True, SECRET_KEY="t", CONTACT_STORE=client.mgr)
        LoginManager(anon).user_loader(lambda _u: None)
        anon.register_blueprint(routes.bp)
        r = anon.test_client().post(f"/api/contacts/{PEER}/settings", json={"pinned": True})
        assert r.status_code in (302, 401)

    def test_an_empty_body_changes_nothing_but_succeeds(self, client):
        assert self.post(client, {}).status_code == 200

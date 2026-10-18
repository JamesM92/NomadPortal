"""The web UI's local file download must honour ``<file>.allowed`` exactly
like a Reticulum requester is held to, and must never serve an access
list itself."""

from unittest import mock

import pytest
from flask import Flask
from flask_login import LoginManager

from tests.job_table import JobsMixin
from nomadnet_web import routes
from nomadnet_web.cache import PageCache

HOSTED = "aa" * 16
ALICE = "ab" * 16


class Browser(JobsMixin):
    def __init__(self):
        self.__init_jobs__()
        self.scanner = None
        self.scan_required = False

    def is_blocked(self, _h):
        return False


@pytest.fixture
def client(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "open.txt").write_text("public")
    (files / "secret.txt").write_text("private")
    (files / "secret.txt.allowed").write_text(ALICE + "\n")
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "t"
    site = mock.Mock()
    site.node_hash.return_value = HOSTED
    site.files_dir.return_value = str(files)
    ui = mock.Mock()
    ui.get_all.return_value = {}
    app.config.update(BROWSER=Browser(), CACHE=PageCache(), SITE_SERVER=site, UI_SETTINGS=ui)
    LoginManager(app).user_loader(lambda _uid: None)
    app.register_blueprint(routes.bp)
    return app.test_client()


def fetch(c, name):
    return c.post("/api/file/fetch", json={"hash": HOSTED, "path": f"/file/{name}"})


def test_an_open_file_is_served(client):
    r = fetch(client, "open.txt")
    assert r.status_code == 200, r.get_json()


def test_a_restricted_file_is_refused_to_a_guest(client):
    r = fetch(client, "secret.txt")
    assert r.status_code == 403
    assert "private" not in r.get_data(as_text=True)


def test_an_access_list_cannot_be_downloaded(client):
    r = fetch(client, "secret.txt.allowed")
    assert r.status_code == 404
    assert ALICE not in r.get_data(as_text=True)


def test_a_listed_identity_may_download(client):
    with mock.patch.object(routes, "_local_identity_hex", return_value=ALICE):
        r = fetch(client, "secret.txt")
    assert r.status_code == 200, r.get_json()


def test_an_unlisted_identity_is_refused(client):
    with mock.patch.object(routes, "_local_identity_hex", return_value="cd" * 16):
        assert fetch(client, "secret.txt").status_code == 403

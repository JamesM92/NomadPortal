"""Route-level check that the handlers return the same denial responses
after moving to ``nomadnet_web.access``: status codes and exact message
text for browse, form submit and file download, plus the blocklist.
"""

import threading
from unittest import mock

import pytest
from flask import Flask
from flask_login import LoginManager

from nomadnet_web import routes
from nomadnet_web.cache import PageCache

HOSTED = "aa" * 16
OTHER = "cc" * 16


class Browser:
    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self._jobs = {}
        self._jobs_lock = threading.Lock()

    def _record_fetch(self, *a, **k):
        pass

    def is_blocked(self, node_hash):
        return node_hash.lower() in self.blocked

    def fetch_page_async(self, *a, **k):
        return "job1"


def build(settings, blocked=()):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "t"
    site = mock.Mock()
    site.node_hash.return_value = HOSTED
    site.fetch_page.return_value = (b">hello\n", None)
    ui = mock.Mock()
    ui.get_all.return_value = settings
    app.config.update(BROWSER=Browser(blocked), CACHE=PageCache(),
                      SITE_SERVER=site, UI_SETTINGS=ui)
    LoginManager(app).user_loader(lambda _uid: None)
    app.register_blueprint(routes.bp)
    return app.test_client()


@pytest.fixture
def locked():
    return build({"guests_default_lock": True})


def test_locked_guest_browse_get_is_401_with_login_message(locked):
    r = locked.get(f"/api/page?hash={OTHER}&path=/page/index.mu")
    assert r.status_code == 401
    assert r.get_json() == {"error": "Login required to browse external nodes"}


def test_locked_guest_page_fetch_is_401_with_login_message(locked):
    r = locked.post("/api/page/fetch", json={"hash": OTHER, "path": "/page/index.mu"})
    assert r.status_code == 401
    assert r.get_json() == {"error": "Login required to browse external nodes"}


def test_guest_form_submit_off_the_operators_nodes_is_401_with_form_message():
    # Lock OFF: browsing is open, but form submission is still confined.
    c = build({"guests_default_lock": False})
    r = c.post("/api/page/fetch",
               json={"hash": OTHER, "path": "/page/x.mu", "fields": {"a": "b"}})
    assert r.status_code == 401
    assert r.get_json() == {"error": "Login required to submit forms on external nodes"}


def test_locked_guest_file_fetch_is_401_with_file_message(locked):
    r = locked.post("/api/file/fetch", json={"hash": OTHER, "path": "/file/a.zip"})
    assert r.status_code == 401
    assert r.get_json() == {"error": "Login required to fetch files from external nodes"}


def test_blocked_node_is_403_with_the_blocked_message():
    c = build({}, blocked={OTHER})
    r = c.post("/api/page/fetch", json={"hash": OTHER, "path": "/page/index.mu"})
    assert r.status_code == 403
    assert r.get_json() == {"error": "This node has been blocked by the administrator."}


def test_locked_guest_may_still_reach_the_hosted_site(locked):
    r = locked.post("/api/page/fetch", json={"hash": HOSTED, "path": "/page/index.mu"})
    assert r.status_code not in (401, 403)

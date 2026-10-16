"""Route-level tests for the kiosk cache wiring in /api/page/fetch and
/api/page/poll: a cached copy rides along on fetch-start, a finished
anonymous fetch refreshes it, and anything that could be per-visitor
(form data, identified fetches) never touches it.

Uses a minimal Flask app with a mocked ``NodeBrowser`` so no Reticulum
stack is started.
"""

import threading

import pytest
from flask import Flask
from flask_login import LoginManager

from nomadnet_web import routes
from nomadnet_web.cache import PageCache
from nomadnet_web.kiosk_cache import KioskCache, kiosk_key

NODE = "ab" * 16


class FakeBrowser:
    def __init__(self):
        self._jobs = {}
        self._jobs_lock = threading.Lock()
        self.identify_seen = []

    def is_blocked(self, _h):
        return False

    def fetch_page_async(self, node_hash, path, field_data=None, identify_with=None):
        self.identify_seen.append(identify_with)
        self._jobs["job1"] = {
            "status": "fetching", "progress": 0.0,
            "node_hash": node_hash.lower(), "path": path,
        }
        return "job1"

    def get_job(self, job_id):
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            return dict(j) if j else None

    def drop_job(self, job_id, grace_seconds=0):
        self._jobs.pop(job_id, None)

    def finish(self, content):
        self._jobs["job1"].update(status="done", content=content, progress=1.0)


@pytest.fixture
def env(tmp_path):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    browser = FakeBrowser()
    kiosk = KioskCache(str(tmp_path))
    app.config.update(BROWSER=browser, CACHE=PageCache(), KIOSK_CACHE=kiosk)
    LoginManager(app).user_loader(lambda _uid: None)
    app.register_blueprint(routes.bp)
    return app, browser, kiosk


def start(app, **body):
    body.setdefault("hash", NODE)
    body.setdefault("path", "/page/index.mu")
    return app.test_client().post("/api/page/fetch", json=body)


def poll(app):
    return app.test_client().get("/api/page/poll?id=job1")


class TestFetchStart:
    def test_no_cached_key_when_nothing_stored(self, env):
        app, _b, _k = env
        r = start(app)
        assert r.status_code == 200
        assert "cached" not in r.get_json()

    def test_cached_copy_rides_along(self, env):
        app, _b, kiosk = env
        kiosk.set(kiosk_key(NODE, "/page/index.mu"), b">Hello\n")
        body = start(app).get_json()
        assert body["job_id"] == "job1"
        assert "Hello" in body["cached"]["html"]
        assert body["cached"]["micron"] == ">Hello\n"
        assert body["cached"]["age_s"] >= 0

    def test_form_submission_never_reads_the_cache(self, env):
        app, _b, kiosk = env
        kiosk.set(kiosk_key(NODE, "/page/index.mu"), b"stale")
        r = start(app, fields={"name": "x"})
        # Guests may not submit forms to external nodes; either way the
        # cached copy must not be offered.
        assert "cached" not in (r.get_json() or {})

    def test_job_is_flagged_cacheable_for_anonymous_get(self, env):
        app, browser, _k = env
        start(app)
        assert browser._jobs["job1"]["kiosk_cacheable"] is True


class TestPoll:
    def test_finished_anonymous_fetch_refreshes_the_cache(self, env):
        app, browser, kiosk = env
        start(app)
        browser.finish(b">Fresh\n")
        assert poll(app).status_code == 200
        assert kiosk.get(kiosk_key(NODE, "/page/index.mu")).content == b">Fresh\n"

    def test_failed_fetch_leaves_the_old_copy_alone(self, env):
        app, browser, kiosk = env
        kiosk.set(kiosk_key(NODE, "/page/index.mu"), b"old")
        start(app)
        browser._jobs["job1"].update(status="error", error="timeout")
        assert poll(app).status_code == 503
        assert kiosk.get(kiosk_key(NODE, "/page/index.mu")).content == b"old"

    def test_unflagged_job_is_not_cached(self, env):
        app, browser, kiosk = env
        start(app)
        browser._jobs["job1"].pop("kiosk_cacheable")
        browser.finish(b"private")
        poll(app)
        assert kiosk.get(kiosk_key(NODE, "/page/index.mu")) is None

    def test_empty_response_is_not_cached(self, env):
        app, browser, kiosk = env
        start(app)
        browser.finish(b"")
        poll(app)
        assert kiosk.get(kiosk_key(NODE, "/page/index.mu")) is None

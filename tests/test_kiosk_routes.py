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
        self.started = 0

    def is_blocked(self, _h):
        return False

    def fetch_page_async(self, node_hash, path, field_data=None, identify_with=None):
        self.started += 1
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

    def flag_job(self, job_id, **flags):
        if job_id not in self._jobs:
            return False
        self._jobs[job_id].update(flags)
        return True

    def add_finished_job(self, node_hash, path, content, error=None, **extra):
        self._jobs["local"] = {"status": "done", "content": content, "node_hash": node_hash,
                               "path": path, "error": error, **extra}
        return "local"

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

    def test_failed_cold_fetch_stores_nothing(self, env):
        app, browser, kiosk = env
        start(app)
        browser._jobs["job1"].update(status="error", error="timeout")
        assert poll(app).status_code == 503
        assert kiosk.get(kiosk_key(NODE, "/page/index.mu")) is None
        assert kiosk.stats()["entries"] == 0

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


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def aging(tmp_path):
    """Like ``env`` but with a clock the test can move."""
    clock = Clock()
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    browser = FakeBrowser()
    kiosk = KioskCache(str(tmp_path), clock=clock)
    app.config.update(BROWSER=browser, CACHE=PageCache(), KIOSK_CACHE=kiosk)
    LoginManager(app).user_loader(lambda _uid: None)
    app.register_blueprint(routes.bp)
    return app, browser, kiosk, clock


KEY = kiosk_key(NODE, "/page/index.mu")


class TestPageCacheDirective:
    """``#!c=N`` decides whether a saved copy is shown as-is or refreshed."""

    def test_copy_inside_its_window_is_served_without_asking_the_mesh(self, aging):
        app, browser, kiosk, clock = aging
        kiosk.set(KEY, b"#!c=3600\n>Hello\n")
        clock.now += 600
        body = start(app).get_json()
        assert body["job_id"] is None
        assert body["cached"]["fresh"] is True and "Hello" in body["cached"]["html"]
        assert browser.started == 0

    def test_copy_past_its_window_is_refreshed_behind_the_saved_copy(self, aging):
        app, browser, kiosk, clock = aging
        kiosk.set(KEY, b"#!c=3600\n>Hello\n")
        clock.now += 3601
        body = start(app).get_json()
        assert body["job_id"] == "job1" and "fresh" not in body["cached"]
        assert browser.started == 1

    def test_window_boundary_is_exclusive(self, aging):
        app, browser, kiosk, clock = aging
        kiosk.set(KEY, b"#!c=100\n>x\n")
        clock.now += 100
        assert start(app).get_json()["job_id"] == "job1"

    def test_no_directive_always_refreshes(self, aging):
        app, browser, kiosk, _clock = aging
        kiosk.set(KEY, b">Hello\n")
        body = start(app).get_json()
        assert body["job_id"] == "job1" and "fresh" not in body["cached"]

    def test_zero_always_refreshes_but_still_shows_the_saved_copy(self, aging):
        app, browser, kiosk, _clock = aging
        kiosk.set(KEY, b"#!c=0\n>Hello\n")
        body = start(app).get_json()
        assert body["job_id"] == "job1" and "Hello" in body["cached"]["html"]
        assert "fresh" not in body["cached"]

    def test_refresh_flag_forces_a_live_fetch(self, aging):
        app, browser, kiosk, _clock = aging
        kiosk.set(KEY, b"#!c=3600\n>Hello\n")
        body = start(app, refresh=True).get_json()
        assert body["job_id"] == "job1" and "fresh" not in body["cached"]
        assert browser.started == 1

    def test_form_submissions_never_get_the_fresh_shortcut(self, aging):
        app, browser, kiosk, _clock = aging
        kiosk.set(KEY, b"#!c=3600\n>x\n")
        r = start(app, fields={"a": "b"})
        assert "cached" not in (r.get_json() or {})

"""The portal's own hosted site and the saved copy. A page that works is
remembered on disk; when it later fails (a script that errors or times out,
a file that goes missing mid-edit), a plain anonymous visit still gets the
last good copy, with the red indicator, instead of an error. Forms and
identified visits are never remembered or substituted."""

from unittest import mock

import pytest
from flask import Flask
from flask_login import LoginManager

from tests.job_table import JobsMixin
from nomadnet_web import routes
from nomadnet_web.cache import PageCache
from nomadnet_web.kiosk_cache import KioskCache, kiosk_key

HOSTED = "aa" * 16
PATH = "/page/index.mu"


class Browser(JobsMixin):
    def __init__(self):
        self.__init_jobs__()

    def _record_fetch(self, *a, **k):
        pass

    def is_blocked(self, _h):
        return False


@pytest.fixture
def env(tmp_path):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="t")
    site = mock.Mock()
    site.node_hash.return_value = HOSTED
    ui = mock.Mock()
    ui.get_all.return_value = {}
    kiosk = KioskCache(str(tmp_path))
    app.config.update(BROWSER=Browser(), CACHE=PageCache(), SITE_SERVER=site,
                      UI_SETTINGS=ui, KIOSK_CACHE=kiosk)
    LoginManager(app).user_loader(lambda _uid: None)
    app.register_blueprint(routes.bp)
    return app, site, kiosk


def fetch(app, **body):
    body.setdefault("hash", HOSTED)
    body.setdefault("path", PATH)
    return app.test_client().post("/api/page/fetch", json=body)


def poll(app, job_id):
    return app.test_client().get(f"/api/page/poll?id={job_id}")


class TestRemembering:
    def test_a_good_anonymous_visit_is_saved(self, env):
        app, site, kiosk = env
        site.fetch_page.return_value = (b">Hello\n", None)
        fetch(app)
        assert kiosk.get(kiosk_key(HOSTED, PATH)).content == b">Hello\n"

    def test_a_form_submission_is_never_saved(self, env):
        app, site, kiosk = env
        site.fetch_page.return_value = (b">Saved your note\n", None)
        fetch(app, fields={"note": "x"})
        assert kiosk.get(kiosk_key(HOSTED, PATH)) is None

    def test_a_failed_visit_saves_nothing(self, env):
        app, site, kiosk = env
        site.fetch_page.return_value = (None, "script failed")
        fetch(app)
        assert kiosk.get(kiosk_key(HOSTED, PATH)) is None

    def test_an_identified_visit_is_not_saved(self, env):
        app, site, kiosk = env
        with mock.patch.object(routes, "_local_identity_hex", return_value="ee" * 16):
            site.fetch_page.return_value = (b">Hi Alice\n", None)
            fetch(app)
        assert kiosk.get(kiosk_key(HOSTED, PATH)) is None


class TestFallingBack:
    def saved(self, env, text=b">Last good\n"):
        app, site, kiosk = env
        kiosk.set(kiosk_key(HOSTED, PATH), text)
        site.fetch_page.return_value = (None, "script timed out")
        return app, site, kiosk

    def test_a_failure_with_a_saved_copy_shows_it_and_reports_the_failure(self, env):
        app, *_ = self.saved(env)
        r = fetch(app)
        body = r.get_json()
        assert r.status_code == 200
        assert "Last good" in body["cached"]["html"] and body["cached"]["micron"] == ">Last good\n"
        p = poll(app, body["job_id"])
        assert p.status_code == 503 and "script timed out" in p.get_json()["error"]

    def test_the_saved_copy_stays_after_a_failure(self, env):
        app, _site, kiosk = self.saved(env)
        fetch(app)
        assert kiosk.get(kiosk_key(HOSTED, PATH)).content == b">Last good\n"

    def test_a_failure_with_no_saved_copy_is_the_plain_error(self, env):
        app, site, _ = env
        site.fetch_page.return_value = (None, "script timed out")
        r = fetch(app)
        assert r.status_code == 503 and "script timed out" in r.get_json()["error"]

    def test_a_form_submission_never_gets_a_saved_copy_back(self, env):
        app, *_ = self.saved(env)
        r = fetch(app, fields={"a": "b"})
        assert r.status_code == 503 and "cached" not in r.get_json()

    def test_an_identified_visit_never_gets_a_saved_copy_back(self, env):
        app, *_ = self.saved(env)
        with mock.patch.object(routes, "_local_identity_hex", return_value="ee" * 16):
            r = fetch(app)
        assert r.status_code == 503 and "cached" not in r.get_json()

    def test_success_after_failure_replaces_the_copy(self, env):
        app, site, kiosk = self.saved(env)
        fetch(app)
        site.fetch_page.return_value = (b">Newer\n", None)
        app.config["CACHE"].clear()
        fetch(app)
        assert kiosk.get(kiosk_key(HOSTED, PATH)).content == b">Newer\n"

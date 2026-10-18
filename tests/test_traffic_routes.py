"""What gets counted at /api/page/fetch: views of the operator's own
nodes, split into live vs saved-copy, and nothing else."""

import json
from unittest import mock

import pytest
from flask import Flask
from flask_login import LoginManager

from tests.job_table import JobsMixin
from nomadnet_web import routes
from nomadnet_web.cache import PageCache
from nomadnet_web.kiosk_cache import KioskCache, kiosk_key
from nomadnet_web.traffic import TrafficCounters

HOSTED = "aa" * 16
PRESCRIBED = "dd" * 16
OTHER = "cc" * 16


class Browser(JobsMixin):
    def __init__(self):
        self.__init_jobs__()

    def is_blocked(self, _h):
        return False

    def _record_fetch(self, *a, **k):
        pass

    def fetch_page_async(self, *args, **k):
        return self._fetch_jobs.start(args[0], args[1] if len(args) > 1 else "/")


@pytest.fixture
def env(tmp_path):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "t"
    site = mock.Mock()
    site.node_hash.return_value = HOSTED
    site.fetch_page.return_value = (b">hi", None)
    ui = mock.Mock()
    ui.get_all.return_value = {"prescribed_nodes": [PRESCRIBED]}
    traffic = TrafficCounters(str(tmp_path), flush_interval_s=0)
    kiosk = KioskCache(str(tmp_path))
    app.config.update(BROWSER=Browser(), CACHE=PageCache(), SITE_SERVER=site,
                      UI_SETTINGS=ui, TRAFFIC=traffic, KIOSK_CACHE=kiosk)
    LoginManager(app).user_loader(lambda _uid: None)
    app.register_blueprint(routes.bp)
    return app.test_client(), traffic, kiosk, tmp_path


def start(c, node, path="/page/index.mu", **extra):
    return c.post("/api/page/fetch", json={"hash": node, "path": path, **extra})


def totals(traffic):
    return {p["page"]: (p["live"], p["saved"]) for p in traffic.summary(days=1)["top"]}


def test_hosted_site_view_is_counted_as_live(env):
    c, traffic, *_ = env
    start(c, HOSTED)
    assert totals(traffic) == {f"{HOSTED}:/page/index.mu": (1, 0)}


def test_prescribed_node_without_a_saved_copy_counts_as_live(env):
    c, traffic, *_ = env
    start(c, PRESCRIBED)
    assert totals(traffic) == {f"{PRESCRIBED}:/page/index.mu": (1, 0)}


def test_prescribed_node_served_from_a_saved_copy_counts_as_saved(env):
    c, traffic, kiosk, _ = env
    kiosk.set(kiosk_key(PRESCRIBED, "/page/index.mu"), b">cached")
    start(c, PRESCRIBED)
    assert totals(traffic) == {f"{PRESCRIBED}:/page/index.mu": (0, 1)}


def test_other_nodes_are_not_counted(env):
    c, traffic, *_ = env
    start(c, OTHER)
    assert totals(traffic) == {}


def test_form_submissions_are_not_counted(env):
    c, traffic, *_ = env
    start(c, HOSTED, fields={"a": "b"})
    assert totals(traffic) == {}


def test_nothing_about_the_visitor_reaches_the_stored_file(env):
    c, traffic, _k, tmp_path = env
    c.post("/api/page/fetch", json={"hash": HOSTED, "path": "/page/index.mu"},
           headers={"User-Agent": "SecretAgent/9", "X-Forwarded-For": "203.0.113.77"},
           environ_overrides={"REMOTE_ADDR": "198.51.100.9"})
    raw = (tmp_path / "traffic.json").read_text()
    for needle in ("SecretAgent", "203.0.113.77", "198.51.100.9"):
        assert needle not in raw
    assert set(json.loads(raw).values().__iter__().__next__()) == {f"{HOSTED}:/page/index.mu"}


def test_a_fresh_copy_served_without_the_mesh_counts_as_saved(env):
    c, traffic, kiosk, _ = env
    kiosk.set(kiosk_key(PRESCRIBED, "/page/index.mu"), b"#!c=3600\n>cached\n")
    body = start(c, PRESCRIBED).get_json()
    assert body["job_id"] is None and body["cached"]["fresh"] is True
    assert totals(traffic) == {f"{PRESCRIBED}:/page/index.mu": (0, 1)}

"""Health endpoints: ``/healthz`` keeps its container-health meaning
(503 whenever the mesh side is unhealthy), and ``/healthz/web`` answers
200 for as long as the website itself is serving."""

import time
from unittest import mock

from flask import Flask

from nomadnet_web import routes
from nomadnet_web.cache import PageCache
from nomadnet_web.kiosk_cache import KioskCache


class Browser:
    def __init__(self, ready=True, interfaces=None, status_raises=False, progress=None):
        self._ready = ready
        self._interfaces = interfaces
        self._raises = status_raises
        self._progress = progress or {}

    def is_ready(self):
        return self._ready

    def rns_init_progress(self):
        return self._progress

    def get_status(self):
        if self._raises:
            raise RuntimeError("secret internal detail")
        return {"interfaces": self._interfaces, "nodes_discovered": 3}


def client(browser, tmp_path=None, site=None):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config.update(BROWSER=browser, CACHE=PageCache(), START_TIME=time.time() - 5)
    if tmp_path is not None:
        app.config["KIOSK_CACHE"] = KioskCache(str(tmp_path))
    if site is not None:
        app.config["SITE_SERVER"] = site
    app.register_blueprint(routes.bp)
    return app.test_client()


UP = [{"name": "tcp", "online": True}]
DOWN = [{"name": "tcp", "online": False}]


class TestContainerHealthUnchanged:
    def test_ok_when_an_interface_is_online(self):
        r = client(Browser(interfaces=UP)).get("/healthz")
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "ok" and body["interfaces_online"] == 1
        assert body["nodes_discovered"] == 3

    def test_starting_while_rns_is_coming_up(self):
        r = client(Browser(ready=False, progress={"elapsed_seconds": 7})).get("/healthz")
        assert r.status_code == 503
        body = r.get_json()
        assert body["status"] == "starting" and body["elapsed_seconds"] == 7

    def test_degraded_with_no_interfaces(self):
        r = client(Browser(interfaces=[])).get("/healthz")
        assert (r.status_code, r.get_json()["reason"]) == (503, "no interfaces configured")

    def test_degraded_when_all_interfaces_are_offline(self):
        r = client(Browser(interfaces=DOWN)).get("/healthz")
        assert (r.status_code, r.get_json()["reason"]) == (503, "no interfaces online")

    def test_status_error_hides_the_exception_text(self):
        r = client(Browser(status_raises=True)).get("/healthz")
        assert r.status_code == 503
        assert "secret" not in r.get_data(as_text=True)

    def test_no_browser_is_starting(self):
        r = client(None).get("/healthz")
        assert (r.status_code, r.get_json()["status"]) == (503, "starting")

    def test_stalled_announce_loop_is_degraded(self):
        site = mock.Mock()
        site.auto_announce_enabled.return_value = True
        site.last_announce_at.return_value = time.time() - 1000
        site.announce_interval.return_value = 100
        r = client(Browser(interfaces=UP), site=site).get("/healthz")
        assert (r.status_code, r.get_json()["reason"]) == (503, "site announce loop appears stalled")

    def test_mesh_alias_matches_healthz(self):
        c = client(Browser(interfaces=DOWN))
        assert c.get("/healthz/mesh").status_code == c.get("/healthz").status_code == 503


class TestWebHealth:
    def test_stays_200_when_the_mesh_is_down(self):
        r = client(Browser(interfaces=DOWN)).get("/healthz/web")
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "ok" and body["mesh"] == "degraded"

    def test_stays_200_while_rns_is_still_starting(self):
        r = client(Browser(ready=False)).get("/healthz/web")
        assert (r.status_code, r.get_json()["mesh"]) == (200, "starting")

    def test_stays_200_even_if_the_browser_object_is_missing(self):
        assert client(None).get("/healthz/web").status_code == 200

    def test_reports_mesh_ok_and_saved_copy_counts(self, tmp_path):
        c = client(Browser(interfaces=UP), tmp_path=tmp_path)
        c.application.config["KIOSK_CACHE"].set("k", b"v")
        body = c.get("/healthz/web").get_json()
        assert body["mesh"] == "ok"
        assert body["saved_copies"] == {"entries": 1, "bytes": 1}

    def test_exposes_no_exception_text(self):
        r = client(Browser(status_raises=True)).get("/healthz/web")
        assert r.status_code == 200 and "secret" not in r.get_data(as_text=True)

"""``/api/media``: the one place an inline image is fetched for the page.

Access rules, size and type checks, caching, the concurrency limit, and
what the response says about itself."""

import threading
from unittest import mock

import pytest
from flask import Flask
from flask_login import LoginManager

from nomadnet_web import routes
from nomadnet_web.cache import PageCache
from nomadnet_web.kiosk_cache import KioskCache, kiosk_key

HOSTED = "aa" * 16
REMOTE = "dd" * 16
OTHER = "cc" * 16
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PATH = "/media/logo.webp"


class Browser:
    def __init__(self, result=(WEBP, None), blocked=()):
        self.result = result
        self.blocked = set(blocked)
        self.calls = []
        self._jobs = {}
        self._jobs_lock = threading.Lock()
        self.scanner = None
        self.scan_required = False

    def is_blocked(self, h):
        return h in self.blocked

    def fetch_page(self, node, path, **kw):
        self.calls.append((node, path, kw))
        return self.result


def build(tmp_path, settings=None, browser=None, pages=None):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "t"
    site = mock.Mock()
    site.node_hash.return_value = HOSTED
    site.pages_dir.return_value = str(pages or tmp_path / "pages")
    ui = mock.Mock()
    ui.get_all.return_value = settings or {}
    browser = browser or Browser()
    app.config.update(BROWSER=browser, CACHE=PageCache(), SITE_SERVER=site, UI_SETTINGS=ui,
                      KIOSK_CACHE=KioskCache(str(tmp_path / "kc")))
    LoginManager(app).user_loader(lambda _uid: None)
    app.register_blueprint(routes.bp)
    return app, browser


def get(app, node=REMOTE, path=PATH):
    return app.test_client().get(f"/api/media?hash={node}&path={path}")


class TestRemoteImage:
    def test_served_with_a_sniffed_type_and_safe_headers(self, tmp_path):
        app, browser = build(tmp_path)
        r = get(app)
        assert r.status_code == 200 and r.data == WEBP
        assert r.mimetype == "image/webp"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "private" in r.headers["Cache-Control"]
        node, path, kw = browser.calls[0]
        assert (node, path, kw["media_path"]) == (REMOTE, PATH, PATH)

    def test_second_request_comes_from_the_saved_copy(self, tmp_path):
        app, browser = build(tmp_path)
        get(app)
        assert get(app).status_code == 200
        assert len(browser.calls) == 1

    def test_the_type_comes_from_the_bytes_not_from_the_node(self, tmp_path):
        app, _ = build(tmp_path, browser=Browser((PNG, None)))
        assert get(app, path="/media/p.webp").mimetype == "image/png"

    @pytest.mark.parametrize("payload", [b"<html><script>alert(1)</script>", b"<svg onload=alert(1)>", b"plain text"])
    def test_non_images_are_refused_and_not_cached(self, tmp_path, payload):
        app, browser = build(tmp_path, browser=Browser((payload, None)))
        r = get(app)
        assert r.status_code == 502
        assert app.config["KIOSK_CACHE"].get(kiosk_key(REMOTE, PATH)) is None

    def test_oversize_images_are_refused(self, tmp_path):
        big = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * (3 * 1024 * 1024)
        app, _ = build(tmp_path, browser=Browser((big, None)))
        assert get(app).status_code == 413

    def test_node_errors_become_a_plain_502(self, tmp_path):
        app, _ = build(tmp_path, browser=Browser((None, "Link closed before response")))
        r = get(app)
        assert r.status_code == 502 and "Link closed" not in r.get_data(as_text=True)

    def test_empty_answer_is_not_found(self, tmp_path):
        app, _ = build(tmp_path, browser=Browser((b"", None)))
        assert get(app).status_code == 404

    def test_a_failed_fetch_caches_nothing(self, tmp_path):
        app, _ = build(tmp_path, browser=Browser((None, "timeout")))
        get(app)
        assert app.config["KIOSK_CACHE"].stats()["entries"] == 0


class TestAccess:
    def test_blocked_node_is_refused(self, tmp_path):
        app, browser = build(tmp_path, browser=Browser(blocked={REMOTE}))
        assert get(app).status_code == 403 and browser.calls == []

    def test_a_confined_guest_cannot_load_off_site_images(self, tmp_path):
        app, browser = build(tmp_path, {"guests_default_lock": True, "prescribed_nodes": [REMOTE]})
        r = get(app, node=OTHER)
        assert r.status_code == 401 and browser.calls == []
        assert get(app, node=REMOTE).status_code == 200

    @pytest.mark.parametrize("path", [
        "/media/../x.webp", "/page/logo.webp", "/media/", "/file/logo.webp",
        "/media/" + "a" * 600,
    ])
    def test_bad_paths_are_400(self, tmp_path, path):
        app, browser = build(tmp_path)
        assert get(app, path=path).status_code == 400 and browser.calls == []

    def test_bad_node_is_400(self, tmp_path):
        app, _ = build(tmp_path)
        assert get(app, node="not-hex").status_code == 400


class TestConcurrency:
    def test_when_all_slots_are_busy_the_answer_is_503_with_retry_after(self, tmp_path):
        app, browser = build(tmp_path)
        held = [routes._MEDIA_SLOTS.acquire(blocking=False) for _ in range(3)]
        try:
            r = get(app)
        finally:
            for ok in held:
                if ok:
                    routes._MEDIA_SLOTS.release()
        assert r.status_code == 503 and r.headers["Retry-After"] == "2" and browser.calls == []

    def test_a_slot_is_released_after_a_fetch_even_if_it_raises(self, tmp_path):
        class Boom(Browser):
            def fetch_page(self, *a, **k):
                raise RuntimeError("boom")
        app, _ = build(tmp_path, browser=Boom())
        with pytest.raises(RuntimeError):
            get(app)
        free = [routes._MEDIA_SLOTS.acquire(blocking=False) for _ in range(3)]
        try:
            assert all(free)
        finally:
            for ok in free:
                if ok:
                    routes._MEDIA_SLOTS.release()


class TestHostedImage:
    def test_served_from_the_pages_directory_without_touching_the_mesh(self, tmp_path):
        pages = tmp_path / "pages"
        pages.mkdir()
        (pages / "logo.webp").write_bytes(WEBP)
        (pages / "pic.png").write_bytes(PNG)
        app, browser = build(tmp_path, pages=pages)
        r = get(app, node=HOSTED)
        assert r.status_code == 200 and r.mimetype == "image/webp" and browser.calls == []
        assert get(app, node=HOSTED, path="/media/pic.png").mimetype == "image/png"

    def test_missing_file_is_404(self, tmp_path):
        (tmp_path / "pages").mkdir()
        app, _ = build(tmp_path)
        assert get(app, node=HOSTED).status_code == 404

    def test_a_restricted_image_is_not_served_to_a_guest(self, tmp_path):
        pages = tmp_path / "pages"
        pages.mkdir()
        (pages / "logo.webp").write_bytes(WEBP)
        (pages / "logo.webp.allowed").write_text("ab" * 16)
        app, _ = build(tmp_path, pages=pages)
        r = get(app, node=HOSTED)
        assert r.status_code == 404 and WEBP not in r.data

    def test_a_script_renamed_to_an_image_is_not_served(self, tmp_path):
        pages = tmp_path / "pages"
        pages.mkdir()
        (pages / "logo.webp").write_bytes(b"<html><script>alert(1)</script>")
        app, _ = build(tmp_path, pages=pages)
        assert get(app, node=HOSTED).status_code == 404

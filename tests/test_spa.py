"""The shell route: what search engines and link previews see.

Covers the server-rendered page content, title / description / canonical
tags, the rule that executable pages are never run for a crawler, the
sitemap and robots.txt. Uses a minimal Flask app with a fake site server
and the real templates.
"""

import os
from unittest import mock

import pytest
from flask import Flask
from flask_login import LoginManager

from nomadnet_web.cache import PageCache
from nomadnet_web.kiosk_cache import KioskCache, kiosk_key
from nomadnet_web.spa import register_spa_routes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTED = "aa" * 16
LAND = "dd" * 16
OTHER = "cc" * 16

PAGES = {
    "/page/index.mu": b">Welcome home\nHello `[About us`about.mu] friends\n",
    "/page/about.mu": b">About us\nWe are a small group.\n",
}


class Site:
    def __init__(self):
        self.ran = []

    def node_hash(self):
        return HOSTED

    def read_static_page(self, path):
        return PAGES.get(path)

    def static_page_paths(self):
        return sorted(PAGES)


def build(tmp_path, settings=None, site=True):
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"),
                static_folder=os.path.join(ROOT, "static"))
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "t"
    app.jinja_env.globals["csrf_token"] = lambda: "tok"
    ui = mock.Mock()
    ui.get_all.return_value = {"site_name": "Test Site", **(settings or {})}
    browser = mock.Mock()
    browser.is_blocked.return_value = False
    app.config.update(BROWSER=browser, CACHE=PageCache(), UI_SETTINGS=ui,
                      KIOSK_CACHE=KioskCache(str(tmp_path)))
    if site:
        app.config["SITE_SERVER"] = Site()
    LoginManager(app).user_loader(lambda _uid: None)
    register_spa_routes(app)
    return app


@pytest.fixture
def hosted_only(tmp_path):
    return build(tmp_path).test_client()


@pytest.fixture
def kiosk(tmp_path):
    app = build(tmp_path, {"prescribed_nodes": [LAND]})
    return app, app.test_client()


class TestHostedSite:
    def test_root_contains_the_rendered_home_page(self, hosted_only):
        body = hosted_only.get("/").get_data(as_text=True)
        assert 'id="ssr-page"' in body and "Welcome home" in body

    def test_title_description_and_canonical(self, hosted_only):
        body = hosted_only.get("/").get_data(as_text=True)
        assert "<title>Welcome home · Test Site</title>" in body
        assert '<meta name="description" content="Welcome home' in body
        assert '<link rel="canonical" href="http://localhost/">' in body
        assert '<meta property="og:title" content="Welcome home · Test Site">' in body

    def test_welcome_block_is_kept_but_hidden(self, hosted_only):
        # app.js reads elements inside it at startup, so it must stay.
        body = hosted_only.get("/").get_data(as_text=True)
        assert 'id="welcome" style="display:none"' in body and 'id="welcome-guest"' in body

    def test_another_hosted_page_by_explicit_url(self, hosted_only):
        body = hosted_only.get(f"/n/{HOSTED}/page/about.mu").get_data(as_text=True)
        assert "<title>About us · Test Site</title>" in body
        assert f'href="http://localhost/n/{HOSTED}/page/about.mu"' in body

    def test_missing_page_gets_the_plain_shell_not_an_error(self, hosted_only):
        r = hosted_only.get(f"/n/{HOSTED}/page/nope.mu")
        body = r.get_data(as_text=True)
        assert r.status_code == 200 and 'id="ssr-page"' not in body
        assert "<title>Test Site</title>" in body and 'id="welcome"' in body

    def test_site_name_falls_back_to_the_app_title(self, tmp_path):
        app = build(tmp_path, {"site_name": "", "app_title": "`F4afMy Portal`f"})
        body = app.test_client().get(f"/n/{HOSTED}/page/nope.mu").get_data(as_text=True)
        assert "<title>" in body and "My Portal" in body

    def test_hostile_page_text_is_escaped_in_meta_tags(self, tmp_path):
        PAGES["/page/evil.mu"] = b'>"><script>alert(1)</script>\nbody\n'
        try:
            body = build(tmp_path).test_client().get(f"/n/{HOSTED}/page/evil.mu").get_data(as_text=True)
        finally:
            del PAGES["/page/evil.mu"]
        head = body.split("</head>")[0]
        assert "<script>alert(1)</script>" not in head


class TestExecutablePagesAreNeverRun:
    def test_site_server_without_a_static_copy_means_plain_shell(self, tmp_path):
        app = build(tmp_path)
        app.config["SITE_SERVER"].read_static_page = lambda p: None      # e.g. executable
        body = app.test_client().get("/").get_data(as_text=True)
        assert 'id="ssr-page"' not in body

    def test_fetch_page_is_never_called(self, tmp_path):
        app = build(tmp_path)
        site = app.config["SITE_SERVER"]
        site.fetch_page = mock.Mock(side_effect=AssertionError("must not run pages"))
        app.test_client().get("/")
        app.test_client().get("/sitemap.xml")


class TestRemotePrescribedNode:
    def test_saved_copy_is_rendered_at_the_short_url(self, kiosk):
        app, c = kiosk
        app.config["KIOSK_CACHE"].set(kiosk_key(LAND, "/page/index.mu"), b">Partner home\nhi\n")
        body = c.get("/").get_data(as_text=True)
        assert "Partner home" in body and 'id="ssr-page"' in body

    def test_short_path_belongs_to_the_landing_node(self, kiosk):
        app, c = kiosk
        app.config["KIOSK_CACHE"].set(kiosk_key(LAND, "/page/a.mu"), b">Partner A\nx\n")
        body = c.get("/page/a.mu").get_data(as_text=True)
        assert "Partner A" in body
        assert '<link rel="canonical" href="http://localhost/page/a.mu">' in body

    def test_no_saved_copy_never_waits_on_the_mesh(self, kiosk):
        app, c = kiosk
        r = c.get("/")
        assert r.status_code == 200 and 'id="ssr-page"' not in r.get_data(as_text=True)
        app.config["BROWSER"].fetch_page.assert_not_called()


class TestNotOursToPublish:
    def test_untrusted_node_is_marked_noindex_and_not_rendered(self, kiosk):
        app, c = kiosk
        app.config["KIOSK_CACHE"].set(kiosk_key(OTHER, "/page/index.mu"), b">Secret\n")
        body = c.get(f"/n/{OTHER}").get_data(as_text=True)
        assert '<meta name="robots" content="noindex">' in body
        assert "Secret" not in body and 'id="ssr-page"' not in body

    def test_blocked_node_is_not_rendered(self, kiosk):
        app, c = kiosk
        app.config["KIOSK_CACHE"].set(kiosk_key(LAND, "/page/index.mu"), b">Was fine\n")
        app.config["BROWSER"].is_blocked.side_effect = lambda h: h == LAND
        body = c.get("/").get_data(as_text=True)
        assert "Was fine" not in body and 'name="robots" content="noindex"' in body

    def test_legacy_page_entry_point_is_noindex(self, hosted_only):
        assert 'content="noindex"' in hosted_only.get("/page").get_data(as_text=True)

    def test_file_urls_are_not_rendered(self, kiosk):
        app, c = kiosk
        body = c.get("/file/x.pdf").get_data(as_text=True)
        assert 'id="ssr-page"' not in body


class TestRoutingUnchanged:
    def test_reserved_prefixes_are_real_404s(self, hosted_only):
        for p in ("/api/typo", "/admin/typo", "/auth/typo", "/static/typo"):
            assert hosted_only.get(p).status_code == 404, p


class TestSitemapAndRobots:
    def test_hosted_only_site(self, hosted_only):
        xml = hosted_only.get("/sitemap.xml").get_data(as_text=True)
        assert "<loc>http://localhost/</loc>" in xml
        assert f"<loc>http://localhost/n/{HOSTED}/page/about.mu</loc>" in xml
        assert xml.count("<loc>http://localhost/</loc>") == 1       # home listed once

    def test_prescribed_node_pages_come_from_saved_copies(self, kiosk):
        app, c = kiosk
        app.config["KIOSK_CACHE"].set(kiosk_key(LAND, "/page/a.mu"), b">A\n")
        app.config["KIOSK_CACHE"].set(kiosk_key(LAND, "/file/big.zip"), b"zip")
        app.config["KIOSK_CACHE"].set(kiosk_key(OTHER, "/page/z.mu"), b">Z\n")
        xml = c.get("/sitemap.xml").get_data(as_text=True)
        assert "<loc>http://localhost/page/a.mu</loc>" in xml
        assert "big.zip" not in xml and OTHER not in xml

    def test_blocked_nodes_are_left_out(self, kiosk):
        app, c = kiosk
        app.config["BROWSER"].is_blocked.side_effect = lambda h: h == LAND
        assert f"{LAND}" not in c.get("/sitemap.xml").get_data(as_text=True)

    def test_content_types(self, hosted_only):
        assert hosted_only.get("/sitemap.xml").mimetype == "application/xml"
        assert hosted_only.get("/robots.txt").mimetype == "text/plain"

    def test_robots_points_at_the_sitemap_and_keeps_pages_crawlable(self, hosted_only):
        txt = hosted_only.get("/robots.txt").get_data(as_text=True)
        assert "Sitemap: http://localhost/sitemap.xml" in txt
        assert "Disallow: /api/" in txt and "Disallow: /\n" not in txt


class TestPublicUrl:
    def test_pinned_public_url_is_used_everywhere(self, tmp_path):
        app = build(tmp_path)
        app.config["PUBLIC_URL"] = "https://nomad.example.org/"
        c = app.test_client()
        page = c.get("/", headers={"Host": "evil.example"}).get_data(as_text=True)
        assert '<link rel="canonical" href="https://nomad.example.org/">' in page
        assert "evil.example" not in page
        assert "<loc>https://nomad.example.org/</loc>" in c.get("/sitemap.xml").get_data(as_text=True)
        assert "Sitemap: https://nomad.example.org/sitemap.xml" in c.get("/robots.txt").get_data(as_text=True)

    def test_without_it_the_request_address_is_used(self, hosted_only):
        assert "http://localhost/sitemap.xml" in hosted_only.get("/robots.txt").get_data(as_text=True)

"""The server-rendered copy of a page includes its images and partials."""

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
    "/page/index.mu": (b">Home\n`(Our logo`:/media/logo.webp)\n"
                       b"`{/page/news.mu`30`pid=news}\n`{/page/form.mu`0`pid=f|name=x}\n`{/page/script.mu`0`pid=s}\n"),
    "/page/news.mu": b">Latest news\nWe opened a new node.\n",
    "/page/form.mu": b">A form partial\n",
    # script.mu is "executable": read_static_page refuses it (returns None)
}


class Site:
    def node_hash(self):
        return HOSTED

    def read_static_page(self, path):
        return PAGES.get(path)

    def static_page_paths(self):
        return sorted(PAGES)


def build(tmp_path, settings=None):
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"),
                static_folder=os.path.join(ROOT, "static"))
    app.config.update(TESTING=True, SECRET_KEY="t")
    app.jinja_env.globals["csrf_token"] = lambda: "tok"
    ui = mock.Mock()
    ui.get_all.return_value = {"site_name": "Test Site", **(settings or {})}
    browser = mock.Mock()
    browser.is_blocked.return_value = False
    app.config.update(BROWSER=browser, CACHE=PageCache(), UI_SETTINGS=ui,
                      KIOSK_CACHE=KioskCache(str(tmp_path)), SITE_SERVER=Site())
    LoginManager(app).user_loader(lambda _uid: None)
    register_spa_routes(app)
    return app


@pytest.fixture
def app(tmp_path):
    return build(tmp_path)


def home(app):
    return app.test_client().get("/").get_data(as_text=True)


def test_an_image_in_the_server_copy_has_a_source(app):
    body = home(app)
    assert f'src="/api/media?hash={HOSTED}&amp;path=%2Fmedia%2Flogo.webp"' in body
    assert "Our logo" in body


def test_a_static_partial_is_shown_in_the_server_copy(app):
    body = home(app)
    assert "Latest news" in body and "We opened a new node." in body
    assert 'class="mu-partial" data-pid="news"' in body


def test_a_partial_that_needs_form_input_stays_a_link(app):
    body = home(app)
    assert "A form partial" not in body


def test_an_executable_partial_is_never_run_for_a_crawler(app):
    body = home(app)
    assert "script.mu" in body                               # still there, as a link
    assert 'data-pid="s"' not in body.replace('class="mu-dynamic" data-fields="pid=s" data-pid="s"', "")


def test_a_partial_on_another_node_comes_from_the_saved_copy_only(tmp_path):
    app = build(tmp_path, {"prescribed_nodes": [LAND]})
    kiosk = app.config["KIOSK_CACHE"]
    kiosk.set(kiosk_key(LAND, "/page/index.mu"),
              f">Landing\n`{{hash://{OTHER}/page/p.mu`0`pid=x}}\n`{{/page/q.mu`0`pid=y}}\n".encode())
    kiosk.set(kiosk_key(LAND, "/page/q.mu"), b">Saved partial text\n")
    body = app.test_client().get("/").get_data(as_text=True)
    assert "Saved partial text" in body            # the same node's saved copy is used
    assert '<div class="mu-partial" data-pid="x"' not in body   # a partial on an untrusted node is not followed


def test_a_partial_nested_inside_a_partial_is_not_followed(tmp_path):
    app = build(tmp_path)
    PAGES["/page/news.mu"] = b">Latest news\n`{/page/form.mu`0`pid=deep}\n"
    try:
        body = app.test_client().get("/").get_data(as_text=True)
    finally:
        PAGES["/page/news.mu"] = b">Latest news\nWe opened a new node.\n"
    assert "A form partial" not in body            # depth is one level in the server copy

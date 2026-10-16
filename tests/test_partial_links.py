"""Partial-refresh links (`[label`p:pid]) and partials in rendered pages."""

from flask import Flask

from nomadnet_web.routes import PARTIAL_REFRESH_PREFIX, _converter, _web_url_resolver

H = "ab" * 16


def resolve(url):
    return _web_url_resolver(url, H, "/page/index.mu")


class TestResolver:
    def test_single_and_multiple_ids(self):
        assert resolve("p:news") == PARTIAL_REFRESH_PREFIX + "news"
        assert resolve("p:news|ticker") == PARTIAL_REFRESH_PREFIX + "news|ticker"

    def test_only_plain_ids_survive(self):
        assert resolve('p:ok|"><script>|a b|x/y') == PARTIAL_REFRESH_PREFIX + "ok"
        assert resolve("p:") == PARTIAL_REFRESH_PREFIX

    def test_ordinary_links_are_unaffected(self):
        assert resolve("about.mu").startswith("/page?url=")
        assert resolve("https://example.com") == "https://example.com"

    def test_a_page_named_like_a_partial_id_is_still_a_page(self):
        assert resolve("pages/p.mu").startswith("/page?url=")


class TestRenderedPage:
    def render(self, text):
        app = Flask(__name__)
        with app.test_request_context():
            return _converter.convert(text, node_hash=H, base_path="/page/index.mu",
                                      authenticated=True)

    def test_refresh_link_becomes_the_placeholder_href(self):
        html = self.render("`[Reload`p:news]")
        assert f'href="{PARTIAL_REFRESH_PREFIX}news"' in html and 'class="mu-link"' in html

    def test_partial_carries_its_settings_as_data_attributes(self):
        html = self.render("`{/page/news.mu`30`pid=news|x=1}")
        assert 'class="mu-dynamic"' in html
        assert 'data-refresh="30.0"' in html and 'data-pid="news"' in html
        assert 'data-fields="pid=news|x=1"' in html
        assert 'href="/page?url=' in html

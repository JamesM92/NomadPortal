"""Pure helpers behind the public-site SEO: URL mapping that mirrors the
browser's, titles, descriptions, the sitemap and robots.txt."""

from nomadnet_web import seo

LAND = "dd" * 16
HOSTED = "aa" * 16
OTHER = "cc" * 16


class TestResolvePathname:
    def test_root_is_the_landing_pages_index(self):
        assert seo.resolve_pathname("/", LAND, HOSTED) == (LAND, "/page/index.mu")

    def test_root_falls_back_to_the_hosted_site_when_no_landing(self):
        assert seo.resolve_pathname("/", None, HOSTED) == (HOSTED, "/page/index.mu")

    def test_root_with_nothing_configured_is_unresolvable(self):
        assert seo.resolve_pathname("/", None, None) is None

    def test_short_paths_belong_to_the_landing_node(self):
        assert seo.resolve_pathname("/page/foo.mu", LAND, HOSTED) == (LAND, "/page/foo.mu")

    def test_short_paths_need_a_landing_node(self):
        assert seo.resolve_pathname("/page/foo.mu", None, HOSTED) is None

    def test_explicit_node_form(self):
        assert seo.resolve_pathname(f"/n/{OTHER}/page/x.mu", LAND, HOSTED) == (OTHER, "/page/x.mu")
        assert seo.resolve_pathname(f"/n/{OTHER.upper()}", LAND, HOSTED) == (OTHER, "/page/index.mu")

    def test_the_bare_page_entry_point_is_not_a_page(self):
        assert seo.resolve_pathname("/page", LAND, HOSTED) is None
        assert seo.resolve_pathname("/page/", LAND, HOSTED) is None

    def test_pathname_without_leading_slash_is_tolerated(self):
        assert seo.resolve_pathname("page/foo.mu", LAND, HOSTED) == (LAND, "/page/foo.mu")


class TestPathnameFor:
    def test_landing_node_uses_short_paths(self):
        assert seo.pathname_for(LAND, "/page/index.mu", LAND, HOSTED) == "/"
        assert seo.pathname_for(LAND, "/page/a.mu", LAND, HOSTED) == "/page/a.mu"

    def test_hosted_only_site_home_is_root(self):
        assert seo.pathname_for(HOSTED, "/page/index.mu", None, HOSTED) == "/"

    def test_other_nodes_use_the_n_prefix(self):
        assert seo.pathname_for(OTHER, "/page/a.mu", LAND, HOSTED) == f"/n/{OTHER}/page/a.mu"
        assert seo.pathname_for(OTHER, "/page/index.mu", LAND, HOSTED) == f"/n/{OTHER}"

    def test_round_trips_with_resolve(self):
        for node, path in [(LAND, "/page/a.mu"), (OTHER, "/page/a.mu"), (LAND, "/page/index.mu")]:
            pn = seo.pathname_for(node, path, LAND, HOSTED)
            assert seo.resolve_pathname(pn, LAND, HOSTED) == (node, path)


class TestTitleAndDescription:
    plain = staticmethod(lambda s: s.replace("`F4af", "").replace("`f", "").strip())

    def test_title_is_the_first_heading_plus_site_name(self):
        t = seo.page_title(">Welcome\nbody", "My Site", self.plain)
        assert t == "Welcome · My Site"

    def test_deeper_heading_markers_are_stripped(self):
        assert seo.page_title(">>> Sub\n", "S", self.plain) == "Sub · S"

    def test_formatting_codes_are_removed_via_the_supplied_function(self):
        assert seo.page_title(">`F4afBold`f\n", "S", self.plain) == "Bold · S"

    def test_no_heading_gives_just_the_site_name(self):
        assert seo.page_title("just text\nmore", "My Site", self.plain) == "My Site"

    def test_empty_site_name_uses_a_default(self):
        assert seo.page_title("x", "", self.plain) == "NomadPortal"

    def test_heading_only_when_no_site_name(self):
        assert seo.page_title(">Hello\n", "", self.plain) == "Hello · NomadPortal"

    def test_description_strips_tags_and_collapses_whitespace(self):
        html = "<div><p>Hello   <b>world</b></p>\n<p>again</p></div>"
        assert seo.page_description(html) == "Hello world again"

    def test_description_is_truncated_at_a_word_boundary(self):
        d = seo.page_description("<p>" + "word " * 100 + "</p>", limit=40)
        assert len(d) <= 41 and d.endswith("…") and not d.endswith(" …")

    def test_description_unescapes_entities(self):
        assert seo.page_description("<p>a &amp; b</p>") == "a & b"

    def test_empty_description(self):
        assert seo.page_description("") == ""


class TestSitemapAndRobots:
    def test_sitemap_lists_absolute_escaped_urls(self):
        xml = seo.sitemap_xml("https://ex.org", ["/", "/page/a&b.mu"])
        assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert "<loc>https://ex.org/</loc>" in xml
        assert "<loc>https://ex.org/page/a&amp;b.mu</loc>" in xml

    def test_sitemap_is_deduplicated_and_capped(self):
        xml = seo.sitemap_xml("https://ex.org", ["/a", "/a"] + [f"/p{i}" for i in range(60000)])
        assert xml.count("<loc>") == seo.SITEMAP_MAX_URLS
        assert xml.count("<loc>https://ex.org/a</loc>") == 1

    def test_robots_blocks_backend_paths_and_points_at_the_sitemap(self):
        txt = seo.robots_txt("https://ex.org")
        assert "Disallow: /api/" in txt and "Disallow: /admin/" in txt and "Disallow: /auth/" in txt
        assert "Sitemap: https://ex.org/sitemap.xml" in txt
        assert "Disallow: /\n" not in txt                   # pages stay crawlable

"""Server-rendered pages (the version a crawler or a browser with JavaScript
off receives) show a page's images and its partials, not just its text.
Pure functions over the HTML the converter already produced."""

from nomadnet_web.ssr_inline import add_image_sources, inline_partials

NODE = "ab" * 16


def img(path="/media/logo.webp", node=NODE, alt="Logo"):
    return (f'<figure class="mu-image"><img class="mu-image-el" alt="{alt}" data-media-hash="{node}" '
            f'data-media-path="{path}" style="max-width:100%" loading="lazy" decoding="async">'
            f'<figcaption class="mu-image-alt">{alt}</figcaption></figure>')


def dyn(target="/page/part.mu", fields="pid=news", pid="news", node=NODE):
    href = f"/page?url=hash%3A%2F%2F{node}%2F" + target.lstrip("/").replace("/", "%2F")
    return f'<a href="{href}" class="mu-dynamic" data-fields="{fields}" data-pid="{pid}">[live]</a>'


class TestImages:
    def test_an_image_gets_a_real_address_to_load_from(self):
        out = add_image_sources(img())
        assert f'src="/api/media?hash={NODE}&amp;path=%2Fmedia%2Flogo.webp"' in out

    def test_the_rest_of_the_figure_is_untouched(self):
        out = add_image_sources(img())
        assert 'alt="Logo"' in out and "mu-image-alt" in out and 'data-media-path="/media/logo.webp"' in out

    def test_every_image_gets_one(self):
        out = add_image_sources(img("/media/a.webp") + img("/media/b.webp"))
        assert out.count(' src="/api/media?') == 2

    def test_a_path_with_odd_characters_is_encoded(self):
        out = add_image_sources(img("/media/a b&c.webp"))
        assert "path=%2Fmedia%2Fa%20b%26c.webp" in out

    def test_an_unavailable_figure_without_an_image_is_left_alone(self):
        html = '<figure class="mu-image mu-image-unavailable"><figcaption>Alt only</figcaption></figure>'
        assert add_image_sources(html) == html

    def test_an_image_that_already_has_a_source_is_not_changed(self):
        html = img().replace('<img ', '<img src="/x.png" ')
        assert add_image_sources(html).count(" src=") == 1

    def test_text_without_images_is_returned_as_is(self):
        assert add_image_sources("<p>hello</p>") == "<p>hello</p>"


class TestPartials:
    def load(self, html="<p>LIVE CONTENT</p>"):
        calls = []

        def f(node, path):
            calls.append((node, path))
            return html
        f.calls = calls
        return f

    def test_a_partial_is_replaced_by_its_content(self):
        f = self.load()
        out = inline_partials(f"<div>{dyn()}</div>", f)
        assert '<div class="mu-partial" data-pid="news"><p>LIVE CONTENT</p></div>' in out
        assert "mu-dynamic" not in out
        assert f.calls == [(NODE, "/page/part.mu")]

    def test_a_partial_that_cannot_be_loaded_stays_a_link(self):
        out = inline_partials(dyn(), lambda n, p: None)
        assert "mu-dynamic" in out and "mu-partial" not in out

    def test_a_partial_that_takes_form_fields_is_not_inlined(self):
        f = self.load()
        out = inline_partials(dyn(fields="pid=news|name|x=1"), f)
        assert "mu-dynamic" in out and f.calls == []

    def test_only_safe_ids_are_kept(self):
        out = inline_partials(dyn(pid='a"b'), self.load())
        assert 'data-pid="a&quot;b"' not in out and 'a"b' not in out

    def test_at_most_ten_partials_are_inlined(self):
        f = self.load()
        out = inline_partials("".join(dyn(pid=f"p{i}") for i in range(14)), f)
        assert out.count("mu-partial") == 10 and out.count("mu-dynamic") == 4 and len(f.calls) == 10

    def test_ordinary_links_are_untouched(self):
        html = '<a href="/page?url=hash%3A%2F%2F' + NODE + '%2Fpage%2Fabout.mu">About</a>'
        f = self.load()
        assert inline_partials(html, f) == html and f.calls == []

    def test_a_target_that_is_not_a_node_address_is_left_alone(self):
        html = '<a href="https://evil.example/x" class="mu-dynamic" data-fields="pid=a" data-pid="a">x</a>'
        f = self.load()
        assert inline_partials(html, f) == html and f.calls == []

    def test_a_loader_that_raises_leaves_the_link(self):
        def boom(n, p):
            raise RuntimeError("x")
        assert "mu-dynamic" in inline_partials(dyn(), boom)

"""NomadNet inline images: the ``(alt`w=..`h=..`a=..`url)`` tag.

Covers parsing (mirroring NomadNet's own parser), where an image line is
and is not an image (literal blocks, tables, escaped lines), the HTML
produced, and the safety of what ends up in attributes and styles.
"""

import re

import pytest
from micron2html import MicronConverter

from nomadnet_web import media

H = "ab" * 16
OTHER = "cd" * 16
conv = MicronConverter()


def render(text, can_load=lambda _h: True):
    return media.render_with_images(
        conv, text, node_hash=H, base_path="/page/index.mu",
        authenticated=False, can_load=can_load,
    )


class TestParseTag:
    def test_alt_properties_and_url(self):
        t = media.parse_image_tag("The RNS logo`w=n`a=c`:/media/demo.webp)")
        assert (t.alt, t.width, t.height, t.align, t.url) == (
            "The RNS logo", "n", None, "center", ":/media/demo.webp")

    def test_minimal_alt_and_url(self):
        t = media.parse_image_tag("Logo`:/media/a.webp)")
        assert (t.alt, t.url, t.width, t.align) == ("Logo", ":/media/a.webp", None, None)

    @pytest.mark.parametrize("code,word", [("l", "left"), ("c", "center"), ("r", "right")])
    def test_alignment_letters(self, code, word):
        assert media.parse_image_tag(f"x`a={code}`:/media/a.webp)").align == word

    def test_width_and_height(self):
        t = media.parse_image_tag("x`w=40`h=10`:/media/a.webp)")
        assert (t.width, t.height) == ("40", "10")

    def test_unknown_properties_and_bare_words_are_ignored(self):
        t = media.parse_image_tag("x`z=9`bare`w=5`:/media/a.webp)")
        assert t.width == "5"

    @pytest.mark.parametrize("bad", ["", "no closing", "x)", ")", "`)"])
    def test_malformed_is_none(self, bad):
        assert media.parse_image_tag(bad) is None

    def test_empty_url_is_none(self):
        assert media.parse_image_tag("x`w=n`)") is None

    def test_the_last_closing_bracket_ends_the_tag(self):
        t = media.parse_image_tag("a (b) c`:/media/x.webp)")
        assert t.alt == "a (b) c"


class TestFindImageLines:
    def test_an_image_line_becomes_a_token_line(self):
        text, tags = media.replace_image_lines("Hi\n`(x`:/media/a.webp)\nBye", "N")
        assert tags[0].url == ":/media/a.webp"
        lines = text.split("\n")
        assert lines[0] == "Hi" and lines[2] == "Bye" and "`(" not in lines[1]
        assert media.token("N", 0) == lines[1]

    def test_lines_in_a_literal_block_stay_literal(self):
        text, tags = media.replace_image_lines("`=\n`(x`:/media/a.webp)\n`=\n", "N")
        assert tags == [] and "`(x`:/media/a.webp)" in text

    def test_lines_inside_a_table_stay_in_the_table(self):
        text, tags = media.replace_image_lines("`t\n`(x`:/media/a.webp)\n`t\n`(y`:/media/b.webp)", "N")
        assert [t.alt for t in tags] == ["y"]

    def test_an_escaped_line_is_not_an_image(self):
        _text, tags = media.replace_image_lines("\\`(x`:/media/a.webp)", "N")
        assert tags == []

    def test_an_image_tag_not_at_the_line_start_is_not_an_image(self):
        _text, tags = media.replace_image_lines("see `(x`:/media/a.webp)", "N")
        assert tags == []

    def test_a_malformed_tag_is_left_alone(self):
        text, tags = media.replace_image_lines("`(oops", "N")
        assert tags == [] and text == "`(oops"

    def test_numbering_follows_document_order(self):
        _t, tags = media.replace_image_lines("`(a`:/media/1.webp)\n`(b`:/media/2.webp)", "N")
        assert [t.alt for t in tags] == ["a", "b"]


class TestMediaTarget:
    def t(self, url):
        return media.media_target(url, H, "/page/index.mu")

    def test_current_node_forms(self):
        assert self.t(":/media/a.webp") == (H, "/media/a.webp")
        assert self.t("/media/a.webp") == (H, "/media/a.webp")

    def test_other_node(self):
        assert self.t(f"{OTHER}:/media/a.webp") == (OTHER, "/media/a.webp")

    def test_subdirectories_are_fine(self):
        assert self.t(":/media/art/logo.webp") == (H, "/media/art/logo.webp")

    @pytest.mark.parametrize("bad", [
        ":/page/a.webp", ":/file/a.webp", "https://example.com/a.webp",
        ":/media/../secret.webp", ":/media/a/../../b.webp", "", ":/media/",
    ])
    def test_non_media_and_traversal_are_refused(self, bad):
        assert self.t(bad) is None

    def test_overlong_paths_are_refused(self):
        assert self.t(":/media/" + "a" * 600 + ".webp") is None


class TestRendering:
    def test_figure_with_the_image_target_in_data_attributes(self):
        html = render("`(The logo`w=n`a=c`:/media/demo.webp)")
        assert '<figure class="mu-image mu-image-center">' in html
        assert f'data-media-hash="{H}"' in html and 'data-media-path="/media/demo.webp"' in html
        assert 'alt="The logo"' in html and "<figcaption" in html

    def test_no_src_attribute_so_nothing_loads_without_the_script(self):
        assert " src=" not in render("`(x`:/media/a.webp)")

    def test_surrounding_text_and_alignment_state_are_kept(self):
        html = render("`c\nTop\n`(x`:/media/a.webp)\nBottom\n")
        assert "Top" in html and "Bottom" in html and html.index("Top") < html.index("<figure") < html.index("Bottom")

    def test_the_original_tag_text_never_leaks_into_the_output(self):
        html = render("`(x`w=n`:/media/a.webp)")
        assert "w=n" not in html.replace('style="', "")

    def test_a_literal_block_keeps_its_text(self):
        html = render("`=\n`(x`:/media/a.webp)\n`=\n")
        assert "<figure" not in html and "`(x`:/media/a.webp)" in html

    def test_image_that_the_viewer_may_not_load_shows_only_its_alt_text(self):
        html = render(f"`(Away`{OTHER}:/media/a.webp)", can_load=lambda h: h == H)
        assert "data-media-path" not in html and "Away" in html

    def test_unloadable_url_shows_alt_text(self):
        html = render("`(Nope`https://example.com/a.webp)")
        assert "<img" not in html and "Nope" in html

    def test_a_page_with_no_images_is_unchanged(self):
        text = "Hello\n`c\nthere\n"
        assert render(text) == conv.convert(text, node_hash=H, base_path="/page/index.mu", authenticated=False)

    def test_alt_text_is_escaped(self):
        from html.parser import HTMLParser

        class Tags(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags, self.attrs = [], []

            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)
                self.attrs.extend(name for name, _ in attrs)

        html = render('`(<script>alert(1)</script>" onerror="x`:/media/a.webp)')
        parsed = Tags()
        parsed.feed(html)
        assert set(parsed.tags) <= {"figure", "img", "figcaption"}      # no <script>
        assert "onerror" not in parsed.attrs                            # no injected attribute
        assert "&lt;script&gt;" in html

    def test_many_images(self):
        html = render("\n".join(f"`(i{n}`:/media/{n}.webp)" for n in range(5)))
        assert html.count("<figure") == 5

    def test_a_token_lookalike_in_page_text_is_not_replaced(self):
        # Tokens carry a per-render random nonce, so author text cannot forge one.
        html = render("@@IMG0-0@@\n`(x`:/media/a.webp)")
        assert "@@IMG0-0@@" in html and html.count("<figure") == 1


class TestSizing:
    def style(self, props):
        html = render(f"`(x`{props}`:/media/a.webp)")
        m = re.search(r'<img[^>]*style="([^"]*)"', html)
        return m.group(1) if m else ""

    def test_native_size_has_no_width(self):
        assert "width" not in self.style("w=n").replace("max-width", "")

    def test_percent_width(self):
        assert "width:50%" in self.style("w=50%")

    def test_cell_width_uses_character_units(self):
        assert "width:40ch" in self.style("w=40")

    def test_height_in_rows(self):
        assert re.search(r"height:\d+(\.\d+)?em", self.style("h=10"))

    def test_values_are_clamped(self):
        assert "width:100%" in self.style("w=999%")
        assert "width:200ch" in self.style("w=99999")

    def test_junk_sizes_are_ignored_and_cannot_inject_css(self):
        s = self.style("w=1;background:url(x)`h=red")
        assert "background" not in s and "url" not in s

    def test_images_never_overflow_their_container(self):
        assert "max-width:100%" in self.style("w=40")


class TestWebpAndLimits:
    def test_recognises_webp_data(self):
        assert media.is_webp(b"RIFF\x24\x00\x00\x00WEBPVP8 ....")

    @pytest.mark.parametrize("data", [b"", b"RIFF", b"<html>", b"\x89PNG\r\n\x1a\n", b"RIFF\x00\x00\x00\x00WAVEfmt "])
    def test_rejects_everything_else(self, data):
        assert not media.is_webp(data)

    def test_sniff_known_browser_image_types(self):
        assert media.sniff_image_type(b"\x89PNG\r\n\x1a\n....") == "image/png"
        assert media.sniff_image_type(b"\xff\xd8\xff\xe0....") == "image/jpeg"
        assert media.sniff_image_type(b"GIF89a....") == "image/gif"
        assert media.sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
        assert media.sniff_image_type(b"<svg onload=alert(1)>") is None

    def test_a_size_cap_exists(self):
        assert 0 < media.MAX_MEDIA_BYTES <= 8 * 1024 * 1024

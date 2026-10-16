"""``#!c=N`` — the NomadNet page cache directive. A page may start with
``#!c=<seconds>`` to say how long a fetched copy stays fresh; ``#!c=0``
means "never treat a copy as fresh"."""

import pytest

from nomadnet_web.cache_directive import MAX_SECONDS, parse_cache_seconds


class TestParse:
    def test_a_directive_on_the_first_line(self):
        assert parse_cache_seconds(b"#!c=3600\n>Title\nbody") == 3600

    def test_zero_means_never_fresh(self):
        assert parse_cache_seconds(b"#!c=0\n>x") == 0

    def test_accepts_text_as_well_as_bytes(self):
        assert parse_cache_seconds("#!c=60\n>x") == 60

    def test_no_directive_is_none(self):
        assert parse_cache_seconds(b">Title\nbody") is None
        assert parse_cache_seconds(b"") is None

    def test_found_among_other_header_directives(self):
        assert parse_cache_seconds(b"#!fg=ddd\n#!bg=222\n#!c=120\n>x") == 120

    def test_a_directive_far_down_the_page_is_not_a_header(self):
        body = ">x\n" * 50 + "#!c=60\n"
        assert parse_cache_seconds(body.encode()) is None

    def test_a_comment_line_mentioning_it_is_not_a_directive(self):
        assert parse_cache_seconds(b"# c=60 is how long\n>x") is None
        assert parse_cache_seconds(b"text #!c=60\n>x") is None

    def test_whitespace_and_crlf_are_tolerated(self):
        assert parse_cache_seconds(b"#!c=45  \r\n>x") == 45
        assert parse_cache_seconds(b"\xef\xbb\xbf#!c=45\n>x") == 45     # UTF-8 BOM

    @pytest.mark.parametrize("bad", [b"#!c=\n", b"#!c=abc\n", b"#!c=-5\n", b"#!c=1.5\n", b"#!c=1e3\n"])
    def test_malformed_values_are_ignored(self, bad):
        assert parse_cache_seconds(bad + b">x") is None

    def test_the_first_valid_directive_wins(self):
        assert parse_cache_seconds(b"#!c=10\n#!c=99\n>x") == 10

    def test_huge_values_are_capped(self):
        assert parse_cache_seconds(b"#!c=99999999999\n>x") == MAX_SECONDS

    def test_undecodable_bytes_do_not_raise(self):
        assert parse_cache_seconds(b"\xff\xfe#!c=5\n") in (None, 5)

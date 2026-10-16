"""Read-only access to a hosted site's static pages — what the sitemap
and the server-rendered pages are built from. Must never run a script
and never expose access-restricted or hidden files."""

import os
import stat
import sys

import pytest

from nomadnet_web.site_server import read_static_page, static_page_paths

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX exec bit")


@pytest.fixture
def pages(tmp_path):
    d = tmp_path / "pages"
    (d / "sub").mkdir(parents=True)
    (d / ".hidden").mkdir()
    (d / "index.mu").write_text(">Home\n")
    (d / "about.mu").write_text(">About\n")
    (d / "sub" / "deep.mu").write_text(">Deep\n")
    (d / ".secret.mu").write_text("nope")
    (d / ".hidden" / "x.mu").write_text("nope")
    (d / "private.mu").write_text("members only")
    (d / "private.mu.allowed").write_text("abcdef\n")
    return str(d)


class TestListing:
    def test_lists_static_pages_as_request_paths_sorted(self, pages):
        assert static_page_paths(pages) == [
            "/page/about.mu", "/page/index.mu", "/page/sub/deep.mu"]

    def test_listing_and_reading_agree_on_restricted_pages(self, pages):
        assert "/page/private.mu" not in static_page_paths(pages)
        assert read_static_page(pages, "/page/private.mu") is None

    @posix_only
    def test_executable_pages_are_excluded(self, pages):
        script = os.path.join(pages, "counter.mu")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\necho hi\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        assert "/page/counter.mu" not in static_page_paths(pages)

    def test_missing_directory_is_empty(self, tmp_path):
        assert static_page_paths(str(tmp_path / "nope")) == []


class TestReading:
    def test_reads_with_or_without_the_page_prefix(self, pages):
        assert read_static_page(pages, "/page/about.mu") == b">About\n"
        assert read_static_page(pages, "about.mu") == b">About\n"
        assert read_static_page(pages, "/page/sub/deep.mu") == b">Deep\n"

    def test_root_means_the_index(self, pages):
        assert read_static_page(pages, "/") == b">Home\n"
        assert read_static_page(pages, "/page/") == b">Home\n"

    def test_missing_page_is_none(self, pages):
        assert read_static_page(pages, "/page/none.mu") is None

    def test_path_traversal_is_refused(self, pages):
        assert read_static_page(pages, "/page/../pages.txt") is None
        assert read_static_page(pages, "/page/../../etc/passwd") is None

    def test_hidden_files_and_directories_are_refused(self, pages):
        assert read_static_page(pages, "/page/.secret.mu") is None
        assert read_static_page(pages, "/page/.hidden/x.mu") is None

    def test_access_restricted_pages_are_refused(self, pages):
        assert read_static_page(pages, "/page/private.mu") is None
        assert read_static_page(pages, "/page/private.mu.allowed") is None

    @posix_only
    def test_an_executable_page_is_never_run_or_read(self, pages):
        script = os.path.join(pages, "counter.mu")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\ntouch RAN\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        assert read_static_page(pages, "/page/counter.mu") is None
        assert not os.path.exists(os.path.join(pages, "RAN"))

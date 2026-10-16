"""``<page>.allowed`` — NomadNet's access list for a hosted page.

Rules (matching NomadNet, and failing closed):

- No ``.allowed`` file: anyone may fetch the page.
- A ``.allowed`` file: only an identified requester whose identity hash
  is listed (one 32-hex-character hash per line) may. Everyone else gets
  the "Request Not Allowed" page, not the content.
- If the file is executable, it is run and its output is the list.
- An unreadable file, a failing script, a timeout, or a list with no
  valid hashes lets nobody in.
"""

import os
import stat
import sys
from types import SimpleNamespace

import pytest

from nomadnet_web.site_server import (
    NOT_ALLOWED_PAGE, SiteServer, is_request_allowed, load_allowed_hashes,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX exec bit")

ALICE = "aa" * 16
BOB = "bb" * 16
EVE = "ee" * 16


def ident(hexhash):
    return SimpleNamespace(hash=bytes.fromhex(hexhash))


@pytest.fixture
def site(tmp_path):
    pages = tmp_path / "pages"
    files = tmp_path / "files"
    pages.mkdir()
    files.mkdir()
    s = object.__new__(SiteServer)          # skip __init__: no Reticulum here
    s._pages_dir = str(pages)
    s._files_dir = str(files)
    s._node_hash = "cd" * 16
    return s, pages, files


class TestLoad:
    def test_no_file_means_unrestricted(self, tmp_path):
        assert load_allowed_hashes(str(tmp_path / "none.allowed")) is None

    def test_lists_valid_hashes(self, tmp_path):
        f = tmp_path / "p.mu.allowed"
        f.write_text(f"{ALICE}\n{BOB}\n")
        assert load_allowed_hashes(str(f)) == {bytes.fromhex(ALICE), bytes.fromhex(BOB)}

    def test_case_whitespace_and_crlf_are_tolerated(self, tmp_path):
        f = tmp_path / "p.mu.allowed"
        f.write_bytes(f"  {ALICE.upper()}  \r\n\r\n{BOB}\r\n".encode())
        assert load_allowed_hashes(str(f)) == {bytes.fromhex(ALICE), bytes.fromhex(BOB)}

    def test_junk_lines_are_ignored(self, tmp_path):
        f = tmp_path / "p.mu.allowed"
        f.write_text(f"not a hash\n{ALICE}\nabc\n{'zz' * 16}\n")
        assert load_allowed_hashes(str(f)) == {bytes.fromhex(ALICE)}

    def test_an_empty_file_lets_nobody_in(self, tmp_path):
        f = tmp_path / "p.mu.allowed"
        f.write_text("")
        assert load_allowed_hashes(str(f)) == set()

    def test_a_file_of_only_junk_lets_nobody_in(self, tmp_path):
        f = tmp_path / "p.mu.allowed"
        f.write_text("everyone\n*\n")
        assert load_allowed_hashes(str(f)) == set()

    def test_a_directory_in_its_place_fails_closed(self, tmp_path):
        d = tmp_path / "p.mu.allowed"
        d.mkdir()
        assert load_allowed_hashes(str(d)) == set()

    @posix_only
    def test_an_executable_list_is_run_and_its_output_used(self, tmp_path):
        f = tmp_path / "p.mu.allowed"
        f.write_text(f"#!/bin/sh\necho {ALICE}\n")
        os.chmod(f, os.stat(f).st_mode | stat.S_IXUSR)
        assert load_allowed_hashes(str(f)) == {bytes.fromhex(ALICE)}

    @posix_only
    def test_a_failing_script_fails_closed(self, tmp_path):
        f = tmp_path / "p.mu.allowed"
        f.write_text(f"#!/bin/sh\necho {ALICE}\nexit 3\n")
        os.chmod(f, os.stat(f).st_mode | stat.S_IXUSR)
        assert load_allowed_hashes(str(f)) == set()

    @posix_only
    def test_a_hanging_script_times_out_and_fails_closed(self, tmp_path):
        f = tmp_path / "p.mu.allowed"
        f.write_text("#!/bin/sh\nsleep 5\n")
        os.chmod(f, os.stat(f).st_mode | stat.S_IXUSR)
        assert load_allowed_hashes(str(f), timeout=0.3) == set()


class TestIsRequestAllowed:
    def test_unrestricted_page(self, tmp_path):
        p = tmp_path / "p.mu"
        p.write_text(">x")
        assert is_request_allowed(str(p), None) is True
        assert is_request_allowed(str(p), bytes.fromhex(EVE)) is True

    def test_listed_identity_only(self, tmp_path):
        p = tmp_path / "p.mu"
        p.write_text(">x")
        (tmp_path / "p.mu.allowed").write_text(ALICE + "\n")
        assert is_request_allowed(str(p), bytes.fromhex(ALICE)) is True
        assert is_request_allowed(str(p), bytes.fromhex(EVE)) is False

    def test_unidentified_requester_is_refused(self, tmp_path):
        p = tmp_path / "p.mu"
        p.write_text(">x")
        (tmp_path / "p.mu.allowed").write_text(ALICE + "\n")
        assert is_request_allowed(str(p), None) is False


class TestServedOverReticulum:
    def make(self, pages, text=">Secret\n", allowed=None):
        (pages / "p.mu").write_text(text)
        if allowed is not None:
            (pages / "p.mu.allowed").write_text(allowed)
        return str(pages / "p.mu")

    def request(self, s, pages, remote):
        path = "/page/p.mu"
        # _serve_page maps "/page" onto the pages directory
        return s._serve_page(path, None, b"id", b"lid", remote, 0)

    def test_open_page_is_served(self, site):
        s, pages, _ = site
        self.make(pages)
        assert self.request(s, pages, None) == b">Secret\n"

    def test_listed_identity_gets_the_content(self, site):
        s, pages, _ = site
        self.make(pages, allowed=ALICE)
        assert self.request(s, pages, ident(ALICE)) == b">Secret\n"

    def test_other_identity_gets_not_allowed_and_not_the_content(self, site):
        s, pages, _ = site
        self.make(pages, allowed=ALICE)
        out = self.request(s, pages, ident(EVE))
        assert out == NOT_ALLOWED_PAGE.encode() and b"Secret" not in out

    def test_anonymous_requester_gets_not_allowed(self, site):
        s, pages, _ = site
        self.make(pages, allowed=ALICE)
        assert self.request(s, pages, None) == NOT_ALLOWED_PAGE.encode()

    @posix_only
    def test_an_executable_page_is_not_run_for_a_refused_requester(self, site):
        s, pages, _ = site
        script = pages / "p.mu"
        marker = pages / "RAN"
        script.write_text(f"#!/bin/sh\ntouch {marker}\necho hi\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        (pages / "p.mu.allowed").write_text(ALICE)
        out = self.request(s, pages, ident(EVE))
        assert out == NOT_ALLOWED_PAGE.encode() and not (pages / "RAN").exists()

    def test_file_downloads_are_covered_too(self, site):
        s, _pages, files = site
        (files / "doc.txt").write_text("private")
        (files / "doc.txt.allowed").write_text(ALICE)
        refused = s._serve_file("/file/doc.txt", None, b"id", b"lid", ident(EVE), 0)
        assert refused == NOT_ALLOWED_PAGE.encode()
        granted = s._serve_file("/file/doc.txt", None, b"id", b"lid", ident(ALICE), 0)
        assert granted[0].read() == b"private"
        granted[0].close()

    def test_unrestricted_file_is_unchanged(self, site):
        s, _pages, files = site
        (files / "doc.txt").write_text("public")
        out = s._serve_file("/file/doc.txt", None, b"id", b"lid", None, 0)
        assert out[0].read() == b"public" and out[1] == {"name": b"doc.txt"}
        out[0].close()


class TestServedToTheWebUi:
    """The portal's own web visitors reach hosted pages through
    ``fetch_page``; the signed-in user's identity (when they have enabled
    it for this site) is what the list is checked against."""

    def test_guest_is_refused(self, site):
        s, pages, _ = site
        (pages / "p.mu").write_text(">Secret\n")
        (pages / "p.mu.allowed").write_text(ALICE)
        assert s.fetch_page("/page/p.mu") == (NOT_ALLOWED_PAGE.encode(), None)

    def test_listed_user_is_served(self, site):
        s, pages, _ = site
        (pages / "p.mu").write_text(">Secret\n")
        (pages / "p.mu.allowed").write_text(ALICE)
        assert s.fetch_page("/page/p.mu", local_identity_hex=ALICE) == (b">Secret\n", None)

    def test_unlisted_user_is_refused(self, site):
        s, pages, _ = site
        (pages / "p.mu").write_text(">Secret\n")
        (pages / "p.mu.allowed").write_text(ALICE)
        assert s.fetch_page("/page/p.mu", local_identity_hex=EVE)[0] == NOT_ALLOWED_PAGE.encode()

    def test_a_malformed_identity_is_treated_as_anonymous(self, site):
        s, pages, _ = site
        (pages / "p.mu").write_text(">Secret\n")
        (pages / "p.mu.allowed").write_text(ALICE)
        assert s.fetch_page("/page/p.mu", local_identity_hex="not-hex")[0] == NOT_ALLOWED_PAGE.encode()

    def test_unrestricted_pages_are_unchanged(self, site):
        s, pages, _ = site
        (pages / "open.mu").write_text(">Hi\n")
        assert s.fetch_page("/page/open.mu") == (b">Hi\n", None)

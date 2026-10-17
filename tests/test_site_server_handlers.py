"""What the hosted site hands out: the request handlers, the directory scan
and the environment given to executable pages. Called directly, the way
Reticulum calls them, so no network is needed."""

import os
import stat
import sys
from types import SimpleNamespace

import pytest

from nomadnet_web.site_server import SiteServer, _build_env

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX exec bit")


@pytest.fixture
def site(tmp_path):
    pages, files = tmp_path / "pages", tmp_path / "files"
    pages.mkdir(); files.mkdir()
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    (pages / "index.mu").write_text(">Home\n")
    (pages / "sub").mkdir()
    (pages / "sub" / "a.mu").write_text(">A\n")
    (pages / ".hidden.mu").write_text("hidden")
    (pages / "locked.mu").write_text("members only")
    (pages / "locked.mu.allowed").write_text("aa" * 16 + "\n")
    (files / "doc.txt").write_text("a document")
    s = SiteServer(str(pages), str(files), str(tmp_path / "identity"))
    return s, tmp_path


def page(s, path, ident=None):
    return s._serve_page(path, None, b"rid", b"lid", ident, 0)


def file(s, path, ident=None):
    return s._serve_file(path, None, b"rid", b"lid", ident, 0)


class TestScan:
    def test_lists_files_below_the_directory_but_not_hidden_or_access_lists(self, site):
        s, root = site
        found = []
        s._scan_dir(str(root / "pages"), found)
        names = sorted(os.path.relpath(f, root / "pages") for f in found)
        assert names == ["index.mu", "locked.mu", os.path.join("sub", "a.mu")]

    def test_a_missing_directory_is_just_empty(self, site):
        s, root = site
        found = []
        s._scan_dir(str(root / "nope"), found)
        assert found == []


class TestPages:
    def test_a_static_page_is_returned(self, site):
        s, _ = site
        assert page(s, "/page/index.mu") == b">Home\n"
        assert page(s, "/page/sub/a.mu") == b">A\n"

    def test_a_missing_page_says_so(self, site):
        s, _ = site
        assert b"Page Not Found" in page(s, "/page/missing.mu")

    def test_an_access_list_is_enforced(self, site):
        s, _ = site
        assert b"members only" not in page(s, "/page/locked.mu")
        assert page(s, "/page/locked.mu", SimpleNamespace(hash=bytes.fromhex("aa" * 16))) == b"members only"

    @pytest.mark.parametrize("path", ["/page/../secret.txt", "/page/sub/../../secret.txt",
                                      "/page//../secret.txt"])
    def test_a_path_that_climbs_out_of_the_pages_directory_gets_nothing(self, site, path):
        s, _ = site
        out = page(s, path)
        assert out is None or b"TOP SECRET" not in out

    @posix_only
    def test_an_executable_page_runs_and_gets_only_a_small_environment(self, site, monkeypatch):
        s, root = site
        monkeypatch.setenv("OIDC_CLIENT_SECRET", "hunter2")
        p = root / "pages" / "run.mu"
        p.write_text("#!/bin/sh\nprintf '>Env\\n'; env | sort\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        out = page(s, "/page/run.mu", SimpleNamespace(hash=b"\x01" * 16)).decode()
        assert out.startswith(">Env")
        assert "remote_identity=" + "01" * 16 in out
        assert "hunter2" not in out and "OIDC_CLIENT_SECRET" not in out


class TestFiles:
    def test_a_file_is_returned_with_its_name(self, site):
        s, _ = site
        handle, meta = file(s, "/file/doc.txt")
        try:
            assert handle.read() == b"a document" and meta == {"name": b"doc.txt"}
        finally:
            handle.close()

    def test_a_missing_file_returns_nothing(self, site):
        s, _ = site
        assert file(s, "/file/missing.txt") is None

    @pytest.mark.parametrize("path", ["/file/../secret.txt", "/file/sub/../../secret.txt"])
    def test_a_path_that_climbs_out_of_the_files_directory_gets_nothing(self, site, path):
        s, _ = site
        out = file(s, path)
        if out is not None:
            out[0].close()
        assert out is None


class TestEnvironment:
    def test_only_path_and_pythonpath_are_inherited(self, monkeypatch):
        monkeypatch.setenv("PATH", "/bin")
        monkeypatch.setenv("PYTHONPATH", "/site/lib")
        monkeypatch.setenv("ADMIN_PASSWORD", "x")
        env = _build_env(None, None, None)
        assert env == {"PATH": "/bin", "PYTHONPATH": "/site/lib"}

    def test_identity_node_and_link_are_provided(self):
        env = _build_env(b"\x0a" * 16, "ab" * 16, None, node_destination="cd" * 16)
        assert env["remote_identity"] == "ab" * 16 and env["node_destination"] == "cd" * 16
        assert env["link_id"] == "0a" * 16

    def test_form_fields_arrive_under_both_names(self):
        env = _build_env(None, None, {"field_name": "Ann", "var_x": "1", "other": "no", 5: "int-key"})
        assert env["field_name"] == "Ann" and env["var_name"] == "Ann"
        assert env["var_x"] == "1"
        assert "other" not in env and 5 not in env

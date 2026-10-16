"""Hosted images: which file a ``/media/..`` request may read, and the
``/media`` request handler that serves NomadNet clients.

Images live in the pages directory (``/media/logo.webp`` is
``pages/logo.webp``), exactly as in NomadNet.
"""

import os
import sys
from types import SimpleNamespace

import pytest

from nomadnet_web.site_server import SiteServer, resolve_media_file

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")

ALICE = "aa" * 16
EVE = "ee" * 16
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
EXTS = (".webp",)
WEB_EXTS = (".webp", ".png", ".jpg", ".jpeg", ".gif")


@pytest.fixture
def pages(tmp_path):
    d = tmp_path / "pages"
    (d / "art").mkdir(parents=True)
    (d / "logo.webp").write_bytes(WEBP)
    (d / "art" / "deep.webp").write_bytes(WEBP)
    (d / "photo.png").write_bytes(PNG)
    (d / "notes.txt").write_text("not an image")
    (d / "secret.webp").write_bytes(WEBP)
    (d / "secret.webp.allowed").write_text(ALICE + "\n")
    (d / ".hidden.webp").write_bytes(WEBP)
    return str(d)


def ident(h):
    return bytes.fromhex(h)


class TestResolve:
    def test_finds_an_image_in_the_pages_directory(self, pages):
        assert resolve_media_file(pages, "/media/logo.webp", None, EXTS) == os.path.join(pages, "logo.webp")

    def test_subdirectories(self, pages):
        assert resolve_media_file(pages, "/media/art/deep.webp", None, EXTS).endswith("art/deep.webp")

    def test_extension_whitelist_depends_on_the_caller(self, pages):
        assert resolve_media_file(pages, "/media/photo.png", None, EXTS) is None            # mesh: WebP only
        assert resolve_media_file(pages, "/media/photo.png", None, WEB_EXTS) is not None    # web: browsers draw PNG

    def test_non_images_are_refused(self, pages):
        assert resolve_media_file(pages, "/media/notes.txt", None, WEB_EXTS) is None

    def test_missing_file(self, pages):
        assert resolve_media_file(pages, "/media/nope.webp", None, EXTS) is None

    @pytest.mark.parametrize("bad", [
        "/media/../outside.webp", "/media/art/../../outside.webp", "/media//etc/passwd.webp",
        "/page/logo.webp", "logo.webp", "/media/", "/media", "", "/media/" + "a" * 600 + ".webp",
    ])
    def test_traversal_and_malformed_paths_are_refused(self, pages, bad):
        assert resolve_media_file(pages, bad, None, EXTS) is None

    def test_hidden_files_are_refused(self, pages):
        assert resolve_media_file(pages, "/media/.hidden.webp", None, EXTS) is None

    @posix_only
    def test_a_symlink_out_of_the_directory_is_refused(self, pages, tmp_path):
        outside = tmp_path / "outside.webp"
        outside.write_bytes(WEBP)
        os.symlink(outside, os.path.join(pages, "link.webp"))
        assert resolve_media_file(pages, "/media/link.webp", None, EXTS) is None

    def test_access_lists_apply_to_images(self, pages):
        assert resolve_media_file(pages, "/media/secret.webp", ident(ALICE), EXTS) is not None
        assert resolve_media_file(pages, "/media/secret.webp", ident(EVE), EXTS) is None
        assert resolve_media_file(pages, "/media/secret.webp", None, EXTS) is None

    def test_an_access_list_is_never_an_image(self, pages):
        assert resolve_media_file(pages, "/media/secret.webp.allowed", ident(ALICE), EXTS) is None


class TestMediaHandler:
    @pytest.fixture
    def site(self, pages):
        s = object.__new__(SiteServer)
        s._pages_dir = pages
        s._files_dir = pages
        s._node_hash = "cd" * 16
        return s

    def request(self, site, path, remote=None, data="default"):
        payload = {"path": path, "key": None} if data == "default" else data
        return site._serve_media("/media", payload, b"id", b"lid", remote, 0)

    def test_serves_a_webp_as_a_file_response(self, site, pages):
        out = self.request(site, "/media/logo.webp")
        try:
            assert out[0].read() == WEBP and out[1] == {"name": b"logo.webp"}
        finally:
            out[0].close()

    def test_denial_and_absence_are_false_like_nomadnet(self, site):
        assert self.request(site, "/media/nope.webp") is False
        assert self.request(site, "/media/photo.png") is False       # not WebP on the wire

    def test_an_access_list_is_honoured(self, site):
        assert self.request(site, "/media/secret.webp", SimpleNamespace(hash=ident(EVE))) is False
        assert self.request(site, "/media/secret.webp", None) is False
        out = self.request(site, "/media/secret.webp", SimpleNamespace(hash=ident(ALICE)))
        assert out[0].read() == WEBP
        out[0].close()

    @pytest.mark.parametrize("data", [None, "x", {}, {"key": None}, {"path": 5}])
    def test_malformed_requests_get_none(self, site, data):
        assert self.request(site, None, data=data) is None

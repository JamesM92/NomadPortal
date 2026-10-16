"""What the portal asks a node for: pages and files by their own path,
inline images through NomadNet's ``/media`` request."""

from nomadnet_web.browser import rns_request_for


class TestPagesAndFiles:
    def test_page_path_is_used_as_is(self):
        assert rns_request_for("/page/a.mu") == ("/page/a.mu", None)

    def test_file_path_is_used_as_is(self):
        assert rns_request_for("/file/x.zip") == ("/file/x.zip", None)

    def test_a_bare_path_becomes_a_page(self):
        assert rns_request_for("/about.mu") == ("/page/about.mu", None)
        assert rns_request_for("/") == ("/page/index.mu", None)
        assert rns_request_for("") == ("/page/index.mu", None)

    def test_trailing_slash_is_ignored(self):
        assert rns_request_for("/page/sub/") == ("/page/sub", None)

    def test_form_fields_get_the_field_prefix(self):
        _p, data = rns_request_for("/page/a.mu", {"name": "x", "field_other": "y", "var_k": "z"})
        assert data == {"field_name": "x", "field_other": "y", "var_k": "z"}

    def test_empty_field_data_is_no_data(self):
        assert rns_request_for("/page/a.mu", {}) == ("/page/a.mu", None)


class TestMedia:
    def test_image_requests_use_the_media_path_and_put_the_file_in_the_data(self):
        assert rns_request_for("/media/logo.webp", None, "/media/logo.webp") == (
            "/media", {"path": "/media/logo.webp", "key": None})

    def test_the_key_is_present_because_nomadnets_server_requires_it(self):
        _p, data = rns_request_for("/", None, "/media/x.webp")
        assert "key" in data and data["key"] is None

    def test_media_ignores_form_fields(self):
        _p, data = rns_request_for("/", {"a": "b"}, "/media/x.webp")
        assert data == {"path": "/media/x.webp", "key": None}

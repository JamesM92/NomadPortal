"""Tests for ``_render_appearance_svg``'s real-icon rendering.

Motivation: this used to always draw just the icon name's first letter
as text — a real webfont would've been needed for anything richer. Real
MeshChat/Sideband contacts routinely send genuine Material Design Icons
(MDI) names (``"hiking"``, ``"account-supervisor"``, ...), so rendering
the actual icon shape (via mdi_icons.py) is now the common case; the
first-letter text is only the fallback for a name the catalog doesn't
recognize.
"""

import base64

from nomadnet_web.messaging import _render_appearance_svg

# Confirmed present in the real bundled static/data/mdi_icons.json —
# see tests/test_mdi_icons.py's own note on why this matters.
KNOWN_ICON = "hiking"


def _decode(icon_b64: str) -> str:
    return base64.b64decode(icon_b64).decode("utf-8")


def test_recognized_name_renders_a_real_path():
    icon_b64, mime = _render_appearance_svg(KNOWN_ICON, b"\xff\xff\xff", b"\x5b\xa3\xc9")
    assert mime == "image/svg+xml"
    svg = _decode(icon_b64)
    assert "<path" in svg
    assert "<text" not in svg  # real icon found — must not also draw a letter


def test_unrecognized_name_falls_back_to_first_letter():
    icon_b64, mime = _render_appearance_svg("not-a-real-mdi-icon-xyz", b"\xff\xff\xff", b"\x5b\xa3\xc9")
    svg = _decode(icon_b64)
    assert "<path" not in svg
    assert "<text" in svg
    assert ">N<" in svg  # first letter of "not-a-real..." uppercased


def test_non_string_name_falls_back_to_letter_glyph():
    # Some senders (or malformed field data) could hand this a non-string
    # — must degrade to the "?" placeholder, not raise.
    icon_b64, _ = _render_appearance_svg(None, b"\xff\xff\xff", b"\x5b\xa3\xc9")
    svg = _decode(icon_b64)
    assert ">?<" in svg


def test_recognized_name_uses_the_given_foreground_color():
    icon_b64, _ = _render_appearance_svg(KNOWN_ICON, b"\x11\x22\x33", b"\x00\x00\x00")
    svg = _decode(icon_b64)
    assert 'fill="#112233"' in svg


def test_recognized_name_uses_the_given_background_color():
    icon_b64, _ = _render_appearance_svg(KNOWN_ICON, b"\xff\xff\xff", b"\xaa\xbb\xcc")
    svg = _decode(icon_b64)
    assert '<circle cx="16" cy="16" r="16" fill="#aabbcc"/>' in svg


def test_sideband_shape_colors_still_work_with_real_icon_rendering():
    # Regression check: the interop fix for Sideband's [r,g,b] float
    # color shape (test_appearance_color.py) must keep working now that
    # icon rendering itself has a second code path (real icon vs letter).
    icon_b64, _ = _render_appearance_svg(KNOWN_ICON, [1.0, 0.0, 0.0], [0.0, 0.0, 1.0])
    svg = _decode(icon_b64)
    assert 'fill="#ff0000"' in svg
    assert '<circle cx="16" cy="16" r="16" fill="#0000ff"/>' in svg


def test_uppercase_name_still_resolves_to_the_real_icon():
    # Underscore/space/hyphen normalization itself is covered thoroughly
    # against a hyphenated name in test_mdi_icons.py — this just confirms
    # that normalization is actually wired up on this call path too.
    svg = _decode(_render_appearance_svg(KNOWN_ICON.upper(), b"\xff\xff\xff", b"\x5b\xa3\xc9")[0])
    assert "<path" in svg

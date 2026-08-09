"""Tests for ``_appearance_color_to_hex`` — the LXMF FIELD_ICON_APPEARANCE
color-shape adapter.

Both shapes seen in the wild must produce a real hex string; only
truly-unknown inputs should fall back to grey.

Motivation for the range of cases below: the historical bug was
that Sideband-users' colors always rendered as ``#888888`` because
the converter only accepted MeshChat's ``bytes(3)`` shape and
treated Sideband's ``[r, g, b]`` float sequence as unknown.
Ported from the NomadPortal-Android sister project's
``python-core``, which hit and fixed this same interop failure.
"""

import pytest

from nomadnet_web.messaging import (
    _appearance_color_to_hex,
    _channel_to_255,
)


class TestMeshChatBytesShape:
    """MeshChat + this app: raw 3-byte ``bytes`` object."""

    def test_bytes_triplet(self):
        assert _appearance_color_to_hex(b"\xff\x00\x00") == "#ff0000"

    def test_bytes_all_zero(self):
        assert _appearance_color_to_hex(b"\x00\x00\x00") == "#000000"

    def test_bytes_all_max(self):
        assert _appearance_color_to_hex(b"\xff\xff\xff") == "#ffffff"

    def test_bytearray_same_as_bytes(self):
        assert _appearance_color_to_hex(bytearray([16, 32, 48])) == "#102030"

    def test_bytes_longer_than_three_ignores_alpha(self):
        # A 4-byte RGBA payload — the alpha byte is discarded, RGB
        # still resolves. Matches the historical converter's
        # behaviour so nothing regresses for any client that
        # happens to send four bytes.
        assert _appearance_color_to_hex(b"\xff\x00\x00\x80") == "#ff0000"


class TestSidebandFloatShape:
    """Sideband: ``[r, g, b]`` or ``[r, g, b, a]`` 0-1 floats.
    DEFAULT_APPEARANCE = ["account", [0,0,0,1], [1,1,1,1]].
    """

    def test_floats_red(self):
        assert _appearance_color_to_hex([1.0, 0.0, 0.0]) == "#ff0000"

    def test_floats_black(self):
        assert _appearance_color_to_hex([0.0, 0.0, 0.0]) == "#000000"

    def test_floats_white(self):
        assert _appearance_color_to_hex([1.0, 1.0, 1.0]) == "#ffffff"

    def test_floats_with_alpha_ignored(self):
        # Sideband's DEFAULT_APPEARANCE carries an alpha channel; the
        # alpha is ignored (SVG rendering doesn't use it) but the
        # first three channels still resolve correctly.
        assert _appearance_color_to_hex([0.0, 0.0, 0.0, 1.0]) == "#000000"

    def test_floats_midpoint(self):
        # 0.5 → round(0.5 * 255) = 128 (0x80)
        assert _appearance_color_to_hex([0.5, 0.5, 0.5]) == "#808080"

    def test_int_sequence_treated_as_channels(self):
        # A tuple/list of ints (not floats) — accepted as the same
        # kind of sequence; ints already in 0-255 pass through.
        assert _appearance_color_to_hex([255, 128, 0]) == "#ff8000"

    def test_tuple_shape_works_same_as_list(self):
        assert _appearance_color_to_hex((1.0, 0.5, 0.0)) == "#ff8000"


class TestUnknownShapeFallsBackToGrey:
    """Anything we don't recognise renders as grey, so a badly-formed
    icon field doesn't crash the receive path.
    """

    @pytest.mark.parametrize("value", [
        None,
        "",
        "#ff0000",       # string — not a valid shape (color is packed as bytes/list)
        [1.0],           # too short
        b"",             # empty bytes
        b"\xff\x00",     # bytes of length < 3
        {"r": 1, "g": 0, "b": 0},  # dict — not a shape
        42,              # int scalar
    ])
    def test_grey_fallback(self, value):
        assert _appearance_color_to_hex(value) == "#888888"


class TestChannelClamping:
    """``_channel_to_255`` enforces the 0-255 range so a
    bad-actor / miscalibrated peer sending out-of-range values
    can't produce out-of-hex-range output.
    """

    def test_float_out_of_range_high(self):
        assert _channel_to_255(2.5) == 255

    def test_float_out_of_range_low(self):
        assert _channel_to_255(-0.5) == 0

    def test_int_out_of_range_high(self):
        assert _channel_to_255(999) == 255

    def test_int_out_of_range_low(self):
        assert _channel_to_255(-100) == 0

    def test_bool_true_is_max(self):
        # bool is a subclass of int in Python; check we handle it
        # before the int branch so True doesn't become 1 (which
        # would then not be scaled — Sideband could reasonably
        # send True as an "on" alpha).
        assert _channel_to_255(True) == 255

    def test_bool_false_is_zero(self):
        assert _channel_to_255(False) == 0

    def test_none_falls_back_to_mid_grey(self):
        assert _channel_to_255(None) == 128

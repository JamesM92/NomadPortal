"""Tests for identity_qr.py — the "show my QR" identity-sharing payload
and SVG rendering.

build_identity_qr_payload's format (lxma://<hash>:<pubkey>) is a fixed
external contract (Columba's own real scheme, adopted verbatim so a
code generated here scans correctly in Columba/NomadPortal-Android) —
these tests exist specifically to catch an accidental format drift
(wrong separator, wrong prefix, swapped fields) that would silently
break cross-app scanning without ever raising an exception.
"""

from nomadnet_web.identity_qr import build_identity_qr_payload, render_qr_svg

HASH_HEX = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
PUBKEY_HEX = "deadbeef" * 8  # 32 bytes, roughly real pubkey length


def test_payload_format_matches_the_lxma_scheme_exactly():
    payload = build_identity_qr_payload(HASH_HEX, PUBKEY_HEX)
    assert payload == f"lxma://{HASH_HEX}:{PUBKEY_HEX}"


def test_payload_has_exactly_one_colon_separator():
    payload = build_identity_qr_payload(HASH_HEX, PUBKEY_HEX)
    body = payload.removeprefix("lxma://")
    assert body.count(":") == 1


def test_payload_round_trips_through_the_android_side_parser_shape():
    # Mirrors NomadPortal-Android's parseIdentityQrPayload() logic
    # directly (lxma:// prefix, split on ":", exactly 2 parts) rather
    # than importing Kotlin — this is the real contract those apps
    # depend on this payload satisfying.
    payload = build_identity_qr_payload(HASH_HEX, PUBKEY_HEX)
    assert payload.startswith("lxma://")
    parts = payload.removeprefix("lxma://").split(":")
    assert len(parts) == 2
    assert parts[0] == HASH_HEX
    assert parts[1] == PUBKEY_HEX


def test_render_qr_svg_returns_real_svg_bytes():
    svg = render_qr_svg(build_identity_qr_payload(HASH_HEX, PUBKEY_HEX))
    assert isinstance(svg, bytes)
    assert svg.startswith(b"<?xml")
    assert b"<svg" in svg
    assert b"<path" in svg


def test_render_qr_svg_handles_a_short_payload_without_crashing():
    # Degenerate but real input shape — an identity with no announced
    # address yet shouldn't be reachable here (the route itself guards
    # on that), but the renderer itself should stay robust to whatever
    # string it's handed.
    svg = render_qr_svg("lxma://:")
    assert isinstance(svg, bytes)
    assert len(svg) > 0

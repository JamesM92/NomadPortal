"""Tests for mdi_icons.py's MDI catalog lookup.

Uses the real bundled static/data/mdi_icons.json — not a stub — since
this module's whole job is loading and normalizing against that actual
file; a fake catalog wouldn't exercise anything the real one couldn't
also verify, and would risk drifting from the real data's shape over
time (e.g. if it were ever regenerated with a different SVG path
character set).
"""

import re

from nomadnet_web import mdi_icons

_SVG_PATH_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9.,\-\s]+$")

# A name confirmed present in the real bundled catalog at the time this
# was written — deliberately hyphenated (multi-word) so the normalization
# tests below actually exercise something: a name with no hyphen at all
# would make replace("-", " ")/replace("-", "_") a no-op, silently
# turning those tests into "hiking" == "hiking" and passing even if
# normalization were completely broken. If a future regeneration of
# mdi_icons.json ever drops this name, these tests should fail loudly
# (not silently pass against nothing) — that's the point of testing
# against real data.
KNOWN_ICON = "ab-testing"


def test_known_icon_resolves_to_valid_path_data():
    path = mdi_icons.get_path(KNOWN_ICON)
    assert path is not None
    assert _SVG_PATH_RE.match(path)


def test_unknown_name_returns_none():
    assert mdi_icons.get_path("this-name-does-not-exist-in-mdi-xyz123") is None


def test_normalizes_spaces_to_hyphens():
    space_form = KNOWN_ICON.replace("-", " ")
    assert mdi_icons.get_path(space_form) == mdi_icons.get_path(KNOWN_ICON)


def test_normalizes_underscores_to_hyphens():
    underscore_form = KNOWN_ICON.replace("-", "_")
    assert mdi_icons.get_path(underscore_form) == mdi_icons.get_path(KNOWN_ICON)


def test_case_insensitive():
    assert mdi_icons.get_path(KNOWN_ICON.upper()) == mdi_icons.get_path(KNOWN_ICON)


def test_empty_and_none_input_return_none():
    assert mdi_icons.get_path("") is None
    assert mdi_icons.get_path(None) is None


def test_is_loaded_true_after_a_lookup():
    mdi_icons.get_path(KNOWN_ICON)
    assert mdi_icons.is_loaded() is True


def test_every_entry_in_the_real_catalog_passes_the_path_allowlist():
    # Guards against a future regeneration of mdi_icons.json introducing
    # path data get_path()'s own allowlist would reject (e.g. a
    # generator change that starts emitting scientific notation or a
    # different separator) — would silently turn every icon in that
    # batch into "not found" otherwise, degrading real icons to letter
    # glyphs with no obvious cause.
    mdi_icons._load()
    bad = [name for name, path in mdi_icons._paths.items() if not _SVG_PATH_RE.match(path)]
    assert bad == [], f"{len(bad)} entries fail the SVG path allowlist, e.g. {bad[:5]}"

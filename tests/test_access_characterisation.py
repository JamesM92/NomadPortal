"""Characterisation of the access rules: who may browse / submit forms on
which node.

The expected table below is written from the documented rules, not
copied from the implementation, and the exhaustive sweep runs every
role x lock x node x action combination. It currently drives the
original ``routes._can_browse`` / ``routes._can_interact``; when the
policy moves into its own module the single ``decide`` function is the
only thing that changes, so the same sweep proves behaviour is
unchanged.

Rules (current behaviour, including one deliberate asymmetry):

- An empty node hash is always denied.
- super_admin is always allowed.
- admin / user: allowed everywhere, unless their own ``*_default_lock``
  is on, then only the hosted site and the configured default node.
- guest, browse: same shape, using ``guests_default_lock``.
- guest, interact: ALWAYS confined to the hosted / default nodes,
  whatever the lock says. (Guests may browse anywhere when the lock is
  off, but can only submit forms on the operator's own nodes.)
"""

import itertools
from types import SimpleNamespace
from unittest import mock

import pytest
from flask import Flask

from nomadnet_web import routes

HOSTED = "aa" * 16
DEFAULT = "bb" * 16
OTHER = "cc" * 16

ROLES = ("super_admin", "admin", "user", "guest")
NODES = {
    "hosted":       HOSTED,
    "default":      DEFAULT,
    "other":        OTHER,
    "hosted_upper": HOSTED.upper(),
    "empty":        "",
}
ACTIONS = ("browse", "interact")


def _viewer(role):
    return SimpleNamespace(
        super_admin=(role == "super_admin"),
        is_admin=(role in ("super_admin", "admin")),
        is_authenticated=(role != "guest"),
    )


def decide(role, lock, node, action):
    """Ask the implementation under test. ``lock`` is the viewer's own
    ``*_default_lock`` value; the other roles' locks are set opposite so
    a role reading the wrong flag fails the sweep."""
    app = Flask(__name__)
    settings = {
        "guests_default_lock": lock if role == "guest" else not lock,
        "users_default_lock":  lock if role == "user" else not lock,
        "admins_default_lock": lock if role == "admin" else not lock,
        "default_node":        DEFAULT,
    }
    site = mock.Mock()
    site.node_hash.return_value = HOSTED
    ui = mock.Mock()
    ui.get_all.return_value = settings
    app.config.update(SITE_SERVER=site, UI_SETTINGS=ui)
    fn = routes._can_browse if action == "browse" else routes._can_interact
    with app.test_request_context(), \
            mock.patch.object(routes, "current_user", _viewer(role)):
        return fn(node)


def expected(role, lock, node, action):
    if not node:
        return False
    if role == "super_admin":
        return True
    confined = node.lower() in (HOSTED, DEFAULT)
    if role == "guest" and action == "interact":
        return confined
    return confined if lock else True


@pytest.mark.parametrize(
    "role,lock,node_name,action",
    list(itertools.product(ROLES, (False, True), NODES, ACTIONS)),
)
def test_matrix(role, lock, node_name, action):
    node = NODES[node_name]
    assert decide(role, lock, node, action) is expected(role, lock, node, action)


class TestNamedInvariants:
    """The same facts as the sweep, spelled out so a failure reads as a
    sentence and the intent survives a refactor."""

    def test_unlocked_guest_may_browse_anywhere(self):
        assert decide("guest", False, OTHER, "browse") is True

    def test_unlocked_guest_may_not_interact_off_the_operators_nodes(self):
        assert decide("guest", False, OTHER, "interact") is False

    def test_unlocked_guest_may_interact_on_the_hosted_site(self):
        assert decide("guest", False, HOSTED, "interact") is True

    def test_locked_guest_is_confined_for_both_actions(self):
        assert decide("guest", True, OTHER, "browse") is False
        assert decide("guest", True, OTHER, "interact") is False
        assert decide("guest", True, DEFAULT, "browse") is True

    def test_hash_comparison_ignores_case(self):
        assert decide("guest", True, HOSTED.upper(), "browse") is True

    def test_super_admin_ignores_every_lock(self):
        assert decide("super_admin", True, OTHER, "interact") is True

    def test_empty_hash_is_always_denied(self):
        for role in ROLES:
            assert decide(role, False, "", "browse") is False

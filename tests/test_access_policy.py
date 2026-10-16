"""Tests for ``nomadnet_web.access`` — the extracted access policy.

The rule sweep reuses the independently written expected table from
``test_access_characterisation`` so the new module is held to exactly
the behaviour the old route helpers had. The rest covers what the new
module adds: denial responses, blocklist ordering, settings migration,
the prescribed-node list, link filtering and the client snapshot.
"""

import itertools
from types import SimpleNamespace

import pytest

from nomadnet_web.access import (
    BLOCKED_MESSAGE, BROWSE, DOWNLOAD, INTERACT, AccessPolicy, PolicyConfig,
    policy_for, role_of,
)
from .test_access_characterisation import (
    ACTIONS, DEFAULT, HOSTED, NODES, OTHER, ROLES, expected,
)


def make(role, lock=False, prescribed=(DEFAULT,), blocked=()):
    cfg = PolicyConfig(
        prescribed=tuple(prescribed),
        hosted=HOSTED,
        locks={"guest": lock, "user": lock, "admin": lock},
        is_blocked=lambda h: h in blocked,
    )
    return AccessPolicy(cfg, role)


class TestRuleParityWithOriginalHelpers:
    @pytest.mark.parametrize(
        "role,lock,node_name,action",
        list(itertools.product(ROLES, (False, True), NODES, ACTIONS)),
    )
    def test_matches_expected_table(self, role, lock, node_name, action):
        node = NODES[node_name]
        assert make(role, lock).allows(node, action) is expected(role, lock, node, action)

    def test_download_follows_the_browse_rule(self):
        for role, lock, node in itertools.product(ROLES, (False, True), NODES.values()):
            p = make(role, lock)
            assert p.allows(node, DOWNLOAD) is p.allows(node, BROWSE)


class TestDenials:
    def test_denied_guest_gets_401_with_login_message(self):
        d = make("guest", lock=True).check(OTHER, BROWSE)
        assert (d.status, d.error) == (401, "Login required to browse external nodes")

    def test_denied_user_gets_403_with_restricted_message(self):
        d = make("user", lock=True).check(OTHER, BROWSE)
        assert (d.status, d.error) == (
            403, "Browsing external nodes is restricted by the administrator")

    def test_form_and_file_messages_are_distinct_and_unchanged(self):
        g = make("guest", lock=True)
        assert g.check(OTHER, INTERACT).error == "Login required to submit forms on external nodes"
        assert g.check(OTHER, DOWNLOAD).error == "Login required to fetch files from external nodes"
        u = make("user", lock=True)
        assert u.check(OTHER, INTERACT).error == (
            "Form submissions to external nodes are restricted by the administrator")
        assert u.check(OTHER, DOWNLOAD).error == (
            "Fetching from external nodes is restricted by the administrator")

    def test_allowed_is_none(self):
        assert make("guest", lock=True).check(DEFAULT) is None

    def test_unknown_action_is_a_programming_error(self):
        with pytest.raises(ValueError):
            make("guest").check(OTHER, "teleport")

    def test_unknown_role_is_rejected(self):
        with pytest.raises(ValueError):
            AccessPolicy(PolicyConfig(), "wizard")


class TestBlocklist:
    def test_blocked_node_is_403_for_everyone_including_super_admin(self):
        for role in ROLES:
            d = make(role, blocked={OTHER}).check(OTHER, BROWSE)
            assert (d.status, d.error) == (403, BLOCKED_MESSAGE), role

    def test_role_rule_is_reported_before_the_blocklist(self):
        # A locked-out guest is told to log in, not that the node is blocked.
        d = make("guest", lock=True, blocked={OTHER}).check(OTHER, BROWSE)
        assert d.status == 401

    def test_blocklist_is_case_insensitive(self):
        assert make("user", blocked={OTHER}).check(OTHER.upper()).error == BLOCKED_MESSAGE


class TestSettingsMigration:
    def test_legacy_default_node_becomes_a_one_entry_prescribed_list(self):
        cfg = PolicyConfig.from_settings({"default_node": DEFAULT.upper()}, hosted=HOSTED)
        assert cfg.prescribed == (DEFAULT,)
        assert cfg.landing == DEFAULT
        assert cfg.allowed == {HOSTED, DEFAULT}

    def test_explicit_prescribed_list_wins_over_default_node(self):
        cfg = PolicyConfig.from_settings(
            {"default_node": DEFAULT, "prescribed_nodes": [OTHER, DEFAULT]}, hosted=HOSTED)
        assert cfg.prescribed == (OTHER, DEFAULT)
        assert cfg.landing == OTHER

    def test_prescribed_list_is_deduplicated_and_blanks_dropped(self):
        cfg = PolicyConfig.from_settings(
            {"prescribed_nodes": [OTHER, OTHER.upper(), "", "  "]})
        assert cfg.prescribed == (OTHER,)

    def test_no_nodes_configured_leaves_only_the_hosted_site(self):
        cfg = PolicyConfig.from_settings({}, hosted=HOSTED)
        assert cfg.prescribed == () and cfg.landing is None
        assert cfg.allowed == {HOSTED}

    def test_locks_are_read_per_role(self):
        cfg = PolicyConfig.from_settings(
            {"guests_default_lock": True, "users_default_lock": False,
             "admins_default_lock": True})
        assert dict(cfg.locks) == {"guest": True, "user": False, "admin": True}


class TestKioskGuest:
    def kiosk(self, prescribed=(DEFAULT,)):
        return make("guest", lock=True, prescribed=prescribed)

    def test_confined_to_the_prescribed_nodes_and_the_hosted_site(self):
        p = self.kiosk()
        assert p.allows(DEFAULT) and p.allows(HOSTED)
        assert not p.allows(OTHER)

    def test_several_prescribed_nodes_are_all_reachable(self):
        p = self.kiosk(prescribed=(DEFAULT, OTHER))
        assert p.allows(DEFAULT) and p.allows(OTHER)

    def test_link_filter_hides_off_limits_links_only(self):
        follow = self.kiosk().link_filter()
        assert follow(DEFAULT) is True
        assert follow(OTHER) is False

    def test_logged_in_user_links_are_not_filtered(self):
        assert make("user", lock=False).link_filter()(OTHER) is True


class TestViewSnapshot:
    def test_confined_guest_gets_trusted_list_and_landing(self):
        v = make("guest", lock=True).view()
        assert v == {"role": "guest", "read_only": True, "landing": DEFAULT,
                     "confined": True, "trusted": sorted([HOSTED, DEFAULT])}

    def test_unconfined_viewer_is_not_confined_but_still_gets_trusted(self):
        v = make("user", lock=False).view()
        assert v["confined"] is False
        assert v["trusted"] == sorted([HOSTED, DEFAULT])

    def test_super_admin_is_never_confined(self):
        assert make("super_admin", lock=True).view()["confined"] is False

    def test_unlocked_guest_is_read_only_but_not_confined(self):
        v = make("guest", lock=False).view()
        assert v["read_only"] is True and v["confined"] is False

    def test_trusted_list_includes_every_prescribed_node(self):
        v = make("guest", lock=True, prescribed=(DEFAULT, OTHER)).view()
        assert set(v["trusted"]) == {HOSTED, DEFAULT, OTHER}


class TestRoleOf:
    def test_roles_from_user_flags(self):
        mk = lambda **kw: SimpleNamespace(super_admin=False, is_admin=False,
                                          is_authenticated=False, **kw)
        assert role_of(mk()) == "guest"
        assert role_of(SimpleNamespace(super_admin=False, is_admin=False,
                                       is_authenticated=True)) == "user"
        assert role_of(SimpleNamespace(super_admin=False, is_admin=True,
                                       is_authenticated=True)) == "admin"
        assert role_of(SimpleNamespace(super_admin=True, is_admin=True,
                                       is_authenticated=True)) == "super_admin"

    def test_object_without_flags_is_a_guest(self):
        assert role_of(object()) == "guest"

    def test_policy_for_wires_user_settings_and_blocklist(self):
        user = SimpleNamespace(super_admin=False, is_admin=False, is_authenticated=False)
        p = policy_for(user, {"guests_default_lock": True, "default_node": DEFAULT},
                       hosted=HOSTED, is_blocked=lambda h: h == DEFAULT)
        assert p.check(OTHER).status == 401
        assert p.check(DEFAULT).error == BLOCKED_MESSAGE


class TestIsTrusted:
    def test_hosted_and_prescribed_nodes_are_trusted_for_every_role(self):
        for role in ROLES:
            p = make(role, prescribed=(DEFAULT,))
            assert p.is_trusted(HOSTED) and p.is_trusted(DEFAULT.upper())

    def test_other_nodes_and_empty_are_not_trusted(self):
        p = make("super_admin")
        assert not p.is_trusted(OTHER) and not p.is_trusted("")

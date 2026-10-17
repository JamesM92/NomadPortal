"""The propagation-node host: settings rules, the mapping to LXMF's router,
the refusal to run on network storage, lifecycle (on, off, failure) and the
admin-facing status. LXMF and Reticulum are replaced by small fakes, so no
network is needed."""

import json
import os

import pytest

from nomadnet_web import propagation_host as ph


# ---------- fakes ----------------------------------------------------------

class FakeIdentity:
    created = 0

    def __init__(self, create_keys=True):
        FakeIdentity.created += 1
        self.hash = bytes([FakeIdentity.created]) * 16

    @classmethod
    def from_file(cls, path):
        if not os.path.exists(path):
            return None
        i = cls(create_keys=False)
        i.hash = open(path, "rb").read()
        return i

    def to_file(self, path):
        open(path, "wb").write(self.hash)


class FakeRNS:
    Identity = FakeIdentity


class FakeRouter:
    instances = []
    fail_enable = False

    def __init__(self, **kw):
        self.kw = kw
        self.calls = []
        self.propagation_node = False
        self.identity = kw["identity"]
        self.propagation_destination = type("D", (), {"hash": b"\x07" * 16})()
        FakeRouter.instances.append(self)

    def set_message_storage_limit(self, megabytes=None, **_):
        self.calls.append(("limit", megabytes))

    def enable_propagation(self):
        if FakeRouter.fail_enable:
            raise RuntimeError("boom")
        self.propagation_node = True
        self.calls.append("enable")

    def disable_propagation(self):
        self.propagation_node = False
        self.calls.append("disable")

    def announce_propagation_node(self):
        self.calls.append("announce")

    def message_storage_size(self):
        return 123456

    def compile_stats(self):
        return {
            "destination_hash": b"\x07" * 16,
            "uptime": 90,
            "messagestore": {"count": 4, "bytes": 123456, "limit": 500 * 1000 * 1000},
            "clients": {"client_propagation_messages_received": 3,
                        "client_propagation_messages_served": 2},
            "peers": {bytes([1]) * 16: {"type": "static", "alive": True, "last_heard": 100,
                                        "name": "Friend", "messages": {"offered": 1, "outgoing": 2, "incoming": 3}}},
            "static_peers": 1,
            "discovered_peers": 0,
        }


class FakeLXMF:
    LXMRouter = FakeRouter


@pytest.fixture(autouse=True)
def _reset():
    FakeRouter.instances = []
    FakeRouter.fail_enable = False
    FakeIdentity.created = 0


@pytest.fixture
def host(tmp_path):
    return ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS, mounts_text="")


# ---------- settings -------------------------------------------------------

class TestSettings:
    def test_safe_defaults(self):
        s = ph.DEFAULTS
        assert s["enabled"] is False
        assert s["storage_mb"] == 500
        assert s["autopeer"] is False and s["only_static"] is False
        assert s["static_peers"] == [] and s["stamp_cost"] == 16

    def test_a_good_patch_is_applied(self):
        s, errs = ph.clean_settings({"enabled": True, "name": "  Home  ", "storage_mb": "800",
                                     "static_peers": ["AB" * 16, "ab" * 16, "xyz"]}, ph.DEFAULTS)
        assert errs == []
        assert s["enabled"] is True and s["name"] == "Home" and s["storage_mb"] == 800
        assert s["static_peers"] == ["ab" * 16]               # lowercased, de-duplicated, junk dropped

    @pytest.mark.parametrize("field,value", [
        ("storage_mb", 0), ("storage_mb", -5), ("storage_mb", "lots"), ("storage_mb", 10**9),
        ("transfer_limit_kb", 0), ("max_peers", -1), ("autopeer_maxdepth", 0), ("stamp_cost", 99),
        ("stamp_cost", "x"), ("name", "n" * 200),
    ])
    def test_bad_values_are_refused_and_the_old_value_kept(self, field, value):
        s, errs = ph.clean_settings({field: value}, ph.DEFAULTS)
        assert errs and field in errs[0]
        assert s[field] == ph.DEFAULTS[field]

    def test_unknown_keys_are_ignored(self):
        s, errs = ph.clean_settings({"evil": 1}, ph.DEFAULTS)
        assert "evil" not in s and errs == []

    def test_only_static_without_peers_is_refused(self):
        s, errs = ph.clean_settings({"only_static": True}, ph.DEFAULTS)
        assert errs and s["only_static"] is False


class TestRouterArguments:
    def test_mapping(self):
        s = dict(ph.DEFAULTS, name="Home", transfer_limit_kb=128, autopeer=True, max_peers=5,
                 autopeer_maxdepth=2, static_peers=["ab" * 16], only_static=True, stamp_cost=18)
        kw = ph.router_kwargs(s)
        assert kw["name"] == "Home" and kw["propagation_limit"] == 128
        assert kw["autopeer"] is True and kw["max_peers"] == 5 and kw["autopeer_maxdepth"] == 2
        assert kw["static_peers"] == [bytes.fromhex("ab" * 16)]
        assert kw["from_static_only"] is True and kw["propagation_cost"] == 18

    def test_blank_name_is_not_passed(self):
        assert ph.router_kwargs(ph.DEFAULTS)["name"] is None


class TestNetworkMount:
    MOUNTS = ("/dev/sda1 / ext4 rw 0 0\n"
              "nas:/export /mnt/nas nfs4 rw 0 0\n"
              "//srv/share /mnt/smb cifs rw 0 0\n"
              "user@h:/ /mnt/ssh fuse.sshfs rw 0 0\n")

    @pytest.mark.parametrize("path,expected", [
        ("/config/propagation", False), ("/mnt/nas/x", True), ("/mnt/smb", True),
        ("/mnt/ssh/a/b", True), ("/mnt/nasty", False),
    ])
    def test_detection(self, path, expected):
        assert ph.is_network_mount(path, self.MOUNTS) is expected

    def test_unreadable_mount_table_is_not_an_alarm(self):
        assert ph.is_network_mount("/x", "") is False


# ---------- lifecycle ------------------------------------------------------

class TestLifecycle:
    def test_nothing_starts_by_default(self, host):
        host.apply()
        assert FakeRouter.instances == []
        assert host.status()["state"] == "off"

    def test_enabling_starts_a_router_with_a_cap_and_announces(self, host):
        host.update({"enabled": True, "storage_mb": 300})
        r = FakeRouter.instances[0]
        assert ("limit", 300) in r.calls and "enable" in r.calls
        assert host.status()["state"] == "running"

    def test_the_identity_is_created_once_and_reused(self, tmp_path):
        a = ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS, mounts_text="")
        a.update({"enabled": True})
        first = FakeRouter.instances[0].identity.hash
        b = ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS, mounts_text="")
        b.apply()
        assert FakeRouter.instances[-1].identity.hash == first
        assert os.path.exists(tmp_path / "identity")

    def test_disabling_stops_and_announces_the_stop(self, host):
        host.update({"enabled": True})
        host.update({"enabled": False})
        assert "disable" in FakeRouter.instances[0].calls
        assert host.status()["state"] == "off"

    def test_turning_it_on_again_reuses_the_router(self, host):
        # LXMF cannot tear a router down (its job thread and destinations
        # live for the whole process), so there is exactly one per run.
        host.update({"enabled": True})
        host.update({"enabled": False})
        host.update({"enabled": True})
        assert len(FakeRouter.instances) == 1
        assert FakeRouter.instances[0].calls.count("enable") == 2
        assert host.status()["state"] == "running"

    def test_the_storage_cap_changes_live(self, host):
        host.update({"enabled": True, "storage_mb": 500})
        host.update({"storage_mb": 900})
        assert len(FakeRouter.instances) == 1
        assert ("limit", 900) in FakeRouter.instances[0].calls
        assert host.status()["restart_required"] is False

    def test_other_router_settings_wait_for_a_restart_and_say_so(self, host):
        host.update({"enabled": True})
        host.update({"max_peers": 3})
        assert len(FakeRouter.instances) == 1 and FakeRouter.instances[0].kw["max_peers"] == 20
        st = host.status()
        assert st["restart_required"] is True and st["settings"]["max_peers"] == 3

    def test_after_a_restart_the_new_settings_are_used(self, tmp_path):
        a = ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS, mounts_text="")
        a.update({"enabled": True})
        a.update({"max_peers": 3})
        b = ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS, mounts_text="")
        b.apply()
        assert FakeRouter.instances[-1].kw["max_peers"] == 3
        assert b.status()["restart_required"] is False

    def test_a_failing_start_leaves_the_node_off_and_says_why(self, host):
        FakeRouter.fail_enable = True
        out = host.update({"enabled": True})
        st = host.status()
        assert st["state"] == "error" and "boom" in st["error"]
        assert out["settings"]["enabled"] is True            # the wish is kept, so Retry works

    def test_refuses_to_run_on_network_storage(self, tmp_path):
        h = ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS,
                               mounts_text=f"nas:/x {tmp_path} nfs4 rw 0 0\n")
        h.update({"enabled": True})
        assert FakeRouter.instances == []
        st = h.status()
        assert st["state"] == "error" and "network" in st["error"].lower()

    def test_settings_survive_a_restart(self, tmp_path):
        a = ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS, mounts_text="")
        a.update({"enabled": True, "name": "Home", "storage_mb": 700})
        b = ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS, mounts_text="")
        assert b.settings()["name"] == "Home" and b.settings()["storage_mb"] == 700
        b.apply()
        assert b.status()["state"] == "running"

    def test_a_damaged_settings_file_falls_back_to_defaults(self, tmp_path):
        (tmp_path / "settings.json").write_text("{not json")
        h = ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS, mounts_text="")
        assert h.settings() == ph.DEFAULTS

    def test_invalid_patch_changes_nothing_and_reports(self, host):
        out = host.update({"storage_mb": -1})
        assert out["errors"] and host.settings()["storage_mb"] == 500


class TestAnnounce:
    def test_manual_announce_works_only_when_running(self, host):
        assert host.announce()["ok"] is False
        host.update({"enabled": True})
        assert host.announce()["ok"] is True
        assert FakeRouter.instances[0].calls.count("announce") == 1

    def test_manual_announce_is_rate_limited(self, tmp_path):
        t = [1000.0]
        h = ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS, mounts_text="", clock=lambda: t[0])
        h.update({"enabled": True})
        assert h.announce()["ok"] is True
        r = h.announce()
        assert r["ok"] is False and r["retry_in_s"] > 0
        t[0] += ph.MANUAL_ANNOUNCE_COOLDOWN_S + 1
        assert h.announce()["ok"] is True

    def test_periodic_announce_is_due_after_the_interval_only(self, tmp_path):
        t = [1000.0]
        h = ph.PropagationHost(str(tmp_path), FakeLXMF, FakeRNS, mounts_text="", clock=lambda: t[0])
        h.update({"enabled": True})
        h.tick()
        n = FakeRouter.instances[0].calls.count("announce")
        t[0] += ph.REANNOUNCE_INTERVAL_S - 5
        h.tick()
        assert FakeRouter.instances[0].calls.count("announce") == n
        t[0] += 10
        h.tick()
        assert FakeRouter.instances[0].calls.count("announce") == n + 1

    def test_tick_does_nothing_when_off(self, host):
        host.tick()
        assert FakeRouter.instances == []


# ---------- status ---------------------------------------------------------

class TestStatus:
    def test_running_status_has_what_the_panel_shows(self, host):
        host.update({"enabled": True, "storage_mb": 500})
        st = host.status()
        assert st["address"] == "07" * 16
        assert st["uptime_s"] == 90 and st["messages"] == 4
        assert st["store_bytes"] == 123456 and st["store_limit_bytes"] == 500 * 1000 * 1000
        assert st["peers"][0]["name"] == "Friend" and st["peers"][0]["alive"] is True
        assert st["peers"][0]["id"] == "01" * 16

    def test_status_never_carries_message_contents(self, host):
        host.update({"enabled": True})
        assert "content" not in json.dumps(host.status(), default=str).lower()

    def test_broken_stats_do_not_break_status(self, host):
        host.update({"enabled": True})
        FakeRouter.instances[0].compile_stats = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        st = host.status()
        assert st["state"] == "running" and st["peers"] == []

    def test_stats_returning_none_is_tolerated(self, host):
        host.update({"enabled": True})
        FakeRouter.instances[0].compile_stats = lambda: None
        assert host.status()["messages"] == 0

    def test_off_status_still_shows_settings(self, host):
        st = host.status()
        assert st["state"] == "off" and st["settings"]["storage_mb"] == 500

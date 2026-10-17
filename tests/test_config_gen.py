"""config.yml -> Reticulum config. These pin what the generator writes now,
so a change to it is a decision, not an accident. The result is also parsed
with ConfigObj (the parser Reticulum itself uses) to prove it is valid."""

import json
import os

import pytest
import yaml
from RNS.vendor.configobj import ConfigObj

from nomadnet_web.config_gen import generate


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.delenv("SITE_HOSTING", raising=False)
    return str(tmp_path / "config.yml"), str(tmp_path / "rns" / "config")


def run(paths, cfg):
    yml, out = paths
    with open(yml, "w") as fh:
        yaml.safe_dump(cfg, fh)
    assert generate(yml, out) is True
    return open(out).read(), ConfigObj(out.__str__())


def test_no_config_yml_means_nothing_is_written(paths):
    yml, out = paths
    assert generate(yml, out) is False
    assert not os.path.exists(out)


def test_a_fresh_file_is_seeded_and_valid(paths):
    _, c = run(paths, {})
    assert c["reticulum"]["share_instance"] == "No"
    assert c["logging"]["loglevel"] == "4"
    assert "interfaces" in c


class TestTransport:
    def test_explicit_true_and_false_win(self, paths):
        _, c = run(paths, {"transport_mode": True})
        assert c["reticulum"]["enable_transport"] == "True"
        _, c = run(paths, {"transport_mode": False})
        assert c["reticulum"]["enable_transport"] == "False"

    def test_unset_follows_the_hosting_env_default(self, paths, monkeypatch):
        _, c = run(paths, {})
        assert c["reticulum"]["enable_transport"] == "True"       # hosting defaults on
        monkeypatch.setenv("SITE_HOSTING", "off")
        _, c = run(paths, {})
        assert c["reticulum"]["enable_transport"] == "False"

    def test_the_admin_setting_beats_the_env(self, paths, monkeypatch):
        monkeypatch.setenv("SITE_HOSTING", "true")
        d = os.path.dirname(paths[0])
        with open(os.path.join(d, "ui_settings.json"), "w") as fh:
            json.dump({"hosting_enabled": False}, fh)
        _, c = run(paths, {})
        assert c["reticulum"]["enable_transport"] == "False"

    def test_a_damaged_ui_settings_file_falls_back_to_env(self, paths):
        d = os.path.dirname(paths[0])
        with open(os.path.join(d, "ui_settings.json"), "w") as fh:
            fh.write("{nope")
        _, c = run(paths, {})
        assert c["reticulum"]["enable_transport"] == "True"


class TestInterfaces:
    def test_disabled_entries_are_left_out(self, paths):
        _, c = run(paths, {"interfaces": {
            "auto": {"enabled": False},
            "tcp_clients": [{"name": "Off", "host": "h", "port": 1, "enabled": False}]}})
        assert list(c["interfaces"]) == []

    def test_each_kind_is_written_with_its_fields(self, paths):
        _, c = run(paths, {"interfaces": {
            "auto": {"enabled": True, "group_id": "grp"},
            "tcp_clients": [{"name": "Hub", "host": "hub.example", "port": 4242, "enabled": True,
                             "mode": "gateway", "network_name": "net", "fixed_mtu": 1000}],
            "tcp_servers": [{"name": "Srv", "port": 4343, "enabled": True}],
            "udp": [{"name": "U", "listen_port": 5000, "enabled": True}],
            "rnodes": [{"name": "Lora", "port": "/dev/ttyUSB0", "enabled": True, "spreading_factor": 10}],
            "i2p": [{"name": "I", "enabled": True, "connectable": True, "peers": ["a.b32.i2p", "c.b32.i2p"]}],
        }})
        i = c["interfaces"]
        assert i["Auto Interface"]["type"] == "AutoInterface" and i["Auto Interface"]["group_id"] == "grp"
        hub = i["Hub"]
        assert (hub["type"], hub["target_host"], hub["target_port"]) == ("TCPClientInterface", "hub.example", "4242")
        assert (hub["mode"], hub["network_name"], hub["fixed_mtu"]) == ("gateway", "net", "1000")
        assert i["Srv"]["listen_ip"] == "0.0.0.0" and i["Srv"]["listen_port"] == "4343"
        assert i["U"]["forward_ip"] == "255.255.255.255" and i["U"]["forward_port"] == "5000"
        assert i["Lora"]["spreadingfactor"] == "10" and i["Lora"]["frequency"] == "867500000"
        assert i["I"]["connectable"] == "Yes" and i["I"]["peers"] == ["a.b32.i2p", "c.b32.i2p"]

    def test_an_explicit_false_boolean_is_written_as_no(self, paths):
        # RNS defaults ingress_control on, so "off" has to reach the file.
        _, c = run(paths, {"interfaces": {"tcp_clients": [
            {"name": "H", "host": "h", "port": 1, "enabled": True, "ingress_control": False}]}})
        assert c["interfaces"]["H"]["ingress_control"] == "No"

    def test_an_unset_or_blank_option_is_not_written(self, paths):
        _, c = run(paths, {"interfaces": {"tcp_clients": [
            {"name": "H", "host": "h", "port": 1, "enabled": True, "mode": "", "passphrase": None}]}})
        assert "mode" not in c["interfaces"]["H"] and "passphrase" not in c["interfaces"]["H"]

    def test_regenerating_replaces_the_old_interfaces(self, paths):
        run(paths, {"interfaces": {"tcp_clients": [{"name": "Old", "host": "h", "port": 1, "enabled": True}]}})
        _, c = run(paths, {"interfaces": {"tcp_clients": [{"name": "New", "host": "h", "port": 2, "enabled": True}]}})
        assert list(c["interfaces"]) == ["New"]


class TestSharedInstance:
    def test_off_by_default(self, paths):
        _, c = run(paths, {})
        assert c["reticulum"]["share_instance"] == "No"
        assert "instance_name" not in c["reticulum"] and "shared_instance_port" not in c["reticulum"]

    def test_settings_are_written_and_removed_again(self, paths):
        _, c = run(paths, {"shared_instance": {"enabled": True, "instance_name": " mesh ", "port": 37428, "control_port": 37429}})
        r = c["reticulum"]
        assert (r["share_instance"], r["instance_name"], r["shared_instance_port"], r["instance_control_port"]) == \
            ("Yes", "mesh", "37428", "37429")
        _, c = run(paths, {})
        assert "instance_name" not in c["reticulum"] and "instance_control_port" not in c["reticulum"]


class TestProbes:
    def test_ignoring_probes_sets_respond_to_probes_no(self, paths):
        _, c = run(paths, {"ignore_discovery_probes": True})
        assert c["reticulum"]["respond_to_probes"] == "No"
        _, c = run(paths, {"ignore_discovery_probes": False})
        assert "respond_to_probes" not in c["reticulum"]


class TestExistingFileIsPreserved:
    def test_other_settings_and_sections_survive(self, paths):
        yml, out = paths
        os.makedirs(os.path.dirname(out))
        with open(out, "w") as fh:
            fh.write("[reticulum]\n  enable_transport = False\n  panic_on_interface_error = Yes\n\n"
                     "[logging]\n  loglevel = 6\n\n[interfaces]\n  [[Old]]\n    type = AutoInterface\n")
        _, c = run(paths, {"transport_mode": True})
        assert c["reticulum"]["panic_on_interface_error"] == "Yes"
        assert c["logging"]["loglevel"] == "6"
        assert c["reticulum"]["enable_transport"] == "True"
        assert "Old" not in c["interfaces"]

    def test_running_twice_gives_the_same_file(self, paths):
        cfg = {"transport_mode": True, "interfaces": {"auto": {"enabled": True}}}
        a, _ = run(paths, cfg)
        b, _ = run(paths, cfg)
        assert a == b

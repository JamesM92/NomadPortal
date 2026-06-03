"""
Sync config.yml → Reticulum config file.

Updates these things on every container start:
  1. enable_transport in the [reticulum] section
  2. share_instance / shared_instance_port / instance_control_port /
     instance_name in the [reticulum] section
  3. The entire [interfaces] block

All other settings in the RNS config file are left untouched.
"""

import logging
import os
import re

import yaml

log = logging.getLogger(__name__)


def generate(config_yml: str, rns_config_path: str) -> bool:
    """Read *config_yml* and update *rns_config_path*.

    Returns True if the file was written, False if config.yml is absent.
    """
    if not os.path.exists(config_yml):
        log.debug("config.yml not found at %s — skipping", config_yml)
        return False

    with open(config_yml, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    transport       = bool(cfg.get("transport_mode", False))
    ignore_probes   = bool(cfg.get("ignore_discovery_probes", False))
    ifaces          = cfg.get("interfaces", {})
    sections        = _build_interface_sections(ifaces)

    os.makedirs(os.path.dirname(rns_config_path), exist_ok=True)

    # Read or seed the RNS config file
    if os.path.exists(rns_config_path):
        with open(rns_config_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = _DEFAULT_CONFIG

    text = _set_transport(text, transport)
    text = _set_reticulum_kv(text, "respond_to_probes",
                             "No" if ignore_probes else None)
    text = _apply_shared_instance(text, cfg.get("shared_instance") or {})
    text = _replace_interfaces(text, sections)

    with open(rns_config_path, "w", encoding="utf-8") as fh:
        fh.write(text)

    log.info(
        "RNS config updated — %d interface(s), transport=%s",
        len(sections), transport,
    )
    return True


# ---------------------------------------------------------------------------
# Interface section builders
# ---------------------------------------------------------------------------

def _build_interface_sections(ifaces: dict) -> list[str]:
    sections = []

    # AutoInterface
    auto = ifaces.get("auto", {})
    if auto.get("enabled", False):
        fields = {"type": "AutoInterface", "enabled": "Yes"}
        if auto.get("group_id"):
            fields["group_id"] = auto["group_id"]
        sections.append(_iface("Auto Interface", fields))

    # TCP Clients
    for entry in ifaces.get("tcp_clients", []):
        if not entry.get("enabled", False):
            continue
        fields = {
            "type":        "TCPClientInterface",
            "enabled":     "Yes",
            "target_host": entry["host"],
            "target_port": entry["port"],
        }
        _optional(fields, entry, "kiss_framing",  "kiss_framing",  _yn)
        _optional(fields, entry, "i2p_tunneled",  "i2p_tunneled",  _yn)
        _optional(fields, entry, "mode",          "mode")
        _optional(fields, entry, "network_name",  "network_name")
        _optional(fields, entry, "passphrase",    "passphrase")
        sections.append(_iface(entry.get("name", "TCP Client"), fields))

    # TCP Servers
    for entry in ifaces.get("tcp_servers", []):
        if not entry.get("enabled", False):
            continue
        fields = {
            "type":        "TCPServerInterface",
            "enabled":     "Yes",
            "listen_ip":   entry.get("listen_ip", "0.0.0.0"),
            "listen_port": entry["port"],
        }
        _optional(fields, entry, "prefer_ipv6",  "prefer_ipv6",  _yn)
        _optional(fields, entry, "mode",         "mode")
        _optional(fields, entry, "network_name", "network_name")
        _optional(fields, entry, "passphrase",   "passphrase")
        sections.append(_iface(entry.get("name", "TCP Server"), fields))

    # UDP Interfaces
    for entry in ifaces.get("udp", []):
        if not entry.get("enabled", False):
            continue
        fields = {
            "type":         "UDPInterface",
            "enabled":      "Yes",
            "listen_ip":    entry.get("listen_ip", "0.0.0.0"),
            "listen_port":  entry["listen_port"],
            "forward_ip":   entry.get("forward_ip", "255.255.255.255"),
            "forward_port": entry.get("forward_port", entry["listen_port"]),
        }
        _optional(fields, entry, "mode",         "mode")
        _optional(fields, entry, "network_name", "network_name")
        _optional(fields, entry, "passphrase",   "passphrase")
        sections.append(_iface(entry.get("name", "UDP Interface"), fields))

    # RNode / LoRa
    for entry in ifaces.get("rnodes", []):
        if not entry.get("enabled", False):
            continue
        fields = {
            "type":           "RNodeInterface",
            "enabled":        "Yes",
            "port":           entry["port"],
            "frequency":      entry.get("frequency", 867500000),
            "bandwidth":      entry.get("bandwidth", 125000),
            "txpower":        entry.get("txpower", 7),
            "spreadingfactor":entry.get("spreading_factor", 8),
            "codingrate":     entry.get("coding_rate", 5),
        }
        _optional(fields, entry, "id_callsign",  "id_callsign")
        _optional(fields, entry, "id_interval",  "id_interval")
        _optional(fields, entry, "flow_control", "flow_control", _yn)
        _optional(fields, entry, "mode",         "mode")
        sections.append(_iface(entry.get("name", "RNode"), fields))

    # I2P
    for entry in ifaces.get("i2p", []):
        if not entry.get("enabled", False):
            continue
        fields = {
            "type":        "I2PInterface",
            "enabled":     "Yes",
            "connectable": _yn(entry.get("connectable", False)),
        }
        peers = entry.get("peers", [])
        if peers:
            fields["peers"] = ", ".join(peers)
        sections.append(_iface(entry.get("name", "I2P Interface"), fields))

    return sections


def _iface(name: str, fields: dict) -> str:
    lines = [f"  [[{name}]]"]
    for k, v in fields.items():
        lines.append(f"    {k} = {v}")
    lines.append("")
    return "\n".join(lines)


def _optional(out: dict, src: dict, key: str, out_key: str, transform=None):
    val = src.get(key)
    if val is not None and val != "" and val is not False:
        out[out_key] = transform(val) if transform else val


def _yn(v) -> str:
    return "Yes" if v else "No"


# ---------------------------------------------------------------------------
# Config file patching helpers
# ---------------------------------------------------------------------------

def _set_transport(text: str, enabled: bool) -> str:
    """Set enable_transport in the [reticulum] section."""
    return _set_reticulum_kv(text, "enable_transport", "True" if enabled else "False")


def _set_reticulum_kv(text: str, key: str, value) -> str:
    """Set or remove a key=value line in the [reticulum] section.

    Pass ``value=None`` (or empty string) to remove the key, letting Reticulum
    fall back to its built-in default. Otherwise the value is stringified
    verbatim.
    """
    if value is None or value == "":
        # Strip the line entirely if present
        return re.sub(
            rf"(?m)^\s*{re.escape(key)}\s*=.*\n?",
            "",
            text,
        )

    val = str(value)
    patched, n = re.subn(
        rf"(?m)^(\s*{re.escape(key)}\s*=\s*).*$",
        rf"\g<1>{val}",
        text,
    )
    if n:
        return patched
    # Insert into [reticulum] block (after header line)
    return re.sub(
        r"(\[reticulum\][^\[]*)",
        lambda m: m.group(0).rstrip() + f"\n  {key} = {val}\n",
        text,
        count=1,
    )


def _apply_shared_instance(text: str, shared: dict) -> str:
    """Apply the [reticulum] shared-instance keys from ``shared``.

    ``shared`` may contain ``enabled`` (bool, default False — NomadPortal
    defaults shared-instance off because co-located instances in the same
    Docker network namespace will otherwise collide on the loopback IPC
    socket), ``instance_name`` (str), ``port`` (int → ``shared_instance_port``),
    and ``control_port`` (int → ``instance_control_port``).
    """
    enabled = shared.get("enabled", False)
    text = _set_reticulum_kv(text, "share_instance", "Yes" if enabled else "No")
    text = _set_reticulum_kv(text, "instance_name",
                             (shared.get("instance_name") or "").strip() or None)
    text = _set_reticulum_kv(text, "shared_instance_port",
                             shared.get("port") or None)
    text = _set_reticulum_kv(text, "instance_control_port",
                             shared.get("control_port") or None)
    return text


def _replace_interfaces(text: str, sections: list[str]) -> str:
    """Replace the [interfaces] block (to end of file or next [section])."""
    block = (
        "[interfaces]\n\n"
        "  # Auto-generated by NomadPortal — edit config.yml to change.\n\n"
        + "\n".join(sections)
    )
    # Remove existing [interfaces] block
    trimmed = re.sub(r"\n\[interfaces\].*", "", text, flags=re.DOTALL).rstrip()
    return trimmed + "\n\n" + block + "\n"


# ---------------------------------------------------------------------------
# Minimal seed config written when no RNS config exists yet
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = """\
[reticulum]
  enable_transport = False
  share_instance = No
  instance_name = default

[logging]
  loglevel = 4

"""

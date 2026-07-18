"""
Sync config.yml → Reticulum config file.

Updates these things on every container start:
  1. enable_transport in the [reticulum] section
  2. share_instance / shared_instance_port / instance_control_port /
     instance_name in the [reticulum] section
  3. The entire [interfaces] block

All other settings in the RNS config file are left untouched.
"""

import json
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

    transport       = _resolve_transport_mode(cfg, config_yml)
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

def _resolve_transport_mode(cfg: dict, config_yml: str) -> bool:
    """Decide the effective ``enable_transport`` setting.

    Order of precedence:

    1. If ``transport_mode`` is explicitly set in ``config.yml`` (a real
       True or False, not None/absent) → use that value.
    2. Otherwise, mirror the site-hosting resolution from
       ``nomadnet_web/__init__.py``: the Admin → Settings UI wins if
       set, otherwise fall back to ``SITE_HOSTING`` env / default True.
       Hosting on → transport on; hosting off → transport off.

    Why: RNS 1.3+ requires the receiver of an inbound link request to
    have proper return-path state in its path table to send the
    proof-RTT packet back. ``enable_transport = False`` (leaf mode)
    doesn't maintain that, so leaf-mode hosts silently fail to
    complete inbound link establishment after accepting the request.
    Pre-RNS-1.3 this worked; from 1.3 onward, hosting nodes need
    transport on. Auto-correlating with the hosting flag keeps
    browse-only NomadPortals as leaves (cheap) while making
    site-hosting NomadPortals transit-capable (correct).
    """
    explicit = cfg.get("transport_mode")
    if explicit is not None:
        return bool(explicit)

    config_dir = os.path.dirname(config_yml)
    ui_hosting = None
    ui_path = os.path.join(config_dir, "ui_settings.json")
    if os.path.exists(ui_path):
        try:
            with open(ui_path, "r", encoding="utf-8") as fh:
                ui = json.load(fh) or {}
            ui_hosting = ui.get("hosting_enabled")
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s for transport auto-default: %s",
                        ui_path, exc)

    if ui_hosting is not None:
        derived = bool(ui_hosting)
        source = "Admin UI hosting_enabled"
    else:
        hosting_raw = os.environ.get("SITE_HOSTING", "true").strip().lower()
        derived     = hosting_raw not in ("0", "false", "no", "off", "")
        source      = "SITE_HOSTING env"

    log.info("transport_mode not set in config.yml — auto-defaulting to %s "
             "(source: %s)", derived, source)
    return derived


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
        _optional(fields, entry, "kiss_framing",     "kiss_framing",     _yn)
        _optional(fields, entry, "i2p_tunneled",     "i2p_tunneled",     _yn)
        _optional(fields, entry, "mode",             "mode")
        _optional(fields, entry, "network_name",     "network_name")
        _optional(fields, entry, "passphrase",       "passphrase")
        # ingress_control defaults to True in RNS. Setting it False
        # disables per-interface announce rate-limiting on this
        # link — recommended for busy public hubs where the default
        # limit causes new-destination announces to be held/dropped
        # during burst periods, leaving the client's path table
        # sparser than peers who happened to be listening during a
        # quiet moment.
        _optional(fields, entry, "ingress_control",  "ingress_control",  _yn)
        # fixed_mtu forces RNS to use this MTU on the interface instead
        # of its default (8192 for TCP). Use when this container's
        # outbound path traverses a low-MTU tunnel (e.g. Gluetun's
        # WireGuard tun0 with MTU 1171) — set fixed_mtu to something
        # comfortably below the tunnel's MTU (1000 is a safe default
        # for the ~1200-1280 MTU tunnels most VPN providers use). Too
        # small hurts throughput; too large replicates the blackhole.
        _optional(fields, entry, "fixed_mtu",        "fixed_mtu")
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
        _optional(fields, entry, "prefer_ipv6",     "prefer_ipv6",     _yn)
        _optional(fields, entry, "mode",            "mode")
        _optional(fields, entry, "network_name",    "network_name")
        _optional(fields, entry, "passphrase",      "passphrase")
        _optional(fields, entry, "ingress_control", "ingress_control", _yn)
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
    """Copy ``src[key]`` into ``out[out_key]`` if the operator set it.

    Handling of ``False``:
      * With NO transform — treat ``False`` as "unset", skip. This
        preserves the old behaviour for string/numeric fields where
        we didn't want to emit "false" as a literal string.
      * WITH a transform (typically ``_yn`` for booleans) —
        ``False`` is a meaningful value and MUST be emitted. Skipping
        it silently collapses "user wants False" with "user didn't
        set it," which is exactly what caused ``ingress_control: false``
        to never reach the RNS config even after we plumbed it through
        the schema. RNS defaults ``ingress_control = True``, so the
        skip-on-False behaviour meant the operator's explicit "off"
        was silently overridden by the RNS default.
    """
    val = src.get(key)
    if val is None or val == "":
        return
    if val is False and transform is None:
        return
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

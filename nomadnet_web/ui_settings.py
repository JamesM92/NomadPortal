import json
import logging
import os
import re
import threading

log = logging.getLogger(__name__)

_ADDR_BAR_STATES = {"enabled", "disabled", "hidden"}
_ACCESS_PRESETS  = {"public", "gated", "locked"}
_HEX_RE          = re.compile(r'^[0-9a-f]{1,128}$')

# Per-audience field templates for each preset. The Settings UI offers these
# as a "fill the table" action; the per-audience values are the source of
# truth, the preset is purely a display label computed from them.
# Admin defaults for every preset — admins are unrestricted. The Settings
# table exposes these cells so the super admin can override per-deployment
# (e.g. compliance) but no preset ever flips them on.
_ADMIN_UNRESTRICTED = {
    "admins_default_lock":     False,
    "admins_address_bar":      "enabled",
    "admins_nodes_panel":      True,
    "admins_messages_panel":   True,
}

_PRESETS = {
    "public": {
        "guests_default_lock":     False,
        "users_default_lock":      False,
        "guests_address_bar":      "enabled",
        "users_address_bar":       "enabled",
        "guests_nodes_panel":      True,
        "users_nodes_panel":       True,
        "guests_messages_panel":   True,
        "users_messages_panel":    True,
        "users_can_message":       True,
        **_ADMIN_UNRESTRICTED,
    },
    "gated": {
        "guests_default_lock":     True,
        "users_default_lock":      False,
        "guests_address_bar":      "hidden",
        "users_address_bar":       "enabled",
        "guests_nodes_panel":      False,
        "users_nodes_panel":       True,
        "guests_messages_panel":   False,
        "users_messages_panel":    True,
        "users_can_message":       True,
        **_ADMIN_UNRESTRICTED,
    },
    "locked": {
        "guests_default_lock":     True,
        "users_default_lock":      True,
        "guests_address_bar":      "hidden",
        "users_address_bar":       "hidden",
        "guests_nodes_panel":      False,
        "users_nodes_panel":       False,
        "guests_messages_panel":   False,
        "users_messages_panel":    False,
        "users_can_message":       True,
        **_ADMIN_UNRESTRICTED,
    },
}

# Fields that the super-admin gate protects. Edits to these from a
# non-super-admin admin are silently dropped at the API layer.
ADMIN_GATED_FIELDS = frozenset(_ADMIN_UNRESTRICTED.keys())


def matching_preset(values: dict) -> str:
    """Return the preset name whose template matches values exactly, or 'custom'."""
    for name, tpl in _PRESETS.items():
        if all(values.get(k) == v for k, v in tpl.items()):
            return name
    return "custom"


# Migrate old lockdown_node (off | guests | users) → preset name
_OLD_LOCKDOWN_TO_PRESET = {
    "off":    "public",
    "guests": "gated",
    "users":  "locked",
}

# Migrate old show_* (everyone | users | admins) → bool
_OLD_SHOW_TO_BOOL = {
    "everyone": True,
    "users":    True,
    "admins":   False,
}



class UISettings:
    # Per-audience access fields are the source of truth. `access_mode` is
    # informational — recomputed from these on read.
    DEFAULTS = {
        "app_title":           "`F4af■ NomadPortal`f",
        "site_name":           "",
        "default_node":        "",
        "abuse_contact":       "",
        # Site-server toggles (None = "fall through to env var"; True/False
        # = explicit override). Operators can flip these via Admin →
        # Settings without rewriting docker-compose; existing env-var
        # config still wins when the UI value is None.
        "hosting_enabled":     None,
        "auto_announce":       None,
        # Re-announce frequency in seconds, or None to fall through to
        # the SITE_ANNOUNCE_INTERVAL env var / 6h default. Clamped at
        # read/write time to [60, 86400].
        "announce_interval":   None,
        # Access controls (defaults seeded from the "locked" preset — safe
        # default for fresh installs; operator can relax via Admin → Settings)
        **_PRESETS["locked"],
    }

    def __init__(self, config_dir: str):
        self._file = os.path.join(config_dir, "ui_settings.json")
        self._lock = threading.Lock()
        self._data = dict(self.DEFAULTS)
        self._load()

    def get_all(self) -> dict:
        with self._lock:
            data = dict(self._data)
        data["access_mode"] = matching_preset(data)
        return data

    _BOOL_FIELDS = (
        "guests_default_lock", "users_default_lock", "admins_default_lock",
        "guests_nodes_panel", "users_nodes_panel", "admins_nodes_panel",
        "guests_messages_panel", "users_messages_panel", "admins_messages_panel",
        "users_can_message",
    )

    def update(self, patch: dict) -> None:
        with self._lock:
            # Preset selection — fills all per-audience fields from a template.
            # Apply BEFORE the per-field updates so the latter can override.
            preset = patch.get("access_mode")
            if preset in _PRESETS:
                self._data.update(_PRESETS[preset])
            elif "lockdown_node" in patch:
                # Backwards-compatible alias for old API clients
                mapped = _OLD_LOCKDOWN_TO_PRESET.get(patch["lockdown_node"])
                if mapped:
                    self._data.update(_PRESETS[mapped])

            for k in self._BOOL_FIELDS:
                if k in patch:
                    self._data[k] = bool(patch[k])
            if patch.get("guests_address_bar") in _ADDR_BAR_STATES:
                self._data["guests_address_bar"] = patch["guests_address_bar"]
            if patch.get("users_address_bar") in _ADDR_BAR_STATES:
                self._data["users_address_bar"] = patch["users_address_bar"]
            if patch.get("admins_address_bar") in _ADDR_BAR_STATES:
                self._data["admins_address_bar"] = patch["admins_address_bar"]

            if "app_title" in patch:
                self._data["app_title"] = str(patch["app_title"]).strip()[:128] or "`F4af■ NomadPortal`f"
            if "site_name" in patch:
                self._data["site_name"] = str(patch["site_name"]).strip()[:64]
            # Tri-state: None means "use env-var" (the default); True/False
            # explicitly overrides. Empty string from a form posts as None.
            for k in ("hosting_enabled", "auto_announce"):
                if k in patch:
                    raw = patch[k]
                    if raw in (None, ""):
                        self._data[k] = None
                    else:
                        self._data[k] = bool(raw)
            # Announce interval: positive int seconds clamped to a sane
            # range, or None for "use env var". Non-int values are dropped
            # silently rather than reverting to default — the operator's
            # last good value stays put.
            if "announce_interval" in patch:
                raw = patch["announce_interval"]
                if raw in (None, ""):
                    self._data["announce_interval"] = None
                else:
                    try:
                        v = int(raw)
                        if v < 60:    v = 60
                        if v > 86400: v = 86400
                        self._data["announce_interval"] = v
                    except (TypeError, ValueError):
                        pass
            if "default_node" in patch:
                v = str(patch["default_node"]).strip().lower()
                self._data["default_node"] = v if _HEX_RE.match(v) else ""
            if "abuse_contact" in patch:
                self._data["abuse_contact"] = str(patch["abuse_contact"]).strip()[:256]
            snapshot = dict(self._data)
        self._save(snapshot)

    def _load(self):
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            with self._lock:
                if data.get("app_title"):
                    self._data["app_title"] = str(data["app_title"])[:128]
                if "site_name" in data:
                    self._data["site_name"] = str(data["site_name"]).strip()[:64]
                v = str(data.get("default_node", "")).strip().lower()
                self._data["default_node"] = v if _HEX_RE.match(v) else ""
                if "abuse_contact" in data:
                    self._data["abuse_contact"] = str(data["abuse_contact"]).strip()[:256]

                # Tri-state site-server toggles: None (use env var) /
                # True / False. The key must exist in the file AND be
                # one of those three values; anything else is treated
                # as absent so we fall through to the DEFAULTS value.
                for k in ("hosting_enabled", "auto_announce"):
                    if k in data:
                        raw = data[k]
                        if raw is None or isinstance(raw, bool):
                            self._data[k] = raw
                # Announce interval: None or a clamped positive int.
                if "announce_interval" in data:
                    raw = data["announce_interval"]
                    if raw is None:
                        self._data["announce_interval"] = None
                    elif isinstance(raw, int) and 60 <= raw <= 86400:
                        self._data["announce_interval"] = raw

                # Access fields. Strategy:
                #   1. If the file already has per-audience fields, take them.
                #   2. Otherwise seed from a preset implied by older fields:
                #        - access_mode  (current)
                #        - lockdown_node (older, three-state)
                #        - lockdown_node bool (oldest)
                #   3. Then layer old show_*_panel / show_* visibility hints.
                preset_seed = None
                raw_access = data.get("access_mode")
                raw_lock   = data.get("lockdown_node")
                if raw_access in _PRESETS:
                    preset_seed = raw_access
                elif isinstance(raw_lock, bool):
                    preset_seed = "locked" if raw_lock else "public"
                elif raw_lock in _OLD_LOCKDOWN_TO_PRESET:
                    preset_seed = _OLD_LOCKDOWN_TO_PRESET[raw_lock]
                if preset_seed:
                    self._data.update(_PRESETS[preset_seed])

                # Per-audience fields override the preset if explicitly stored.
                for k in self._BOOL_FIELDS:
                    if isinstance(data.get(k), bool):
                        self._data[k] = data[k]
                if data.get("guests_address_bar") in _ADDR_BAR_STATES:
                    self._data["guests_address_bar"] = data["guests_address_bar"]
                if data.get("users_address_bar") in _ADDR_BAR_STATES:
                    self._data["users_address_bar"] = data["users_address_bar"]
                if data.get("admins_address_bar") in _ADDR_BAR_STATES:
                    self._data["admins_address_bar"] = data["admins_address_bar"]

                # Older bool show_*_panel — layer on top of the preset.
                if isinstance(data.get("show_nodes_panel"), bool):
                    self._data["guests_nodes_panel"] = data["show_nodes_panel"]
                    self._data["users_nodes_panel"]  = data["show_nodes_panel"]
                elif data.get("show_nodes") in _OLD_SHOW_TO_BOOL:
                    val = _OLD_SHOW_TO_BOOL[data["show_nodes"]]
                    self._data["guests_nodes_panel"] = val
                    self._data["users_nodes_panel"]  = val
                if isinstance(data.get("show_messages_panel"), bool):
                    self._data["guests_messages_panel"] = data["show_messages_panel"]
                    self._data["users_messages_panel"]  = data["show_messages_panel"]
                elif data.get("show_messages") in _OLD_SHOW_TO_BOOL:
                    val = _OLD_SHOW_TO_BOOL[data["show_messages"]]
                    self._data["guests_messages_panel"] = val
                    self._data["users_messages_panel"]  = val

            log.debug("Loaded UI settings")
        except Exception as exc:
            log.warning("Could not load UI settings: %s", exc)

    def _save(self, snapshot: dict):
        try:
            os.makedirs(os.path.dirname(self._file) or ".", exist_ok=True)
            tmp = self._file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp, self._file)
            log.debug("Saved UI settings to %s", self._file)
        except Exception as exc:
            log.warning("Could not save UI settings to %s: %s", self._file, exc)

"""
Named RNS identity management.

Each identity is an independent RNS keypair stored under /config/identities/.
Identities are used to send fingerprints to NomadNet nodes — browsing itself
remains anonymous regardless of whether identities exist.

Storage layout:
    /config/identities/
        store.yml           — name/node-assignment metadata
        <hexhash>.id        — RNS binary key material
"""

import logging
import os
import time
from typing import Optional

import yaml

log = logging.getLogger(__name__)

ANNOUNCE_COOLDOWN = 3 * 3600  # 3 hours

import re as _re
_HEX_COLOR_RE = _re.compile(r'^#?([0-9a-fA-F]{6})$')

def _normalise_hex(value: str, fallback: str) -> str:
    """Return a #rrggbb-form hex string, or fallback if input is invalid."""
    if not isinstance(value, str):
        return fallback
    m = _HEX_COLOR_RE.match(value.strip())
    return ("#" + m.group(1).lower()) if m else fallback


def _default_identity_name(identity) -> str:
    """Return 'NomadPortal-XYZ' where XYZ is the last 3 hex chars of the
    identity's LXMF delivery address. Computed without registering a
    Destination, so there are no Transport-table side effects."""
    try:
        import RNS
        full_name = RNS.Destination.app_and_aspects_to_name("lxmf", "delivery")
        dest_hash = RNS.Destination.hash_from_name_and_identity(full_name, identity)
        suffix    = RNS.hexrep(dest_hash, delimit=False)[-3:]
    except Exception:
        suffix = identity.hexhash[-3:]
    return f"NomadPortal-{suffix}"


class IdentityStore:
    def __init__(self, base_dir: str):
        self._dir = os.path.join(base_dir, "identities")
        self._store_file = os.path.join(self._dir, "store.yml")
        self._data: dict = {}
        os.makedirs(self._dir, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_identities(self) -> list:
        return sorted(self._data.values(), key=lambda e: e["created"])

    def get(self, identity_id: str) -> Optional[dict]:
        return self._data.get(identity_id)

    def create(self, name: str = "", user_sub: str = "") -> dict:
        """Generate a new RNS keypair, store it, return the metadata entry.

        If `name` is empty, defaults to 'NomadPortal-<XYZ>' where XYZ is
        the last three hex chars of the new identity's LXMF address.
        """
        import RNS
        identity = RNS.Identity()
        key_file = os.path.join(self._dir, f"{identity.hexhash}.id")
        identity.to_file(key_file)
        if not name:
            name = _default_identity_name(identity)
        entry = {
            "id":        identity.hexhash,
            "name":      name,
            "key_file":  key_file,
            "nodes":     [],
            "created":   time.time(),
        }
        if user_sub:
            entry["user_sub"] = user_sub
        self._data[identity.hexhash] = entry
        self._save()
        log.info("Created identity '%s' (%s)", name, identity.hexhash[:16])
        return entry

    def ensure_for_user(self, user_sub: str, display_name: str = "") -> dict:
        """Return the identity for this user, creating one if none exists yet.

        `display_name` is accepted for API compatibility but no longer used
        for the default name — new identities auto-name based on their LXMF
        address.
        """
        for entry in self._data.values():
            if entry.get("user_sub") == user_sub:
                return entry
        return self.create("", user_sub=user_sub)

    def get_for_user(self, user_sub: str) -> Optional[dict]:
        for entry in self._data.values():
            if entry.get("user_sub") == user_sub:
                return entry
        return None

    def reset(self, identity_id: str) -> Optional[dict]:
        """Delete an identity and immediately generate a fresh keypair for the same user.

        The new identity gets a freshly auto-generated default name (different
        LXMF address → different suffix). Reset implies starting clean.
        """
        entry = self._data.get(identity_id)
        if entry is None:
            return None
        user_sub = entry.get("user_sub", "")
        self.delete(identity_id)
        return self.create("", user_sub=user_sub)

    def delete(self, identity_id: str) -> bool:
        entry = self._data.pop(identity_id, None)
        if entry is None:
            return False
        key_file = entry.get("key_file", "")
        if key_file and os.path.exists(key_file):
            os.remove(key_file)
        self._save()
        log.info("Deleted identity %s", identity_id[:16])
        return True

    def rename(self, identity_id: str, new_name: str) -> bool:
        entry = self._data.get(identity_id)
        if not entry:
            return False
        entry["name"] = new_name
        self._save()
        return True

    # ------------------------------------------------------------------
    # User icon (LXMF FIELD_ICON_APPEARANCE — vector descriptor)
    # ------------------------------------------------------------------

    def set_icon_appearance(self, identity_id: str, glyph: str, fg_hex: str, bg_hex: str) -> bool:
        """Store the user's icon descriptor: a single-char glyph and two hex colors."""
        entry = self._data.get(identity_id)
        if not entry:
            return False
        glyph  = (glyph or "?")[:8].strip() or "?"
        fg_hex = _normalise_hex(fg_hex, "#ffffff")
        bg_hex = _normalise_hex(bg_hex, "#5ba3c9")
        entry["icon"] = {"glyph": glyph, "fg": fg_hex, "bg": bg_hex}
        self._save()
        return True

    def get_icon_appearance(self, identity_id: str) -> Optional[dict]:
        entry = self._data.get(identity_id)
        return entry.get("icon") if entry else None

    def get_icon_appearance_for_user(self, user_sub: str) -> Optional[dict]:
        entry = self.get_for_user(user_sub)
        return entry.get("icon") if entry else None

    def load_rns_identity(self, identity_id: str):
        """Return the RNS.Identity object for a stored identity, or None."""
        import RNS
        entry = self._data.get(identity_id)
        if not entry:
            return None
        key_file = entry.get("key_file", "")
        if not os.path.exists(key_file):
            log.warning("Key file missing for identity %s: %s", identity_id[:16], key_file)
            return None
        try:
            return RNS.Identity.from_file(key_file)
        except Exception as exc:
            log.error("Could not load identity %s: %s", identity_id[:16], exc)
            return None

    def check_cooldown(self, identity_id: str) -> tuple[bool, str, float]:
        """Check the announce cooldown and update last_announced if allowed.

        Returns (ok, message, next_allowed_timestamp).  Does NOT actually
        send an announce — the caller is responsible for that.
        """
        entry = self._data.get(identity_id)
        if not entry:
            return False, "Identity not found", 0.0

        now = time.time()
        last = entry.get("last_announced", 0.0)
        next_allowed = last + ANNOUNCE_COOLDOWN
        if now < next_allowed:
            remaining = int(next_allowed - now)
            h, m = divmod(remaining // 60, 60)
            return False, f"Cooldown active — next announce in {h}h {m}m", next_allowed

        entry["last_announced"] = now
        self._save()
        next_allowed = now + ANNOUNCE_COOLDOWN
        log.info("Announce cooldown cleared for '%s' (%s)", entry["name"], identity_id[:16])
        return True, "ok", next_allowed

    def announce(self, identity_id: str) -> tuple[bool, str, float]:
        """Backward-compat: check cooldown then send a raw announce.

        Prefer using check_cooldown() + MessagingService.do_announce() so that
        the display name is included in app_data via the LXMRouter.
        """
        import RNS
        import RNS.vendor.umsgpack as msgpack

        ok, message, next_allowed = self.check_cooldown(identity_id)
        if not ok:
            return ok, message, next_allowed

        entry = self._data.get(identity_id)
        identity = self.load_rns_identity(identity_id)
        if identity is None:
            return False, "Identity not found or key file missing", 0.0

        try:
            dest = RNS.Destination(
                identity,
                RNS.Destination.IN,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )
            dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
            app_data = msgpack.packb([entry["name"].encode("utf-8"), 0])
            dest.announce(app_data=app_data)
            log.info("Announced identity '%s' (%s)", entry["name"], identity_id[:16])
            return True, "Announced successfully", next_allowed
        except Exception as exc:
            log.error("Announce failed for %s: %s", identity_id[:16], exc)
            return False, str(exc), 0.0

    # ------------------------------------------------------------------
    # Auto-identify (sticky toggle): which nodes should every page fetch
    # from this user identify with link.identify(). Set explicitly via
    # the address-bar fingerprint button — never automatic.
    # ------------------------------------------------------------------

    def is_identified_to(self, identity_id: str, node_hash: str) -> bool:
        entry = self._data.get(identity_id)
        if not entry:
            return False
        return node_hash.lower() in (entry.get("identified_nodes") or [])

    def set_identified(self, identity_id: str, node_hash: str, value: bool) -> bool:
        entry = self._data.get(identity_id)
        if not entry:
            return False
        nh = node_hash.lower()
        nodes = entry.setdefault("identified_nodes", [])
        if value and nh not in nodes:
            nodes.append(nh)
        elif not value and nh in nodes:
            nodes.remove(nh)
        else:
            return True
        self._save()
        return True

    def get_identified_nodes(self, identity_id: str) -> list:
        entry = self._data.get(identity_id)
        return list(entry.get("identified_nodes") or []) if entry else []

    def clear_identified_nodes(self, identity_id: str) -> None:
        """Reset the identify-on-fetch list to empty.

        Called at every login so the fingerprint toggle defaults to off
        per session — never carried over from a previous browsing window.
        """
        entry = self._data.get(identity_id)
        if not entry:
            return
        if entry.get("identified_nodes"):
            entry["identified_nodes"] = []
            self._save()

    def assign_node(self, identity_id: str, node_hash: str) -> bool:
        """Mark an identity as associated with a node (for display purposes)."""
        entry = self._data.get(identity_id)
        if not entry:
            return False
        if node_hash not in entry["nodes"]:
            entry["nodes"].append(node_hash)
            self._save()
        return True

    def unassign_node(self, identity_id: str, node_hash: str) -> bool:
        entry = self._data.get(identity_id)
        if not entry:
            return False
        if node_hash in entry["nodes"]:
            entry["nodes"].remove(node_hash)
            self._save()
        return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if os.path.exists(self._store_file):
            with open(self._store_file, "r", encoding="utf-8") as fh:
                self._data = yaml.safe_load(fh) or {}

    def _save(self) -> None:
        with open(self._store_file, "w", encoding="utf-8") as fh:
            yaml.dump(self._data, fh)

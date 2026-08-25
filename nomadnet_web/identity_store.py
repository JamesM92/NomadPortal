"""
Named RNS identity management.

Each identity is an independent RNS keypair stored under /config/identities/.
Identities are used to send fingerprints to NomadNet nodes — browsing itself
remains anonymous regardless of whether identities exist.

Multi-identity: one web account (``user_sub``) can own more than one
identity, exactly one of which is "active" at a time (persisted per
account in ``active_by_user``). ``user_sub`` on an entry always means
the *owning web account* here — unlike the NomadPortal-Android sister
project's own copy of this module, which has no real account concept
and repurposes ``user_sub`` as an opaque per-identity storage-scoping
key instead. That distinction matters: web's message history and
contacts stay scoped to the *account* (``MessageStore``/
``ContactStoreManager`` are unchanged, still keyed by the account's
``user_sub``), not to the individual identity — switching your active
identity changes which LXMF address you send/receive as, but everyone
sharing your account still sees one unified inbox and contact list
across all your identities, not a separate mailbox per identity.
Deleting an identity therefore does *not* cascade-delete any message
history, unlike the Android app's own per-identity cascade delete.

``get_for_user()``/``ensure_for_user()`` now resolve to the account's
*active* identity — every existing caller elsewhere in this codebase
that already called these two expecting "the" identity for a user
keeps working unchanged, now correctly against whichever identity is
currently active.

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


def _dest_hash_hex(identity) -> str:
    """The identity's LXMF delivery *address*, as hex — computed without
    registering a Destination, so there are no Transport-table side
    effects. A genuinely different value from the identity's own raw
    hash (identity.hexhash).

    Uses ``RNS.Destination.hash(identity, "lxmf", "delivery")`` directly
    — confirmed as exactly what ``Destination.__init__`` itself calls to
    compute ``self.hash``. This function used to go through
    ``RNS.Destination.app_and_aspects_to_name(...)``, which doesn't
    exist on the installed RNS version at all (confirmed directly
    against its source — no such method on ``Destination``); every call
    silently raised ``AttributeError``, caught by a broad
    ``except Exception``, and fell through to an ``identity.hexhash``
    suffix fallback — the identity's own raw hash, not its real LXMF
    address. Harmless in practice for ``_default_identity_name``'s
    single caller (only needs *some* stable per-identity hex string),
    but genuinely wrong for anything that wants the real address —
    exactly the bug already found and fixed on the NomadPortal-Android
    sister project's own copy of this helper."""
    import RNS
    dest_hash = RNS.Destination.hash(identity, "lxmf", "delivery")
    return RNS.hexrep(dest_hash, delimit=False)


def _default_identity_name(identity) -> str:
    """Return 'NomadPortal-XYZ' where XYZ is the last 3 hex chars of the
    identity's LXMF delivery address."""
    try:
        suffix = _dest_hash_hex(identity)[-3:]
    except Exception:
        suffix = identity.hexhash[-3:]
    return f"NomadPortal-{suffix}"


class IdentityStore:
    def __init__(self, base_dir: str):
        self._dir = os.path.join(base_dir, "identities")
        self._store_file = os.path.join(self._dir, "store.yml")
        self._data: dict = {}
        # web-account user_sub -> identity_id currently active for that
        # account. See module docstring for the multi-identity model.
        self._active_by_user: dict = {}
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
        # A brand-new account's very first identity becomes active by
        # construction — every existing single-identity account already
        # satisfies this the moment its one identity is created, no
        # separate bootstrap step needed. An account's 2nd+ identity
        # does NOT become active automatically — same "create, then
        # explicitly switch" split the NomadPortal-Android sister
        # project's own create_identity() uses.
        if user_sub and not self._active_by_user.get(user_sub):
            self._active_by_user[user_sub] = identity.hexhash
        self._save()
        log.info("Created identity '%s' (%s)", name, identity.hexhash[:16])
        return entry

    def import_identity(self, key_bytes: bytes, name: str = "", user_sub: str = "") -> dict:
        """Import an existing RNS keypair from raw bytes (a `.identity`
        file's real on-disk contents) as a new identity owned by
        [user_sub]. Same on-disk format `RNS.Identity.to_file()` writes
        and `create()` already uses — confirmed cross-compatible with
        Columba's own `.identity` export/import (its
        `IdentityFileReader.kt` expects the identical raw private-key
        byte layout), so a file exported from either app imports
        cleanly here.

        Does NOT switch to it — same "import, then explicitly activate"
        split as `create()`'s own single-active-per-account behavior
        (still becomes active automatically if it's this account's very
        first identity, via the same `create()`-style bootstrap).

        Raises ValueError if [key_bytes] isn't a valid RNS identity, or
        if this exact keypair already exists under a *different*
        account (importing someone else's identity file must not
        silently grant your account access to their identity's
        metadata). Re-importing a keypair your own account already
        owns is a no-op — returns the existing entry unchanged.
        """
        import RNS
        identity = RNS.Identity.from_bytes(bytes(key_bytes))
        if identity is None:
            raise ValueError("Not a valid Reticulum identity file")

        existing = self._data.get(identity.hexhash)
        if existing is not None:
            if existing.get("user_sub") != user_sub:
                raise ValueError("This identity already belongs to another account")
            return existing

        key_file = os.path.join(self._dir, f"{identity.hexhash}.id")
        identity.to_file(key_file)
        entry = {
            "id":        identity.hexhash,
            "name":      name or _default_identity_name(identity),
            "key_file":  key_file,
            "nodes":     [],
            "created":   time.time(),
        }
        if user_sub:
            entry["user_sub"] = user_sub
        self._data[identity.hexhash] = entry
        if user_sub and not self._active_by_user.get(user_sub):
            self._active_by_user[user_sub] = identity.hexhash
        self._save()
        log.info("Imported identity '%s' (%s)", entry["name"], identity.hexhash[:16])
        return entry

    def export_key_bytes(self, identity_id: str) -> Optional[bytes]:
        """Raw bytes of this identity's own `.id` key file, for a real
        `.identity` export — the counterpart to `import_identity`
        above. None if the identity or its key file doesn't exist."""
        entry = self._data.get(identity_id)
        if not entry:
            return None
        key_file = entry.get("key_file", "")
        if not key_file or not os.path.exists(key_file):
            return None
        with open(key_file, "rb") as fh:
            return fh.read()

    def get_dest_hash_hex(self, identity_id: str) -> Optional[str]:
        """The real LXMF delivery address for a stored identity, computed
        live rather than trusting a cached/stale value. None if the
        identity doesn't exist or its key file can't be loaded."""
        identity = self.load_rns_identity(identity_id)
        if identity is None:
            return None
        return _dest_hash_hex(identity)

    # ------------------------------------------------------------------
    # Multi-identity: per-account ownership and active-identity tracking
    # ------------------------------------------------------------------

    def list_for_user(self, user_sub: str) -> list:
        """All identities owned by this account, oldest first."""
        return sorted(
            (e for e in self._data.values() if e.get("user_sub") == user_sub),
            key=lambda e: e["created"],
        )

    def list_active_identities(self) -> list:
        """One entry per distinct owning account — whichever identity is
        currently active for it. General-purpose utility: anywhere that
        wants "the one identity per account that's currently presented"
        rather than every identity every account owns
        (`list_identities()`). Not used by
        `MessagingService.setup_delivery()` — that brings up a live
        router for *every* identity every account owns (so messages can
        be received on any of them), only gating each identity's own
        announce on whether it's the active one — see that method's own
        doc comment."""
        out = []
        for user_sub in {e.get("user_sub") for e in self._data.values() if e.get("user_sub")}:
            entry = self.get_active_for_user(user_sub)
            if entry is not None:
                out.append(entry)
        return out

    def get_active_for_user(self, user_sub: str) -> Optional[dict]:
        """The identity currently active for this account.

        Self-healing: if the account has no active identity recorded
        yet, or the recorded one no longer exists / no longer belongs
        to this account (e.g. it was deleted from under an in-flight
        session), falls back to the account's earliest-owned identity
        and persists that as the new active one — the same "always
        resolves to something real, no explicit migration step needed"
        contract `get_for_user()` always had before multi-identity
        existed. Returns None only if the account owns no identity at
        all yet.
        """
        active_id = self._active_by_user.get(user_sub)
        if active_id:
            entry = self._data.get(active_id)
            if entry is not None and entry.get("user_sub") == user_sub:
                return entry

        owned = self.list_for_user(user_sub)
        if not owned:
            return None
        fallback = owned[0]
        self._active_by_user[user_sub] = fallback["id"]
        self._save()
        return fallback

    def set_active_for_user(self, user_sub: str, identity_id: str) -> bool:
        """Switch which of this account's identities is active. False if
        [identity_id] doesn't exist or isn't owned by [user_sub] — never
        lets one account activate another account's identity. True
        (no-op, no save) if it's already the active one."""
        entry = self._data.get(identity_id)
        if entry is None or entry.get("user_sub") != user_sub:
            return False
        if self._active_by_user.get(user_sub) == identity_id:
            return True
        self._active_by_user[user_sub] = identity_id
        self._save()
        return True

    def create_for_user(self, user_sub: str, name: str = "") -> dict:
        """Create a new identity owned by [user_sub]. Does not switch to
        it unless it's the account's first identity (see `create()`)."""
        return self.create(name, user_sub=user_sub)

    def delete_for_user(self, user_sub: str, identity_id: str) -> tuple[bool, str]:
        """Delete one of [user_sub]'s own identities.

        Refuses (False, reason) if the identity isn't owned by this
        account, or if it's the account's *only* identity — unlike the
        NomadPortal-Android sister project's own `delete_identity()`,
        which auto-creates a fresh replacement when the last one goes,
        this deliberately does not spontaneously hand a web account a
        brand-new identity it never asked for; the caller must create
        one first if they want a replacement. If the deleted identity
        was the active one, active status moves to the account's
        earliest remaining identity.
        """
        entry = self._data.get(identity_id)
        if entry is None or entry.get("user_sub") != user_sub:
            return False, "Identity not found"
        owned = self.list_for_user(user_sub)
        if len(owned) <= 1:
            return False, "Can't delete your only identity"

        was_active = self._active_by_user.get(user_sub) == identity_id
        self.delete(identity_id)  # pops + saves

        if was_active:
            remaining = self.list_for_user(user_sub)
            self._active_by_user[user_sub] = remaining[0]["id"] if remaining else None
            self._save()
        return True, "ok"

    def ensure_for_user(self, user_sub: str, display_name: str = "") -> dict:
        """Return this account's active identity, creating one if the
        account owns none yet.

        `display_name` is accepted for API compatibility but no longer used
        for the default name — new identities auto-name based on their LXMF
        address.
        """
        existing = self.get_active_for_user(user_sub)
        if existing is not None:
            return existing
        return self.create("", user_sub=user_sub)

    def get_for_user(self, user_sub: str) -> Optional[dict]:
        """Back-compat alias: this account's currently *active* identity.

        Every pre-multi-identity caller in this codebase already means
        "the identity for this user" when it calls this — now correctly
        resolves to whichever identity is active, no call-site changes
        needed. Prefer `get_active_for_user()` in new code for clarity.
        """
        return self.get_active_for_user(user_sub)

    def reset(self, identity_id: str) -> Optional[dict]:
        """Delete an identity and immediately generate a fresh keypair for
        the same account, keeping it active if the deleted one was.
        Unlike `delete_for_user()`, this is allowed even when it's the
        account's only identity — reset implies starting clean, not
        removal, so "always end up with at least one" still holds.
        """
        entry = self._data.get(identity_id)
        if entry is None:
            return None
        user_sub = entry.get("user_sub", "")
        was_active = user_sub and self._active_by_user.get(user_sub) == identity_id
        self.delete(identity_id)
        fresh = self.create("", user_sub=user_sub)
        if was_active:
            self._active_by_user[user_sub] = fresh["id"]
            self._save()
        return fresh

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
        """Store the user's icon descriptor: an icon glyph and two hex
        colors. ``glyph`` is either a real Material Design Icons name
        (kebab-case, e.g. ``"account-supervisor-outline"`` — up to 40
        chars for the longest real MDI names, so 64 leaves headroom) or,
        for a name the picker's search didn't match against the real
        catalog, a short fallback string whose first character renders
        as a plain letter glyph instead (see mdi_icons.py /
        messaging.py's _render_appearance_svg)."""
        entry = self._data.get(identity_id)
        if not entry:
            return False
        glyph  = (glyph or "?")[:64].strip() or "?"
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
        if not os.path.exists(self._store_file):
            return
        with open(self._store_file, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if "identities" in raw and isinstance(raw.get("identities"), dict):
            # Current on-disk shape: {"identities": {hexhash: entry,
            # ...}, "active_by_user": {user_sub: identity_id, ...}}.
            self._data = raw["identities"]
            self._active_by_user = raw.get("active_by_user") or {}
        else:
            # Every store.yml written before multi-identity existed is
            # just the flat {hexhash: entry, ...} dict directly at the
            # top level — [raw] itself, no wrapper. Loaded as-is (no
            # explicit migration step needed); get_active_for_user()'s
            # own fallback-to-earliest-owned-identity logic populates
            # _active_by_user the first time each account is looked up,
            # and the next _save() call naturally rewrites the file in
            # the current wrapped shape.
            self._data = raw
            self._active_by_user = {}

    def _save(self) -> None:
        with open(self._store_file, "w", encoding="utf-8") as fh:
            yaml.dump({"identities": self._data, "active_by_user": self._active_by_user}, fh)

"""Named contact management with favorites."""

import hashlib
import logging
import os
import threading
import time
from typing import Optional

import yaml

log = logging.getLogger(__name__)

# How much a user trusts a peer (NomadNet 1.2.0's conversation tabs). A peer
# nobody has classified is "unknown" and is listed with the untrusted ones.
TRUST_LEVELS = ("unknown", "trusted", "untrusted")
MAX_NOTE_LEN = 1024


class ContactStore:
    def __init__(self, base_dir: str, filename: str = "contacts.yml"):
        os.makedirs(base_dir, exist_ok=True)
        self._path = os.path.join(base_dir, filename)
        self._lock = threading.Lock()
        self._data: dict = {}  # keyed by hash_hex
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_contacts(self) -> list:
        with self._lock:
            return sorted(self._data.values(), key=lambda e: e.get("name", "").lower())

    def favorites(self) -> list:
        with self._lock:
            return [c for c in self._data.values() if c.get("favorited")]

    def get(self, hash_hex: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(hash_hex)

    def upsert(self, hash_hex: str, name: str = "", note: str = "") -> dict:
        with self._lock:
            entry = self._data.get(hash_hex)
            if entry is None:
                entry = {
                    "hash":      hash_hex,
                    "name":      name or hash_hex[:16],
                    "note":      note,
                    "favorited": False,
                    "created":   time.time(),
                    "updated":   time.time(),
                }
                self._data[hash_hex] = entry
                log.info("Added contact %s", hash_hex[:16])
            else:
                if name:
                    entry["name"] = name
                if note:
                    entry["note"] = note
                entry["updated"] = time.time()
            snapshot = dict(self._data)
        self._persist(snapshot)
        return entry

    def delete(self, hash_hex: str) -> bool:
        with self._lock:
            if hash_hex not in self._data:
                return False
            del self._data[hash_hex]
            snapshot = dict(self._data)
        self._persist(snapshot)
        log.info("Deleted contact %s", hash_hex[:16])
        return True

    def set_icon(self, hash_hex: str, icon_b64: str, icon_mime: str = "image/png") -> None:
        """Store or update a contact's icon (base64-encoded bytes from FIELD_ICON_APPEARANCE)."""
        with self._lock:
            entry = self._data.get(hash_hex)
            if entry is None:
                entry = {
                    "hash":      hash_hex,
                    "name":      hash_hex[:16],
                    "note":      "",
                    "favorited": False,
                    "created":   time.time(),
                    "updated":   time.time(),
                }
                self._data[hash_hex] = entry
                log.info("Added contact (icon) %s", hash_hex[:16])
            if entry.get("icon") != icon_b64:
                entry["icon"]      = icon_b64
                entry["icon_mime"] = icon_mime
                entry["updated"]   = time.time()
            snapshot = dict(self._data)
        self._persist(snapshot)

    def set_favorite(self, hash_hex: str, favorited: bool) -> bool:
        with self._lock:
            entry = self._data.get(hash_hex)
            if entry is None:
                return False
            entry["favorited"] = favorited
            entry["updated"] = time.time()
            snapshot = dict(self._data)
        self._persist(snapshot)
        return True

    def set_blocked(self, hash_hex: str, blocked: bool) -> dict:
        """Unlike set_favorite, works even for a sender with no existing
        contact record — you can want to block someone you've never
        explicitly added, whereas favoriting an unknown sender doesn't
        make sense the same way. Auto-creates a minimal record on
        block, same "don't require a pre-existing record for a
        legitimate user action" reasoning as favoriting a never-
        announced node. Returns the (possibly newly-created) entry.
        """
        with self._lock:
            entry = self._data.get(hash_hex)
            if entry is None:
                entry = {
                    "hash":      hash_hex,
                    "name":      hash_hex[:16],
                    "note":      "",
                    "favorited": False,
                    "created":   time.time(),
                    "updated":   time.time(),
                }
                self._data[hash_hex] = entry
                log.info("Added contact (block) %s", hash_hex[:16])
            entry["blocked"] = blocked
            entry["updated"] = time.time()
            snapshot = dict(self._data)
        self._persist(snapshot)
        log.info("%s %s", "Blocked" if blocked else "Unblocked", hash_hex[:16])
        return entry

    def set_peer_settings(self, hash_hex: str, trust=None, pinned=None, note=None) -> dict:
        """Change how this user treats a peer: ``trust`` (one of TRUST_LEVELS),
        ``pinned`` (conversation sorts first) and ``note`` (free text).
        Fields left as None are not touched. Like blocking, this works for a
        peer with no contact record yet (one is created), because classifying
        someone you only know from an incoming message is the normal case.
        Raises ValueError for a value that is not allowed.
        """
        if trust is not None and trust not in TRUST_LEVELS:
            raise ValueError(f"trust must be one of {TRUST_LEVELS}")
        if pinned is not None and not isinstance(pinned, bool):
            raise ValueError("pinned must be true or false")
        if note is not None:
            if not isinstance(note, str):
                raise ValueError("note must be text")
            note = note.strip()
            if len(note) > MAX_NOTE_LEN:
                raise ValueError(f"note exceeds {MAX_NOTE_LEN} characters")
        with self._lock:
            entry = self._data.get(hash_hex)
            if entry is None:
                entry = {
                    "hash":      hash_hex,
                    "name":      hash_hex[:16],
                    "note":      "",
                    "favorited": False,
                    "created":   time.time(),
                    "updated":   time.time(),
                }
                self._data[hash_hex] = entry
                log.info("Added contact (settings) %s", hash_hex[:16])
            entry.setdefault("trust", "unknown")
            entry.setdefault("pinned", False)
            if trust is not None:
                entry["trust"] = trust
            if pinned is not None:
                entry["pinned"] = pinned
            if note is not None:
                entry["note"] = note
            entry["updated"] = time.time()
            snapshot = dict(self._data)
            result = dict(entry)
        self._persist(snapshot)
        return result

    def peer_view(self, hash_hex: str) -> dict:
        """The three peer settings with defaults filled in, for records
        written before they existed."""
        with self._lock:
            entry = self._data.get(hash_hex) or {}
            return {
                "trust":  entry.get("trust", "unknown"),
                "pinned": bool(entry.get("pinned", False)),
                "note":   entry.get("note", "") or "",
            }

    def is_blocked(self, hash_hex: str) -> bool:
        with self._lock:
            entry = self._data.get(hash_hex)
            return bool(entry and entry.get("blocked"))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            with self._lock:
                self._data = data
            log.info("Loaded %d contacts", len(self._data))
        except Exception as exc:
            log.warning("Could not load contacts: %s", exc)

    def _persist(self, snapshot: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                yaml.dump(snapshot, fh, allow_unicode=True)
            os.replace(tmp, self._path)
        except Exception as exc:
            log.warning("Could not save contacts: %s", exc)


class ContactStoreManager:
    """Manages one ContactStore per user, stored under <base_dir>/contacts/."""

    def __init__(self, base_dir: str):
        self._contacts_dir = os.path.join(base_dir, "contacts")
        os.makedirs(self._contacts_dir, exist_ok=True)
        self._stores: dict = {}
        self._lock = threading.Lock()

    def for_user(self, user_sub: str) -> ContactStore:
        with self._lock:
            store = self._stores.get(user_sub)
        if store is not None:
            return store
        key = hashlib.sha256(user_sub.encode()).hexdigest()[:16]
        store = ContactStore(self._contacts_dir, filename=f"u_{key}.yml")
        with self._lock:
            self._stores[user_sub] = store
        return store

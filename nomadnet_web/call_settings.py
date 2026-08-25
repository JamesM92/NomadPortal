"""Per-account voice-call settings: "allow incoming calls" (master
toggle) and "calls from contacts only". Same per-user YAML-file-per-
account storage shape as contact_store.py's ContactStoreManager (one
small file per account under <base_dir>/call_settings/, keyed by a
truncated SHA-256 of the account's own user_sub — same convention, not
the raw user_sub, for the same "don't put an OIDC subject claim
verbatim into a filename" reasoning that store already established).

Defaults intentionally match CallManager's own real defaults exactly
(calls_enabled=False, contacts_only=True) — see that class's own
__init__ doc comment for why that specific combination, not either
extreme, is the safe starting point. A settings file that predates a
field just falls back to the default for it, no migration needed.
"""

import hashlib
import logging
import os
import threading

import yaml

log = logging.getLogger(__name__)

DEFAULT_CALLS_ENABLED = False
DEFAULT_CONTACTS_ONLY = True


class CallSettings:
    def __init__(self, base_dir: str, filename: str = "settings.yml"):
        os.makedirs(base_dir, exist_ok=True)
        self._path = os.path.join(base_dir, filename)
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    def get_calls_enabled(self) -> bool:
        with self._lock:
            return bool(self._data.get("calls_enabled", DEFAULT_CALLS_ENABLED))

    def get_contacts_only(self) -> bool:
        with self._lock:
            return bool(self._data.get("contacts_only", DEFAULT_CONTACTS_ONLY))

    def set_calls_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._data["calls_enabled"] = bool(enabled)
            snapshot = dict(self._data)
        self._persist(snapshot)

    def set_contacts_only(self, enabled: bool) -> None:
        with self._lock:
            self._data["contacts_only"] = bool(enabled)
            snapshot = dict(self._data)
        self._persist(snapshot)

    def as_dict(self) -> dict:
        return {
            "calls_enabled": self.get_calls_enabled(),
            "contacts_only": self.get_contacts_only(),
        }

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                self._data = yaml.safe_load(fh) or {}
        except Exception as exc:
            log.warning("Could not load call settings: %s", exc)

    def _persist(self, snapshot: dict) -> None:
        try:
            tmp = f"{self._path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                yaml.dump(snapshot, fh)
            os.replace(tmp, self._path)
        except Exception as exc:
            log.warning("Could not save call settings: %s", exc)


class CallSettingsManager:
    """Manages one CallSettings per account, stored under
    <base_dir>/call_settings/."""

    def __init__(self, base_dir: str):
        self._dir = os.path.join(base_dir, "call_settings")
        os.makedirs(self._dir, exist_ok=True)
        self._stores: dict = {}
        self._lock = threading.Lock()

    def for_user(self, user_sub: str) -> CallSettings:
        with self._lock:
            store = self._stores.get(user_sub)
        if store is not None:
            return store
        key = hashlib.sha256(user_sub.encode()).hexdigest()[:16]
        store = CallSettings(self._dir, filename=f"u_{key}.yml")
        with self._lock:
            self._stores[user_sub] = store
        return store

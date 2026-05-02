"""
Persistent OIDC user registry.

Tracks every user who has logged in via OIDC, stores their enabled/disabled
state, and enforces the "new users enabled by default" policy.

Local admin accounts (username/password) are never stored here and are
always permitted — this store only applies to OIDC logins.

Storage: /config/users.yml
"""

import logging
import os
import time
import threading
from typing import Optional

import yaml
from werkzeug.security import generate_password_hash, check_password_hash

log = logging.getLogger(__name__)


class UserStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = {
            "settings": {"new_users_enabled": True},
            "users": {},
        }
        self._load()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @property
    def new_users_enabled(self) -> bool:
        return self._data["settings"].get("new_users_enabled", True)

    @new_users_enabled.setter
    def new_users_enabled(self, value: bool) -> None:
        with self._lock:
            self._data["settings"]["new_users_enabled"] = bool(value)
            self._save()
        log.info("New-user default changed: enabled=%s", value)

    # ------------------------------------------------------------------
    # User lifecycle
    # ------------------------------------------------------------------

    def register_or_update(
        self, sub: str, email: str, name: str
    ) -> tuple[dict, bool]:
        """Record a login attempt.  Returns (user_record, is_new_user).

        New users inherit the new_users_enabled policy.
        Existing users have their name/email refreshed but enabled state preserved.
        """
        with self._lock:
            existing = self._data["users"].get(sub)
            if existing:
                existing["email"]     = email
                existing["name"]      = name
                existing["last_seen"] = time.time()
                self._save()
                return dict(existing), False

            record = {
                "sub":         sub,
                "email":       email,
                "name":        name,
                "enabled":     self.new_users_enabled,
                "first_seen":  time.time(),
                "last_seen":   time.time(),
            }
            self._data["users"][sub] = record
            self._save()
            log.info(
                "New OIDC user registered: %s (%s) enabled=%s",
                name, email, record["enabled"],
            )
            return dict(record), True

    def is_enabled(self, sub: str) -> bool:
        with self._lock:
            record = self._data["users"].get(sub)
            if record is None:
                return self.new_users_enabled
            return record.get("enabled", True)

    def set_enabled(self, sub: str, enabled: bool) -> bool:
        with self._lock:
            record = self._data["users"].get(sub)
            if record is None:
                return False
            record["enabled"] = bool(enabled)
            self._save()
        log.info("User %s (%s) enabled=%s", record.get("name"), sub[:16], enabled)
        return True

    def set_admin(self, sub: str, is_admin: bool) -> bool:
        with self._lock:
            record = self._data["users"].get(sub)
            if record is None:
                return False
            record["is_admin"] = bool(is_admin)
            self._save()
        log.info("User %s (%s) is_admin=%s", record.get("name"), sub[:16], is_admin)
        return True

    def create_local_user(
        self, username: str, password: str, is_admin: bool = False
    ) -> tuple[Optional[dict], str]:
        """Create a local (non-OIDC) user. Returns (record, error_string)."""
        sub = f"local:{username}"
        with self._lock:
            if sub in self._data["users"]:
                return None, "Username already exists"
            if not username.strip():
                return None, "Username is required"
            if len(password) < 8:
                return None, "Password must be at least 8 characters"
            record = {
                "sub":           sub,
                "email":         "",
                "name":          username,
                "enabled":       True,
                "is_admin":      is_admin,
                "local":         True,
                "password_hash": generate_password_hash(password),
                "first_seen":    time.time(),
                "last_seen":     time.time(),
            }
            self._data["users"][sub] = record
            self._save()
        log.info("Created local user %s (admin=%s)", username, is_admin)
        return dict(record), ""

    def authenticate_local(self, username: str, password: str) -> Optional[dict]:
        """Verify a local user's password. Returns the record or None."""
        sub = f"local:{username}"
        with self._lock:
            record = self._data["users"].get(sub)
        if record is None or not record.get("local"):
            return None
        if not record.get("enabled", True):
            return None
        if not check_password_hash(record.get("password_hash", ""), password):
            return None
        return dict(record)

    def delete_user(self, sub: str) -> bool:
        with self._lock:
            if sub not in self._data["users"]:
                return False
            del self._data["users"][sub]
            self._save()
        log.info("Deleted user %s", sub[:32])
        return True

    def list_users(self) -> list:
        with self._lock:
            return sorted(
                self._data["users"].values(),
                key=lambda u: u["last_seen"],
                reverse=True,
            )

    def get_user(self, sub: str) -> Optional[dict]:
        with self._lock:
            return dict(self._data["users"][sub]) if sub in self._data["users"] else None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        self._data["settings"] = raw.get("settings", {"new_users_enabled": True})
        self._data["users"]    = raw.get("users", {})

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as fh:
            yaml.dump(self._data, fh, default_flow_style=False, allow_unicode=True)

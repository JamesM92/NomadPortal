"""The administrator account, kept by the app instead of in an environment
variable.

First start: there is no account, so the sign-in page offers "Set up your
admin account". Finishing setup needs a one-time code that the app writes to
a private file in the config directory (``admin_setup_code``). Only someone
who can read that file, i.e. the person running the container, can claim the
account. The code is never logged or shown on a web page, and the file is
deleted once the account exists.

The password is stored as a salted hash in ``admin_account.json`` (owner-only
permissions). It is never editable from the web: the only way to change it is
``python -m nomadnet_web.admin_cli reset-password`` inside the container.
That command runs in a different process, so every check re-reads the file,
and ``changed_at()`` lets the web app drop sessions older than the reset.

There is deliberately no "forgot password" by email or web. Proof of access to
the container is the recovery path.
"""

import hmac
import json
import os
import re
import secrets
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD = 12
MAX_PASSWORD = 256
CODE_ATTEMPTS = 5
CODE_LOCKOUT_S = 15 * 60

_USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I


def _normalise_code(text) -> str:
    return re.sub(r"[\s-]+", "", str(text or "")).upper()


class AdminAccount:
    def __init__(self, config_dir: str, clock=time.time):
        self._dir = config_dir
        self._clock = clock
        self._lock = threading.RLock()
        self._code = None
        self._failures = []                    # times of wrong setup codes
        self.account_path = os.path.join(config_dir, "admin_account.json")
        self.setup_code_path = os.path.join(config_dir, "admin_setup_code")

    # ---- reading --------------------------------------------------------

    def _read(self):
        try:
            with open(self.account_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if (isinstance(data, dict) and isinstance(data.get("username"), str)
                and isinstance(data.get("password_hash"), str) and data["password_hash"]):
            return data
        return None

    def configured(self) -> bool:
        return self._read() is not None

    def username(self):
        data = self._read()
        return data["username"] if data else None

    def changed_at(self) -> float:
        data = self._read()
        return float(data.get("changed_at", 0)) if data else 0.0

    def verify(self, username: str, password: str) -> bool:
        data = self._read()
        if data is None:
            return False
        name_ok = hmac.compare_digest(str(username or "").encode(), data["username"].encode())
        pass_ok = check_password_hash(data["password_hash"], str(password or ""))
        return bool(name_ok and pass_ok)

    # ---- the setup code -------------------------------------------------

    def _write_private(self, path: str, text: str) -> None:
        os.makedirs(self._dir, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)

    def ensure_setup_code(self) -> bool:
        """True when setup is still needed (and a code file exists for it);
        False when an account exists (and any leftover code file is removed)."""
        with self._lock:
            if self.configured():
                self._code = None
                self._remove_code_file()
                return False
            if self._code is None:
                groups = ["".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(4)]
                self._code = "-".join(groups)
                self._write_private(self.setup_code_path, self._code + "\n")
            return True

    def _remove_code_file(self) -> None:
        try:
            os.remove(self.setup_code_path)
        except OSError:
            pass

    # ---- writing --------------------------------------------------------

    @staticmethod
    def _check_username(name: str) -> str:
        return "" if _USERNAME_RE.fullmatch(name or "") else \
            "Username: 1 to 64 letters, digits, dots, dashes or underscores."

    @staticmethod
    def _check_password(password: str, username: str) -> str:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            return f"Password: at least {MIN_PASSWORD} characters."
        if len(password) > MAX_PASSWORD:
            return f"Password: at most {MAX_PASSWORD} characters."
        if password.lower() == (username or "").lower():
            return "Password: must not be the same as the username."
        return ""

    def _store(self, username: str, password: str) -> None:
        self._write_private(self.account_path, json.dumps({
            "username": username,
            "password_hash": generate_password_hash(password),
            "changed_at": self._clock(),
        }))

    def complete_setup(self, code: str, username: str, password: str) -> str:
        """Create the account. Returns "" on success, otherwise a plain-words
        reason. Needs the one-time code; refuses once an account exists."""
        with self._lock:
            if self.configured():
                return "An admin account already exists."
            now = self._clock()
            self._failures = [t for t in self._failures if now - t < CODE_LOCKOUT_S]
            if len(self._failures) >= CODE_ATTEMPTS:
                return "Too many wrong codes. Try again in a few minutes."
            if self._code is None or not hmac.compare_digest(
                    _normalise_code(code).encode(), _normalise_code(self._code).encode()):
                self._failures.append(now)
                return "That setup code is not right. It is in the admin_setup_code file in the config directory."
            username = (username or "").strip()
            err = self._check_username(username) or self._check_password(password, username)
            if err:
                return err
            self._store(username, password)
            self._code = None
            self._remove_code_file()
            return ""

    def set_password(self, password: str, username: str = None) -> str:
        """Console reset. Returns "" on success or a reason. Keeps the current
        username unless ``username`` is given; a fresh install needs one."""
        with self._lock:
            current = self.username()
            name = (username.strip() if username is not None else current) or ""
            if not name:
                return "Username: give one with --username (there is no account yet)."
            err = self._check_username(name) or self._check_password(password, name)
            if err:
                return err
            self._store(name, password)
            self._code = None
            self._remove_code_file()
            return ""

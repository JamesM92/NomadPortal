"""Hosting an LXMF propagation node (see docs/design/propagation-hosting.md).

One dedicated ``LXMRouter`` with its own identity, switched on and off by the
operator. The module owns: the validated settings (a small JSON file in the
node's own directory), the mapping from those settings to the router's
arguments, the refusal to run on network storage, the lifecycle, periodic
re-announce, and a display-ready status.

LXMF cannot tear a router down once it exists (its job thread and Reticulum
destinations live until the process exits), so at most one router is ever
created per run. Switching the node on and off reuses it; a setting that is
a constructor argument is saved at once and takes effect at the next restart
(``status()["restart_required"]`` says so). Only the storage cap is live.

``lxmf`` and ``rns`` are passed in, so tests use fakes and need no network.
"""

import json
import os
import re
import threading
import time

REANNOUNCE_INTERVAL_S = 6 * 60 * 60
MANUAL_ANNOUNCE_COOLDOWN_S = 60

DEFAULTS = {
    "enabled": False,
    "name": "",
    "storage_mb": 500,
    "transfer_limit_kb": 256,
    "autopeer": False,
    "max_peers": 20,
    "autopeer_maxdepth": 4,
    "static_peers": [],
    "only_static": False,
    "stamp_cost": 16,
}

_HEX32 = re.compile(r"[0-9a-f]{32}")
_NETWORK_FS = ("nfs", "nfs4", "cifs", "smb", "smbfs", "smb3", "sshfs", "fuse.sshfs",
               "9p", "afs", "ceph", "glusterfs", "fuse.glusterfs", "davfs", "fuse.s3fs")

# Constructor arguments: changing one needs a new router, i.e. a restart.
_ROUTER_FIELDS = ("name", "transfer_limit_kb", "autopeer", "max_peers", "autopeer_maxdepth",
                  "static_peers", "only_static", "stamp_cost")


def _int_in(value, lo, hi):
    if isinstance(value, bool):
        raise ValueError
    n = int(value)
    if not lo <= n <= hi:
        raise ValueError
    return n


def clean_settings(patch, current):
    """Apply ``patch`` to ``current``. Returns ``(settings, errors)``. A bad
    value is reported and the old one kept; unknown keys are ignored."""
    out = dict(current)
    out["static_peers"] = list(current.get("static_peers", []))
    errors = []
    patch = patch or {}

    def number(key, lo, hi):
        if key in patch:
            try:
                out[key] = _int_in(patch[key], lo, hi)
            except (TypeError, ValueError):
                errors.append(f"{key}: must be a whole number from {lo} to {hi}.")

    def flag(key):
        if key in patch:
            out[key] = bool(patch[key])

    flag("enabled")
    flag("autopeer")
    number("storage_mb", 1, 1_000_000)
    number("transfer_limit_kb", 1, 100_000)
    number("max_peers", 0, 1000)
    number("autopeer_maxdepth", 1, 32)
    number("stamp_cost", 0, 32)
    if "name" in patch:
        name = str(patch["name"]).strip()
        if len(name) > 64:
            errors.append("name: at most 64 characters.")
        else:
            out["name"] = name
    if "static_peers" in patch:
        seen = []
        for raw in patch["static_peers"] if isinstance(patch["static_peers"], (list, tuple)) else []:
            h = str(raw).strip().lower()
            if _HEX32.fullmatch(h) and h not in seen:
                seen.append(h)
        out["static_peers"] = seen
    if "only_static" in patch:
        want = bool(patch["only_static"])
        if want and not out["static_peers"]:
            errors.append("only_static: add at least one static peer first.")
        else:
            out["only_static"] = want
    if out["only_static"] and not out["static_peers"]:
        out["only_static"] = False
    return out, errors


def router_kwargs(s):
    return {
        "name": s["name"] or None,
        "propagation_limit": s["transfer_limit_kb"],
        "autopeer": s["autopeer"],
        "max_peers": s["max_peers"],
        "autopeer_maxdepth": s["autopeer_maxdepth"],
        "static_peers": [bytes.fromhex(h) for h in s["static_peers"]],
        "from_static_only": s["only_static"],
        "propagation_cost": s["stamp_cost"],
    }


def is_network_mount(path, mounts_text):
    """True when ``path`` lies under a network filesystem in ``/proc/mounts``
    text. Longest matching mount point wins, so a local mount inside a network
    one is judged correctly."""
    best, best_type = "", ""
    target = os.path.abspath(path)
    for line in (mounts_text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt, fstype = parts[1], parts[2]
        if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
            best, best_type = mnt, fstype
    return best_type in _NETWORK_FS


def _read_mounts():
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


class PropagationHost:
    def __init__(self, data_dir, lxmf, rns, mounts_text=None, clock=time.time):
        self._dir = data_dir
        self._lxmf = lxmf
        self._rns = rns
        self._mounts_text = mounts_text
        self._clock = clock
        self._lock = threading.RLock()
        self._router = None
        self._router_args = None
        self._error = None
        self._last_announce = 0.0
        self._last_manual = 0.0
        self._settings = self._load()

    # ---- settings -------------------------------------------------------

    def _path(self, name):
        return os.path.join(self._dir, name)

    def _load(self):
        try:
            with open(self._path("settings.json"), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError
            s, _ = clean_settings(data, DEFAULTS)
            return s
        except (OSError, ValueError):
            return json.loads(json.dumps(DEFAULTS))

    def _save(self):
        os.makedirs(self._dir, exist_ok=True)
        tmp = self._path("settings.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._settings, fh, indent=2)
        os.replace(tmp, self._path("settings.json"))

    def settings(self):
        with self._lock:
            return json.loads(json.dumps(self._settings))

    def update(self, patch):
        """Validate and save ``patch``, then bring the node in line with it."""
        with self._lock:
            new, errors = clean_settings(patch, self._settings)
            if new != self._settings:
                self._settings = new
                self._save()
            self.apply()
            return {"errors": errors, "settings": self.settings(), "status": self.status()}

    # ---- lifecycle ------------------------------------------------------

    def _identity(self):
        os.makedirs(self._dir, exist_ok=True)
        path = self._path("identity")
        ident = self._rns.Identity.from_file(path) if os.path.exists(path) else None
        if ident is None:
            ident = self._rns.Identity()
            ident.to_file(path)
        return ident

    def apply(self):
        """Make the running state match the settings. Never raises: a failure
        leaves the node off and is reported by ``status()``."""
        with self._lock:
            s = self._settings
            try:
                if s["enabled"]:
                    self._start(s)
                else:
                    self._stop()
                self._error = None
            except Exception as exc:                 # LXMF/Reticulum can raise anything
                self._error = str(exc) or exc.__class__.__name__
                self._stop_quietly()

    def _start(self, s):
        mounts = self._mounts_text if self._mounts_text is not None else _read_mounts()
        if is_network_mount(self._dir, mounts):
            raise RuntimeError("The data directory is on network storage. A propagation "
                               "node writes constantly and must use local disk.")
        if self._router is None:
            args = router_kwargs(s)
            os.makedirs(self._dir, exist_ok=True)
            self._router = self._lxmf.LXMRouter(identity=self._identity(),
                                                storagepath=self._dir, **args)
            self._router_args = args
        self._router.set_message_storage_limit(megabytes=s["storage_mb"])
        if not self._router.propagation_node:
            self._router.enable_propagation()
            self._last_announce = self._clock()

    def _stop(self):
        if self._router is not None and self._router.propagation_node:
            self._router.disable_propagation()

    def _stop_quietly(self):
        try:
            self._stop()
        except Exception:
            pass

    def _restart_required(self):
        return (self._router_args is not None
                and router_kwargs(self._settings) != self._router_args)

    # ---- announces ------------------------------------------------------

    def _running(self):
        return self._router is not None and bool(self._router.propagation_node)

    def announce(self):
        with self._lock:
            if not self._running():
                return {"ok": False, "error": "The propagation node is not running."}
            now = self._clock()
            wait = self._last_manual + MANUAL_ANNOUNCE_COOLDOWN_S - now
            if self._last_manual and wait > 0:
                return {"ok": False, "retry_in_s": int(wait) + 1,
                        "error": "Announced a moment ago. Try again shortly."}
            self._router.announce_propagation_node()
            self._last_manual = self._last_announce = now
            return {"ok": True}

    def tick(self):
        """Call regularly (about once a minute); re-announces when due."""
        with self._lock:
            if self._running() and self._clock() - self._last_announce >= REANNOUNCE_INTERVAL_S:
                try:
                    self._router.announce_propagation_node()
                finally:
                    self._last_announce = self._clock()

    # ---- status ---------------------------------------------------------

    def status(self):
        with self._lock:
            st = {"settings": self.settings(), "restart_required": self._restart_required(),
                  "error": self._error, "address": None, "uptime_s": 0, "messages": 0,
                  "store_bytes": 0, "store_limit_bytes": None, "peers": [],
                  "received": 0, "served": 0}
            if self._error:
                st["state"] = "error"
                return st
            if not self._running():
                st["state"] = "off"
                return st
            st["state"] = "running"
            try:
                raw = self._router.compile_stats() or {}
            except Exception:
                raw = {}
            dest = raw.get("destination_hash") or getattr(self._router.propagation_destination, "hash", None)
            st["address"] = dest.hex() if isinstance(dest, (bytes, bytearray)) else None
            st["uptime_s"] = int(raw.get("uptime") or 0)
            store = raw.get("messagestore") or {}
            st["messages"] = int(store.get("count") or 0)
            st["store_bytes"] = int(store.get("bytes") or 0)
            st["store_limit_bytes"] = store.get("limit")
            clients = raw.get("clients") or {}
            st["received"] = int(clients.get("client_propagation_messages_received") or 0)
            st["served"] = int(clients.get("client_propagation_messages_served") or 0)
            for pid, p in (raw.get("peers") or {}).items():
                m = p.get("messages") or {}
                st["peers"].append({
                    "id": pid.hex() if isinstance(pid, (bytes, bytearray)) else str(pid),
                    "name": p.get("name") or "", "type": p.get("type"),
                    "alive": bool(p.get("alive")), "last_heard": p.get("last_heard"),
                    "offered": m.get("offered", 0), "outgoing": m.get("outgoing", 0),
                    "incoming": m.get("incoming", 0),
                })
            return st

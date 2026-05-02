"""
LXMF delivery-announce tracker.

Listens for RNS announces on the lxmf.delivery aspect and records every
identity that announces — the same way NodeBrowser tracks NomadNet nodes.
The display name comes from app_data (UTF-8 encoded name string) attached
to the announce, if present.
"""

import json
import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

ASPECT = "lxmf.delivery"


class LXMFPeerTracker:
    def __init__(self, storage_dir: str):
        self._path  = os.path.join(storage_dir, "lxmf_peers.json")
        self._lock  = threading.Lock()
        self._peers: dict = {}
        os.makedirs(storage_dir, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_peers(self) -> list:
        with self._lock:
            peers = [dict(p) for p in self._peers.values()]
        # Compute hops live — same approach as NodeBrowser._hop_count.
        # `hops_to` returns 128 (sentinel) when no path is known.
        # Cache last known value so it survives restarts as an FYI fallback.
        live_map: dict = {}
        try:
            import RNS
            for p in peers:
                try:
                    hops = RNS.Transport.hops_to(bytes.fromhex(p["hash"]))
                    live_map[p["hash"]] = None if hops is None or hops >= 128 else int(hops)
                except Exception:
                    live_map[p["hash"]] = None
        except Exception:
            pass

        needs_persist = False
        for p in peers:
            live = live_map.get(p["hash"])
            if live is not None:
                p["hops"] = live
                with self._lock:
                    stored = self._peers.get(p["hash"])
                    if stored and stored.get("last_known_hops") != live:
                        stored["last_known_hops"] = live
                        needs_persist = True
            else:
                p["hops"] = p.get("last_known_hops")

        if needs_persist:
            with self._lock:
                snapshot = dict(self._peers)
            self._persist(snapshot)

        return sorted(peers, key=lambda p: -p["last_seen"])

    def register_announce_handler(self) -> "_LXMFAnnounceHandler":
        return _LXMFAnnounceHandler(self)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def record(self, destination_hash: bytes, app_data: Optional[bytes]) -> None:
        hash_hex = destination_hash.hex()
        name = ""
        if app_data:
            try:
                import RNS.vendor.umsgpack as msgpack
                unpacked = msgpack.unpackb(app_data)
                # LXMF delivery format: [display_name_bytes, stamp_cost]
                if isinstance(unpacked, list) and unpacked:
                    raw = unpacked[0]
                    if isinstance(raw, bytes):
                        name = raw.decode("utf-8", errors="replace").strip()
                    elif isinstance(raw, str):
                        name = raw.strip()
            except Exception:
                # Fallback: plain UTF-8 string (older clients)
                try:
                    name = app_data.decode("utf-8", errors="replace").strip()
                except Exception:
                    pass

        now = time.time()
        with self._lock:
            existing = self._peers.get(hash_hex)
            if existing:
                existing["last_seen"]      = now
                existing["announce_count"] = existing.get("announce_count", 0) + 1
                if name:
                    existing["name"] = name
            else:
                self._peers[hash_hex] = {
                    "hash":           hash_hex,
                    "name":           name,
                    "first_seen":     now,
                    "last_seen":      now,
                    "announce_count": 1,
                }
            snapshot = dict(self._peers)

        log.info("LXMF peer announce: %s (%s)", hash_hex[:16], name or "no name")
        self._persist(snapshot)

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            with self._lock:
                self._peers = data
            log.info("Loaded %d LXMF peers", len(self._peers))
        except Exception as exc:
            log.warning("Could not load LXMF peers: %s", exc)

    def _persist(self, snapshot: dict) -> None:
        try:
            tmp = f"{self._path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            log.warning("Could not save LXMF peers: %s", exc)


class _LXMFAnnounceHandler:
    aspect_filter = ASPECT

    def __init__(self, tracker: LXMFPeerTracker):
        self._tracker = tracker

    def received_announce(self, destination_hash, announced_identity, app_data):
        self._tracker.record(destination_hash, app_data)

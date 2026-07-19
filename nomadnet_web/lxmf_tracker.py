"""
LXMF delivery-announce tracker.

Listens for RNS announces on the lxmf.delivery aspect and records every
identity that announces — the same way NodeBrowser tracks NomadNet nodes.
The display name comes from app_data (UTF-8 encoded name string) attached
to the announce, if present.

Persistence is batched. ``record()`` marks the in-memory state dirty and
returns; a background thread flushes to disk every
``PERSIST_INTERVAL_S`` seconds. Historically each announce persisted the
entire 34k-peer database inline, which — on the RNS read_loop thread,
holding the GIL through ``json.dump`` for a multi-megabyte dict —
starved every other thread. NAS-backed ``/config`` made the same code
grind to a halt with LINKREQUESTs never getting CPU to actually
transmit. Batching decouples announce-arrival rate from disk I/O rate.
"""

import atexit
import json
import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

ASPECT = "lxmf.delivery"


class LXMFPeerTracker:
    # Persist at most this often. Announce arrivals mark dirty; the
    # background persist thread flushes to disk once per interval.
    # 60 s balances "state survives a container restart" against
    # "we're not disk-thrashing on busy mesh chatter." Bump if disk
    # I/O is somehow still a bottleneck; drop only if peer freshness
    # after a hard crash matters more than steady-state throughput.
    PERSIST_INTERVAL_S = 60

    def __init__(self, storage_dir: str):
        self._path  = os.path.join(storage_dir, "lxmf_peers.json")
        self._lock  = threading.Lock()
        self._peers: dict = {}
        self._dirty = False
        self._dirty_lock = threading.Lock()
        self._stop_event = threading.Event()
        os.makedirs(storage_dir, exist_ok=True)
        self._load()

        # Background persister — daemon so it dies with the process.
        # Also register an atexit handler so a clean shutdown flushes
        # any pending dirty state to disk before the process exits.
        threading.Thread(
            target=self._persist_loop,
            daemon=True,
            name="lxmf-tracker-persist",
        ).start()
        atexit.register(self._flush_if_dirty)

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
            self._mark_dirty()

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

        log.info("LXMF peer announce: %s (%s)", hash_hex[:16], name or "no name")
        self._mark_dirty()

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

    def _mark_dirty(self) -> None:
        """Flag the in-memory state as needing persistence. Called from
        the announce handler thread (RNS's read_loop). Cheap — sets a
        flag under a lock and returns. The actual disk write happens
        later, on the persister thread.
        """
        with self._dirty_lock:
            self._dirty = True

    def _persist_loop(self) -> None:
        """Background thread: every ``PERSIST_INTERVAL_S`` seconds,
        flush any dirty state to disk. Uses ``Event.wait`` so an
        atexit-triggered ``set()`` on ``_stop_event`` (added later if
        needed) would break out promptly rather than sleeping through.
        """
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=self.PERSIST_INTERVAL_S):
                break
            self._flush_if_dirty()

    def _flush_if_dirty(self) -> None:
        """Snapshot + write if the dirty flag is set. Re-marks dirty
        on write failure so the next tick tries again — matches the
        old inline behaviour where a failed persist just meant we'd
        try again on the next announce.
        """
        with self._dirty_lock:
            if not self._dirty:
                return
            self._dirty = False
        with self._lock:
            snapshot = dict(self._peers)
        try:
            self._persist(snapshot)
        except Exception as exc:
            log.warning("LXMF peers persist failed, will retry: %s", exc)
            with self._dirty_lock:
                self._dirty = True

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

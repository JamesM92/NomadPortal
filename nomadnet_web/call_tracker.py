"""
LXST telephony-announce tracker.

Phase 0 of a real voice-call feature (ported from the NomadPortal-
Android sister project's own call_tracker.py, itself Phase 0 of its own
staged LXST voice-call work): this listens for RNS announces on the
"lxst.telephony" aspect and records which identities have ever shown up
as call-capable — nothing about placing or receiving an actual call
yet, just "does this contact's client support it," surfaced as a phone
icon in the Network tab and the announce-info dialog. The aspect
string, destination shape, and identity-vs-destination-hash
relationship below were verified (on the Android side) directly against
markqvist/LXST's real source (LXST/__init__.py's APP_NAME = "lxst",
LXST/Primitives/Telephony.py's RNS.Destination(identity, IN, SINGLE,
APP_NAME, "telephony")) — not guessed.

Keyed by *identity* hash, not destination hash. A single RNS.Identity
produces a different destination hash per aspect it announces under
(destination hash is derived from identity + aspect together) — so this
identity's "lxst.telephony" destination hash is NOT the same value as
its "lxmf.delivery" destination hash, even though it's the same person.
The identity's own `.hash` (independent of aspect) is what's shared
across both, so that's the correlation key: LXMFPeerTracker.record()
was extended to also capture each LXMF peer's identity_hash, and
routes.py's /api/lxmf-peers cross-references that against
get_call_capable_hashes() here.

Same batched-persistence design as LXMFPeerTracker, for the same
reason (see that module's own doc comment for the real GIL-contention
bug inline persistence caused) — lower expected announce volume here
(not every LXMF client also runs LXST), but no reason to risk the same
class of bug for a brand-new feature.

Actual call placement/audio (Phase 1a/1b) is a separate, much larger
effort layered on top of this later — this module is presence tracking
only.
"""

import atexit
import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

ASPECT = "lxst.telephony"


class CallPeerTracker:
    # Same floor/rationale as LXMFPeerTracker.PERSIST_INTERVAL_S.
    PERSIST_INTERVAL_S = 60

    def __init__(self, storage_dir: str):
        self._path = os.path.join(storage_dir, "call_peers.json")
        self._lock = threading.Lock()
        self._peers: dict = {}
        self._dirty = False
        self._dirty_lock = threading.Lock()
        self._stop_event = threading.Event()
        os.makedirs(storage_dir, exist_ok=True)
        self._load()

        threading.Thread(
            target=self._persist_loop,
            daemon=True,
            name="call-tracker-persist",
        ).start()
        atexit.register(self._flush_if_dirty)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_call_capable_hashes(self) -> set:
        """Every identity hash (hex) that has ever announced on the
        lxst.telephony aspect — not time-limited (yet). Phase 0 only
        needs "has this identity ever shown call support," not "is a
        call to them likely to succeed right now"; a liveness/hops
        notion (mirroring LXMFPeerTracker.get_peers()'s live hop-count
        refresh) can layer on top later, once there's an actual call
        feature that needs to distinguish those.
        """
        with self._lock:
            return set(self._peers.keys())

    def get_peers(self) -> list:
        with self._lock:
            return sorted(
                (dict(p) for p in self._peers.values()),
                key=lambda p: -p["last_seen"],
            )

    def register_announce_handler(self) -> "_CallAnnounceHandler":
        return _CallAnnounceHandler(self)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def record(self, identity_hash: bytes) -> None:
        hash_hex = identity_hash.hex()
        now = time.time()
        with self._lock:
            existing = self._peers.get(hash_hex)
            if existing:
                existing["last_seen"] = now
                existing["announce_count"] = existing.get("announce_count", 0) + 1
            else:
                self._peers[hash_hex] = {
                    "identity_hash": hash_hex,
                    "first_seen": now,
                    "last_seen": now,
                    "announce_count": 1,
                }
        log.info("LXST call-capable announce: %s", hash_hex[:16])
        self._mark_dirty()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            with self._lock:
                self._peers = data
            log.info("Loaded %d call-capable peers", len(self._peers))
        except Exception as exc:
            log.warning("Could not load call peers: %s", exc)

    def _mark_dirty(self) -> None:
        with self._dirty_lock:
            self._dirty = True

    def _persist_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=self.PERSIST_INTERVAL_S):
                break
            self._flush_if_dirty()

    def _flush_if_dirty(self) -> None:
        with self._dirty_lock:
            if not self._dirty:
                return
            self._dirty = False
        with self._lock:
            snapshot = dict(self._peers)
        try:
            self._persist(snapshot)
        except Exception as exc:
            log.warning("Call peers persist failed, will retry: %s", exc)
            with self._dirty_lock:
                self._dirty = True

    def _persist(self, snapshot: dict) -> None:
        try:
            tmp = f"{self._path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            log.warning("Could not save call peers: %s", exc)


class _CallAnnounceHandler:
    aspect_filter = ASPECT

    def __init__(self, tracker: CallPeerTracker):
        self._tracker = tracker

    def received_announce(self, destination_hash, announced_identity, app_data):
        # Keyed by identity, not destination — see this module's own doc
        # comment for why. announced_identity is never None here in
        # practice (RNS only calls received_announce for a successfully
        # decoded announce, which always carries the identity), but
        # guarded anyway since a bad announce silently dropped is far
        # better than one that crashes the read_loop thread.
        if announced_identity is None:
            return
        self._tracker.record(announced_identity.hash)

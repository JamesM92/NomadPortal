"""LXMF propagation-node outbound sync — the MeshChat-parity mechanism
for keeping NomadPortal's transport identity warm at every intermediate
mesh node.

Root of the reliability gap this closes: NomadPortal-browser between
clicks generates almost no outbound RNS traffic. MeshChat, by contrast,
constantly runs a propagation-node sync loop that establishes an
outbound ``RNS.Link`` every ~5 min, does ``identify + request``, and
closes. That periodic round-trip through the mesh keeps intermediate
transport nodes' path tables refreshed for MeshChat's identity, so
peers' path_responses reliably route back to it. Without it, our
identity ages out of intermediate caches over hours and reachability
of specific destinations intermittently breaks — the pattern this
session's investigation ended with.

This service replicates that ongoing outbound traffic by driving each
active LXMRouter to call ``request_messages_from_propagation_node``
every ``SYNC_INTERVAL_S`` against an auto-discovered propagation node.
The mailbox function of that call is coincidental — even when the
mailbox is empty, the outbound Link handshake IS the warming.

Auto-discovery: an announce handler listens on the ``lxmf.propagation``
aspect and maintains a live pool of known propagation nodes. Ranking
is (hops ascending, last_seen descending). Stale nodes drop out of
the pool after ``NODE_FRESHNESS_S``. Re-pick fires on staleness OR on
``CONSECUTIVE_FAILURES_BEFORE_REPICK`` consecutive failures against
the current pick.

Belt-and-braces: this service runs in parallel with the existing
default-node keepalive in browser.py. They don't coordinate. If sync
works, the default-node keepalive succeeds trivially; if sync fails,
the default-node keepalive is exactly the recovery mechanism we
already have.
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Match MeshChat's cadence. Every SYNC_INTERVAL_S the loop fires a
# request_messages_from_propagation_node on each active router.
SYNC_INTERVAL_S = 300

# After this many consecutive per-router failures against the currently-
# picked node, force a re-pick on the next tick. Rationale: the current
# pick may have gone offline for us specifically (from our particular
# TCP session), and re-picking might find a different node whose path
# is currently answerable.
CONSECUTIVE_FAILURES_BEFORE_REPICK = 3

# Prefer nodes whose last-heard announce is within this window. Older
# nodes stay in ``_known_nodes`` (they may come back when they re-
# announce) but are excluded from the pick pool.
NODE_FRESHNESS_S = 60 * 60 * 6  # 6 h

# Startup grace: don't attempt any sync in the first N seconds after
# the loop starts, so the announce handler has time to hear at least
# one lxmf.propagation announce before we look at the pool.
STARTUP_GRACE_S = 60

# Shortest time between two "Sync now" presses by the same account, so the
# button cannot be used to hammer a propagation node.
MANUAL_SYNC_COOLDOWN_S = 10

# What LXMRouter.propagation_transfer_state means, in words for a person.
# (name, still_working). Codes 1-6 are a transfer in progress; 7 is done;
# 240 and above are the ways it can fail.
_TRANSFER_STATES = {
    0:   ("idle", False),
    1:   ("finding a path", True),
    2:   ("connecting", True),
    3:   ("connected", True),
    4:   ("asking for messages", True),
    5:   ("receiving messages", True),
    6:   ("processing messages", True),
    7:   ("complete", False),
    10:  ("timed out finding a path", False),
    240: ("no path to the node", False),
    241: ("could not connect to the node", False),
    242: ("the transfer failed", False),
    243: ("the node did not receive your identity", False),
    244: ("the node refused access", False),
    254: ("failed", False),
}

_NODE_HEX_RE = re.compile(r"[0-9a-f]{32}")      # a 128-bit destination hash


def transfer_state_name(code) -> tuple:
    """``(words, still_working)`` for a router transfer-state code."""
    if code is None:
        return "idle", False
    return _TRANSFER_STATES.get(code, ("unknown", False))


def _parse_node_hex(value) -> Optional[bytes]:
    """A propagation node hash from user input: None for blank, bytes for
    32 hex characters, ValueError for anything else."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("A node hash must be text")
    v = value.strip().lower()
    if not v:
        return None
    if not _NODE_HEX_RE.fullmatch(v):
        raise ValueError("A node hash is 32 hexadecimal characters")
    return bytes.fromhex(v)


# How often the loop wakes up to check the interval. Small enough that
# a re-pick trigger takes effect quickly; large enough that CPU is
# negligible when nothing needs to happen.
LOOP_POLL_S = 1.0


class PropagationSyncService:
    """Auto-discovering LXMF propagation-node sync service.

    Owns:
      - An announce handler on ``lxmf.propagation`` that keeps a live
        map of known propagation nodes and their hop counts
      - A background thread that periodically syncs each active
        LXMRouter with the currently-picked propagation node

    Does not own:
      - Router lifecycle (that's ``MessagingService``)
      - RNS init / interface state (that's ``NodeBrowser``)
      - Fetch-page reliability (that's ``browser.py``, which has its
        own default-node keepalive as belt-and-braces)
    """

    def __init__(self, rns, messaging_service, storage_dir: Optional[str] = None) -> None:
        """Store references. Does NOT touch RNS.Transport yet — call
        ``start()`` to register the announce handler and launch the
        loop. Keeps the constructor safe to call before RNS is up.

        ``storage_dir`` (optional) is where each account's chosen node is
        kept between restarts.
        """
        self._rns = rns
        self._messaging = messaging_service

        # user_sub -> bytes: a node the account chose instead of the
        # automatic pick. Missing means automatic.
        self._prefs: dict = {}
        self._prefs_lock = threading.Lock()
        self._prefs_path = (os.path.join(storage_dir, "propagation_prefs.json")
                            if storage_dir else None)
        self._last_manual: dict = {}          # user_sub -> time of the last "Sync now"
        self._load_prefs()

        # bytes(destination_hash) → {"hops": int, "first_seen": float,
        #                            "last_seen": float,
        #                            "app_data": bytes or None,
        #                            "announce_count": int}
        self._known_nodes: dict = {}
        self._known_nodes_lock = threading.Lock()

        # Currently-selected propagation node. None until we've heard
        # at least one lxmf.propagation announce.
        self._picked: Optional[bytes] = None

        # Per-router sync state:
        # user_sub → {"last_synced_at": float or None,
        #             "consecutive_failures": int,
        #             "last_error": str or None}
        self._status_by_user: dict = {}
        self._status_lock = threading.Lock()

        self._started = False
        self._start_lock = threading.Lock()
        self._announce_handler = None
        self._service_started_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Register the lxmf.propagation announce handler and launch
        the sync loop. Idempotent — subsequent calls are no-ops.
        """
        with self._start_lock:
            if self._started:
                return
            self._started = True
            self._service_started_at = time.monotonic()

        try:
            self._announce_handler = _PropagationAnnounceHandler(self)
            self._rns.Transport.register_announce_handler(
                self._announce_handler
            )
        except Exception:
            log.exception(
                "PropagationSyncService: could not register announce "
                "handler; auto-discovery disabled for this run"
            )

        threading.Thread(
            target=self._sync_loop,
            daemon=True,
            name="lxmf-propagation-sync",
        ).start()
        log.info(
            "PropagationSyncService started — waiting %ds then syncing "
            "every %ds",
            STARTUP_GRACE_S, SYNC_INTERVAL_S,
        )

    def snapshot(self) -> dict:
        """Return a JSON-serialisable view of current state, for
        ``/api/_debug/state``.
        """
        now = time.time()
        with self._known_nodes_lock:
            total = len(self._known_nodes)
            fresh = sum(
                1 for e in self._known_nodes.values()
                if now - e["last_seen"] <= NODE_FRESHNESS_S
            )
        with self._status_lock:
            syncs = {
                sub: {
                    "last_synced_at": s["last_synced_at"],
                    "consecutive_failures": s["consecutive_failures"],
                    "last_error": s["last_error"],
                }
                for sub, s in self._status_by_user.items()
            }
        return {
            "picked_node_hex": (
                self._picked.hex() if self._picked is not None else None
            ),
            "known_nodes": total,
            "fresh_nodes": fresh,
            "syncs_per_user": syncs,
        }

    def list_known_nodes(self) -> list:
        """The individual propagation-node announces this service has
        heard, for the Network tab's Relays list (see routes.py's
        ``GET /api/relays``). Previously this data only existed as an
        aggregate count in ``snapshot()`` — every other announce
        tracker (``NodeBrowser``, ``LXMFPeerTracker``) already exposes
        its own individual entries, so this closes that gap rather
        than adding a parallel tracking mechanism. Shaped to match
        those trackers' own row fields (``hash``/``hops``/
        ``last_seen``/``announce_count``) so the frontend can render
        all three kinds through one shared row renderer. No ``name``
        field — a ``lxmf.propagation`` announce's ``app_data`` carries
        capability flags, not a human-assigned display name (same as
        real MeshChat/Sideband propagation-node listings); the
        frontend falls back to a hash-prefix label like it already
        does for any other unnamed entry. ``picked`` flags the node
        this service is currently syncing through, so the UI can
        surface that rather than leaving it a debug-only detail.
        """
        with self._known_nodes_lock:
            picked = self._picked
            return [
                {
                    "hash": dest_hash.hex(),
                    "hops": entry["hops"],
                    "first_seen": entry["first_seen"],
                    "last_seen": entry["last_seen"],
                    "announce_count": entry.get("announce_count", 1),
                    "picked": dest_hash == picked,
                }
                for dest_hash, entry in self._known_nodes.items()
            ]

    # ------------------------------------------------------------------
    # Announce handling (called by _PropagationAnnounceHandler)
    # ------------------------------------------------------------------

    def _on_propagation_announce(
        self,
        destination_hash: bytes,
        announced_identity,
        app_data,
    ) -> None:
        """Upsert a propagation node into the known-nodes pool.

        Called by the announce handler for every ``lxmf.propagation``
        announce received. Records current hop count and timestamp so
        the picker can rank nodes by proximity + freshness. If we
        don't have a pick yet, or if this announce makes a materially
        better candidate available, we can re-pick on the next tick
        — this method itself doesn't call _pick to avoid holding
        RNS's announce-processing thread.
        """
        now = time.time()
        try:
            hops = self._rns.Transport.hops_to(destination_hash)
        except Exception:
            hops = None
        # hops_to returns PATHFINDER_M for unknown; treat as "very far"
        # rather than special-casing here — ranking will de-prioritise.
        if hops is None:
            hops = 128

        with self._known_nodes_lock:
            entry = self._known_nodes.get(destination_hash)
            if entry is None:
                self._known_nodes[destination_hash] = {
                    "hops": hops,
                    "first_seen": now,
                    "last_seen": now,
                    "app_data": app_data,
                    "announce_count": 1,
                }
                log.info(
                    "PropagationSyncService: new propagation node "
                    "%s (%d hops)",
                    destination_hash.hex()[:16], hops,
                )
            else:
                entry["hops"] = hops
                entry["last_seen"] = now
                entry["app_data"] = app_data
                entry["announce_count"] = entry.get("announce_count", 0) + 1

    # ------------------------------------------------------------------
    # Sync loop
    # ------------------------------------------------------------------

    def _sync_loop(self) -> None:
        """Background thread body.

        Sleeps ``STARTUP_GRACE_S``, fires one sync tick immediately so
        operators can verify the service is working, then loops
        forever: every ``SYNC_INTERVAL_S`` (checked at ``LOOP_POLL_S``
        granularity), run one sync pass across all active routers.
        """
        # Startup grace — let announces trickle in.
        time.sleep(STARTUP_GRACE_S)

        # First tick fires right after grace ends. Earlier drafts set
        # ``last_sync_at = 0.0`` and let the interval compare handle
        # it, but ``time.monotonic()`` on a freshly-booted container
        # returns a small value (system uptime seconds since boot),
        # which is < SYNC_INTERVAL_S, so the first tick didn't fire
        # until ~SYNC_INTERVAL_S after process start — far too late for
        # operators to verify the service works.
        try:
            self._tick()
        except Exception:
            log.exception(
                "PropagationSyncService: initial tick raised"
            )
        last_sync_at = time.monotonic()

        while True:
            try:
                time.sleep(LOOP_POLL_S)
                now = time.monotonic()
                if now - last_sync_at < SYNC_INTERVAL_S:
                    continue
                last_sync_at = now
                self._tick()
            except Exception:
                log.exception(
                    "PropagationSyncService: sync loop error; will "
                    "continue after brief pause"
                )
                time.sleep(30)

    def _tick(self) -> None:
        """One sync pass. Picks a node (if needed), then invokes
        request_messages_from_propagation_node on each active router.
        """
        # Decide whether we need to re-pick.
        should_repick = self._picked is None or self._picked_is_stale()
        if not should_repick:
            # Also re-pick if too many failures on the current pick.
            with self._status_lock:
                for status in self._status_by_user.values():
                    if (status["consecutive_failures"]
                            >= CONSECUTIVE_FAILURES_BEFORE_REPICK):
                        should_repick = True
                        break

        if should_repick:
            new_pick = self._pick_best_node()
            if new_pick is not None and new_pick != self._picked:
                if self._picked is None:
                    log.info(
                        "PropagationSyncService: picked propagation "
                        "node %s",
                        new_pick.hex()[:16],
                    )
                else:
                    log.info(
                        "PropagationSyncService: switched propagation "
                        "node %s → %s (staleness or repeated failures)",
                        self._picked.hex()[:16], new_pick.hex()[:16],
                    )
                self._picked = new_pick
                # Reset failure counters on switch — the old node's
                # failures aren't the new node's problem.
                with self._status_lock:
                    for status in self._status_by_user.values():
                        status["consecutive_failures"] = 0

        if self._picked is None and not self._prefs:
            log.info(
                "PropagationSyncService: no propagation node known yet "
                "— skipping tick"
            )
            return

        # Iterate active routers and sync each.
        try:
            routers = self._messaging.active_routers()
        except Exception:
            log.exception(
                "PropagationSyncService: could not read active routers"
            )
            return

        for user_sub, data in routers:
            self._sync_one(user_sub, data)

    def _sync_one(self, user_sub: str, data: dict) -> bool:
        """Fire one sync for one router. True if the request went out."""
        router = data.get("router")
        identity = data.get("identity")
        if router is None or identity is None:
            log.warning(
                "PropagationSyncService: skipping sync for user %s — "
                "router=%s identity=%s (unexpected router shape)",
                (user_sub or "anon")[:16],
                "present" if router is not None else "missing",
                "present" if identity is not None else "missing",
            )
            return False

        node = self.effective_node_for(user_sub)
        if node is None:
            return False

        try:
            router.set_outbound_propagation_node(node)
        except Exception as exc:
            self._record_failure(user_sub, exc)
            return False

        try:
            router.request_messages_from_propagation_node(identity)
            self._record_success(user_sub)
            log.info(
                "PropagationSyncService: sync ok for user %s "
                "→ propagation %s",
                (user_sub or "anon")[:16],
                node.hex()[:16],
            )
            return True
        except Exception as exc:
            self._record_failure(user_sub, exc)
            log.info(
                "PropagationSyncService: sync failed for user %s "
                "→ propagation %s: %s",
                (user_sub or "anon")[:16],
                node.hex()[:16],
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Choosing a node, syncing on demand, reporting status
    # ------------------------------------------------------------------

    def preferred_for(self, user_sub: str) -> Optional[bytes]:
        """The node this account chose, or None for automatic."""
        with self._prefs_lock:
            return self._prefs.get(user_sub)

    def effective_node_for(self, user_sub: str) -> Optional[bytes]:
        """The node this account's messages actually use: its own choice,
        otherwise the automatic pick."""
        return self.preferred_for(user_sub) or self._picked

    def set_preferred(self, user_sub: str, node_hex) -> None:
        """Pin a propagation node for this account (32 hex characters, a
        node heard on the network or typed in by hand), or pass None or an
        empty string to go back to automatic. Raises ValueError for a
        value that is not a node hash; nothing changes then."""
        node = _parse_node_hex(node_hex)
        with self._prefs_lock:
            if node is None:
                self._prefs.pop(user_sub, None)
            else:
                self._prefs[user_sub] = node
            snapshot = {sub: n.hex() for sub, n in self._prefs.items()}
        self._save_prefs(snapshot)

    def _routers_of(self, user_sub: str) -> list:
        try:
            return [data for sub, data in self._messaging.active_routers() if sub == user_sub]
        except Exception:
            log.exception("PropagationSyncService: could not read active routers")
            return []

    def sync_now(self, user_sub: str) -> dict:
        """Collect this account's waiting messages now instead of at the
        next 5-minute pass. The transfer itself runs in the background;
        ``status_for`` shows how it is going. Returns ``{"ok", "error",
        "node"}``."""
        routers = self._routers_of(user_sub)
        if not routers:
            return {"ok": False, "node": None,
                    "error": "There is no delivery identity to sync for this account."}
        node = self.effective_node_for(user_sub)
        if node is None:
            return {"ok": False, "node": None,
                    "error": "No propagation node is known yet. Wait for one to announce, or enter one by hand."}
        now = time.time()
        with self._status_lock:
            last = self._last_manual.get(user_sub, 0.0)
            if now - last < MANUAL_SYNC_COOLDOWN_S:
                return {"ok": False, "node": node.hex(),
                        "error": "Please wait a few seconds before syncing again."}
            self._last_manual[user_sub] = now
        started = [self._sync_one(user_sub, data) for data in routers]
        if any(started):
            return {"ok": True, "node": node.hex(), "error": None}
        with self._status_lock:
            err = (self._status_by_user.get(user_sub) or {}).get("last_error")
        return {"ok": False, "node": node.hex(), "error": err or "The sync could not be started."}

    def status_for(self, user_sub: str) -> dict:
        """Everything the picker needs for one account: which node is in
        use and why, the nodes heard so far, when the last sync happened
        and how the current one is going."""
        pref = self.preferred_for(user_sub)
        node = self.effective_node_for(user_sub)
        nodes = self.list_known_nodes()
        for n in nodes:
            n["preferred"] = (pref is not None and n["hash"] == pref.hex())
        if pref is not None and pref.hex() not in {n["hash"] for n in nodes}:
            nodes.append({"hash": pref.hex(), "hops": None, "first_seen": None, "last_seen": None,
                          "announce_count": 0, "picked": False, "preferred": True})
        nodes.sort(key=lambda n: (not n["preferred"], n["hops"] if n["hops"] is not None else 999,
                                  -(n["last_seen"] or 0)))
        with self._status_lock:
            st = dict(self._status_by_user.get(user_sub) or {})
        routers = self._routers_of(user_sub)
        router = routers[0]["router"] if routers else None
        code = getattr(router, "propagation_transfer_state", None)
        words, active = transfer_state_name(code)
        return {
            "mode": "manual" if pref is not None else "auto",
            "node": node.hex() if node else None,
            "preferred": pref.hex() if pref else None,
            "picked": self._picked.hex() if self._picked else None,
            "nodes": nodes,
            "last_synced_at": st.get("last_synced_at"),
            "last_error": st.get("last_error"),
            "consecutive_failures": st.get("consecutive_failures", 0),
            "transfer": {
                "state": words,
                "code": code,
                "active": active,
                "progress": getattr(router, "propagation_transfer_progress", 0.0) or 0.0,
                "last_result": getattr(router, "propagation_transfer_last_result", None),
                "last_duplicates": getattr(router, "propagation_transfer_last_duplicates", None),
            },
        }

    def _load_prefs(self) -> None:
        if not self._prefs_path:
            return
        try:
            with open(self._prefs_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("PropagationSyncService: preferences file unreadable, ignoring it")
            return
        if not isinstance(raw, dict):
            return
        for sub, value in raw.items():
            try:
                node = _parse_node_hex(value)
            except ValueError:
                continue
            if node is not None:
                self._prefs[str(sub)] = node

    def _save_prefs(self, snapshot: dict) -> None:
        if not self._prefs_path:
            return
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self._prefs_path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh)
            os.replace(tmp, self._prefs_path)
        except OSError:
            log.exception("PropagationSyncService: could not save preferences")

    # ------------------------------------------------------------------
    # Node selection
    # ------------------------------------------------------------------

    def _pick_best_node(self) -> Optional[bytes]:
        """Choose the best currently-known fresh propagation node.

        Ranking: (hops asc, last_seen desc). Nodes older than
        ``NODE_FRESHNESS_S`` are excluded from selection but stay in
        ``_known_nodes`` in case they re-announce.
        """
        now = time.time()
        with self._known_nodes_lock:
            candidates = [
                (dh, entry) for dh, entry in self._known_nodes.items()
                if now - entry["last_seen"] <= NODE_FRESHNESS_S
            ]
        if not candidates:
            return None
        # Sort: lowest hops first, then newest last_seen first.
        candidates.sort(
            key=lambda pair: (pair[1]["hops"], -pair[1]["last_seen"])
        )
        return candidates[0][0]

    def _picked_is_stale(self) -> bool:
        """Return True if the currently-picked node's last announce
        is older than the freshness window."""
        if self._picked is None:
            return False
        now = time.time()
        with self._known_nodes_lock:
            entry = self._known_nodes.get(self._picked)
        if entry is None:
            return True
        return (now - entry["last_seen"]) > NODE_FRESHNESS_S

    # ------------------------------------------------------------------
    # Per-user status tracking
    # ------------------------------------------------------------------

    def _record_success(self, user_sub: str) -> None:
        now = time.time()
        with self._status_lock:
            s = self._status_by_user.setdefault(
                user_sub,
                {
                    "last_synced_at": None,
                    "consecutive_failures": 0,
                    "last_error": None,
                },
            )
            s["last_synced_at"] = now
            s["consecutive_failures"] = 0
            s["last_error"] = None

    def _record_failure(self, user_sub: str, exc: Exception) -> None:
        with self._status_lock:
            s = self._status_by_user.setdefault(
                user_sub,
                {
                    "last_synced_at": None,
                    "consecutive_failures": 0,
                    "last_error": None,
                },
            )
            s["consecutive_failures"] += 1
            s["last_error"] = f"{type(exc).__name__}: {exc}"


class _PropagationAnnounceHandler:
    """RNS announce handler that filters to lxmf.propagation and
    forwards to ``PropagationSyncService._on_propagation_announce``.
    Defined as a plain class rather than a lambda so RNS's
    ``aspect_filter`` attribute lookup works correctly.
    """

    aspect_filter = "lxmf.propagation"

    def __init__(self, service: PropagationSyncService) -> None:
        self._service = service

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity,
        app_data,
    ) -> None:
        try:
            self._service._on_propagation_announce(
                destination_hash, announced_identity, app_data,
            )
        except Exception:
            log.exception(
                "PropagationSyncService: announce handler raised"
            )

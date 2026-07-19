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

import logging
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

    def __init__(self, rns, messaging_service) -> None:
        """Store references. Does NOT touch RNS.Transport yet — call
        ``start()`` to register the announce handler and launch the
        loop. Keeps the constructor safe to call before RNS is up.
        """
        self._rns = rns
        self._messaging = messaging_service

        # bytes(destination_hash) → {"hops": int, "first_seen": float,
        #                            "last_seen": float,
        #                            "app_data": bytes or None}
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

        if self._picked is None:
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

    def _sync_one(self, user_sub: str, data: dict) -> None:
        """Fire one sync for one router."""
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
            return

        try:
            router.set_outbound_propagation_node(self._picked)
        except Exception as exc:
            self._record_failure(user_sub, exc)
            return

        try:
            router.request_messages_from_propagation_node(identity)
            self._record_success(user_sub)
            log.info(
                "PropagationSyncService: sync ok for user %s "
                "→ propagation %s",
                (user_sub or "anon")[:16],
                self._picked.hex()[:16],
            )
        except Exception as exc:
            self._record_failure(user_sub, exc)
            log.info(
                "PropagationSyncService: sync failed for user %s "
                "→ propagation %s: %s",
                (user_sub or "anon")[:16],
                self._picked.hex()[:16],
                exc,
            )

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

"""
Read-only NomadNet node browser.

Connects to the Reticulum network, discovers NomadNet nodes via announces,
and fetches pages from them.
"""

import atexit
import json
import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

APP_NAME    = "nomadnetwork"
NODE_ASPECT = "node"
STALL_TIMEOUT = 30   # seconds — no-progress watchdog. If no packet arrives
                     # in this window, the fetch is aborted: "no response"
                     # if nothing ever arrived, otherwise "lost connection".
PAGE_HARD_CAP = 600  # seconds — absolute upper bound per fetch (10 min).
PATH_TIMEOUT  = 60   # seconds for RNS path discovery before link is established.
PING_TIMEOUT  = 30   # for ping_node link establishment.

# Max time to wait for a fresh RNS.Link's establishment callback to
# fire before we treat it as failed. This exists because RNS's own
# _on_link_closed callback empirically doesn't always fire for
# destinations that are unreachable — the link sits in HANDSHAKE
# state indefinitely, the stall watchdog is gated behind
# link_active[0] so it never fires, and only PAGE_HARD_CAP (10 min)
# eventually breaks us out. Under the retry loop that's up to
# 30 minutes of "loading spinner" for one click.
#
# 120s is long enough for legitimate multi-hop / high-latency links —
# during real-world flaky mesh conditions we've observed handshake
# RTTs up to 2.5 s across a 2-hop path, and each step of the
# handshake needs a full round trip. A tight 60s can time out on a
# link that would have completed in 90-100 s once the mesh recovers
# from a slow patch. 120s keeps a "loading spinner while the mesh is
# catching up" experience under 3 min per attempt while still failing
# fast enough on genuinely unreachable destinations. Total worst-case
# failed fetch: ~6 min (3 × 120s + 2 × 1.5s backoff).
LINK_ESTABLISH_TIMEOUT = 120

# Default-node keepalive: how often to touch the operator-configured
# default node's index page to keep an RNS.Link warm. Slightly under
# RNS's built-in Link.KEEPALIVE (360s) so we're always the ones
# generating the keepalive activity rather than waiting for RNS's
# internal probe, which fails silently when the peer isn't
# responding during a mesh-flaky window. When the mesh is healthy
# this is one tiny fetch every ~4 min; when the mesh is flaky, we
# detect breakage early and re-establish before the user clicks.
DEFAULT_NODE_KEEPALIVE_S = 240

# After this many consecutive keepalive failures we assume RNS's
# long-running Transport state has some stale entry for the default
# node that's preventing recovery. Force-reset the specific
# destination's path_table entry and request_path throttle so the
# next attempt does a completely fresh discovery — not a full RNS
# restart, just targeted state clearing for one destination. See the
# comment on ``_reset_default_node_rns_state`` for the mechanism.
DEFAULT_NODE_HARD_RESET_FAILURES = 3

# fetch_page reuses successful links per-destination so back-to-back
# page loads to the same site don't repeat the ~2-8 s link handshake
# every click. 50 entries is far more than a browsing session touches
# in practice; oldest is evicted (and torn down) when the cap is hit.
LINK_CACHE_MAX_SIZE = 50

# nodes.json persistence cadence. Discovered-node entries are updated
# on every ``nomadnetwork.node`` announce, per-fetch stat changes, and
# hop-count refreshes — historically all inline via ``_persist`` on
# the RNS read_loop thread, which under NAS-backed ``/config`` and a
# 2k+ node registry gridlocks the same way the LXMFPeerTracker did
# before its own debounce. record-mark-dirty + background 60s flush
# decouples announce arrival rate from disk I/O rate. Matches the
# ``LXMFPeerTracker.PERSIST_INTERVAL_S`` cadence so both stores have
# the same freshness guarantees on a hard crash.
NODES_PERSIST_INTERVAL_S = 60

# Between failed link attempts, wait for a fresh announce from the
# target destination (signal that the mesh has updated its view of
# the path) for up to this long before firing the next attempt.
# NomadNet responds to path_requests by re-announcing, so a fresh
# announce is the crispest possible signal that the next attempt has
# a real chance. The old fixed 1.5s sleep was too short — path_request
# round-trips through the mesh take 30-60s under typical conditions.
# If an announce arrives before the timeout, we retry immediately.
RETRY_ANNOUNCE_WAIT = 45

# After the retry-attempt budget is exhausted with a retryable
# error, wait once more for a fresh announce and, if one arrives,
# do one bonus attempt. Observed a fetch fail at 189 s and the
# destination's fresh announce arrive 7 s later; this window
# rescues that pattern without loosening retry semantics for
# genuinely unreachable destinations.
FINAL_ANNOUNCE_WAIT = 45


class _DestinationAnnounceWaiter:
    """Announce handler that flips an event when an announce for a
    specific ``nomadnetwork.node`` destination arrives.

    fetch_page registers one of these per outbound fetch, uses it to
    wake up its retry sleep whenever the destination re-announces,
    and deregisters it in a ``finally``.

    ``receive_path_responses = True`` is load-bearing. RNS's
    ``Transport`` handler-dispatch loop skips announces whose packet
    context is ``PATH_RESPONSE`` unless the handler opts in via
    this attribute. The announce we most want to catch — a
    ``NomadNet`` node re-announcing in response to our own
    ``request_path`` — arrives exactly as a path response, so
    without this flag the waiter never wakes on the fresh-answer
    case the retry loop is trying to catch.
    """

    aspect_filter = "nomadnetwork.node"
    receive_path_responses = True

    def __init__(self, target_hash: bytes) -> None:
        self._target = target_hash
        self.event = threading.Event()

    def received_announce(self, destination_hash, announced_identity, app_data) -> None:
        if destination_hash == self._target:
            self.event.set()

    def wait_and_reset(self, timeout: float) -> bool:
        """Wait for an announce or until ``timeout`` elapses. Returns
        True if an announce arrived, False on timeout. Always clears
        the flag so the next wait starts fresh.
        """
        got = self.event.wait(timeout=timeout)
        self.event.clear()
        return got

# RNS sentinel value meaning "hop count unknown / unreachable"
_HOPS_UNKNOWN = 128


class NodeBrowser:

    def __init__(self, config_dir: Optional[str] = None):
        import RNS
        self._rns  = RNS
        self.nodes: dict = {}
        self._lock = threading.Lock()
        self._total_announces = 0

        # Async page-fetch jobs for the polling progress UI.
        # job_id (16-hex) -> { status: "fetching"|"done"|"error",
        #                      progress: 0.0-1.0, content, error,
        #                      node_hash, path, started, completed }
        self._jobs: dict = {}
        self._jobs_lock = threading.Lock()

        # RNS.Link cache for page-fetch reuse. Matches rBrowser's pattern:
        # keep the link alive after a successful fetch so the next page-load
        # to the same destination skips the ~2-8 s link establishment
        # handshake (3-way RTT + ratchet exchange). A single "click into a
        # site and then click a subpage" experience drops from
        # "several-seconds twice" to "several-seconds once".
        #
        # We cap the cache at LINK_CACHE_MAX_SIZE entries — dicts in
        # Python 3.7+ preserve insertion order, so evicting the first
        # entry gives us a rough LRU. Links that RNS closes on its own
        # remove themselves via the closed_callback we register.
        self._link_cache: dict = {}   # dest_hash (bytes) -> RNS.Link
        self._link_cache_lock = threading.Lock()

        # Per-destination fetch serialization. Multiple concurrent
        # ``fetch_page(dest, ...)`` calls to the SAME destination
        # would each fire their own Link handshake in parallel,
        # which we observed the peer respond to poorly — three
        # simultaneous Link requests from the same identity in the
        # same second, all timing out. Serializing here means the
        # second call waits for the first to establish (and cache)
        # a link, then benefits from the cache. Different-
        # destination fetches still run in parallel.
        #
        # Map: dest_hash (bytes) -> Lock. Locks are keyed lazily —
        # first fetch to a destination creates its lock, later
        # fetches reuse it. Memory footprint is one Lock per unique
        # destination we've ever fetched, which is bounded by the
        # nodes actually browsed.
        self._inflight_fetches: dict = {}
        self._inflight_fetches_lock = threading.Lock()

        if config_dir:
            self._nodes_file = os.path.join(
                os.path.dirname(config_dir.rstrip("/")), "nodes.json"
            )
        else:
            self._nodes_file = "/config/nodes.json"

        self._favorites_file = os.path.join(
            os.path.dirname(self._nodes_file), "favorites.json"
        )
        self._iface_stats_file = os.path.join(
            os.path.dirname(self._nodes_file), "iface_stats.json"
        )
        # user_sub -> list[{hash, path, name, added}]
        # Legacy format (list[hash_hex]) is auto-migrated on load to objects
        # with path="/" and name=<best-known node name>.
        self._favorites: dict = {}
        self._hosted_hash: str = ""  # set externally after SiteServer starts
        self._hosted_name: str = ""  # authoritative name; overrides cached value
        # Lifetime byte totals per interface name, accumulated across restarts.
        # Value = total bytes from all completed sessions (not including current session).
        # Saved to disk as base + current session so a restart continues correctly.
        self._iface_base: dict = {}   # {name: {"rxb": int, "txb": int}}
        self._blocklist: set  = set()
        self._blocklist_file = os.path.join(
            os.path.dirname(self._nodes_file), "blocklist.json"
        )

        self._load_nodes()
        self._load_favorites()
        self._load_iface_stats()
        self._load_blocklist()

        # Debounced persistence for nodes.json. Announce handlers and
        # per-fetch stat updates mark dirty; a daemon thread flushes
        # every ``NODES_PERSIST_INTERVAL_S`` seconds. See the constant's
        # comment for the pathology this closes.
        self._nodes_dirty = False
        self._nodes_dirty_lock = threading.Lock()
        self._nodes_stop_event = threading.Event()
        threading.Thread(
            target=self._nodes_persist_loop,
            daemon=True,
            name="nodebrowser-persist",
        ).start()
        atexit.register(self._flush_nodes_if_dirty)

        # Reticulum's constructor blocks for 60–300 seconds on real
        # deployments while it replays destination_table, brings up
        # TCP client interfaces to hubs, and runs internal transport
        # startup. Running that on the main thread makes gunicorn's
        # WSGI factory hang for the entire duration, so /healthz and
        # the whole web UI are unreachable until it completes.
        #
        # Defer to a background thread. State-loading above (nodes,
        # favorites, iface stats, blocklist) has already completed on
        # the main thread — it's fast and its output is what the
        # sidebar / node list needs to render even before RNS is up.
        # RNS-dependent operations (fetch_page, get_diagnostics,
        # ping_node, LXMF setup, site-server startup) block on
        # ``self._ready`` before they can run.
        self.reticulum = None
        self._counter_handler  = None
        self._announce_handler = None
        self._config_dir = config_dir
        self._ready = threading.Event()

        # RNS init timing — kept so /healthz can report elapsed time and
        # an ETA derived from previous startup durations. First run has
        # no history so the ETA is None; the second run onwards gets a
        # median of the last few durations.
        self._rns_init_history: list = self._load_rns_init_history()
        self._rns_init_start_mono:  Optional[float] = None
        self._rns_init_end_mono:    Optional[float] = None

        threading.Thread(
            target=self._init_reticulum,
            daemon=True,
            name="rns-init",
        ).start()

        threading.Thread(
            target=self._default_node_keepalive_loop,
            daemon=True,
            name="default-node-keepalive",
        ).start()

    def _init_reticulum(self) -> None:
        """Background-thread RNS init. Sets ``self._ready`` on success.
        Leaves the event unset on failure so callers keep returning 503
        rather than exploding on a partially-initialised transport.
        Records the duration on success so subsequent restarts can quote
        an ETA in ``rns_init_progress()``.
        """
        import signal

        # RNS.Reticulum() unconditionally installs a SIGINT handler via
        # ``signal.signal()``, which raises
        #     ValueError: signal only works in main thread of the main interpreter
        # when called from any thread other than the main one — and this
        # method IS called from a background thread by design (that's the
        # whole point of the deferred init).
        #
        # gunicorn / Docker handle process signals for us in this
        # deployment, so RNS's Ctrl-C handler isn't needed. Install a
        # process-wide shim that swallows the ValueError; if the caller
        # IS on the main thread it still delegates to the real
        # ``signal.signal`` so nothing changes for well-behaved callers.
        original_signal = signal.signal
        def _thread_safe_signal(signum, handler):
            try:
                return original_signal(signum, handler)
            except ValueError:
                # Not on main thread; skip. Wanted behaviour in this deployment.
                return None
        signal.signal = _thread_safe_signal

        RNS = self._rns

        # Patch RNS.TCPClientInterface.process_outgoing BEFORE
        # constructing Reticulum(). See docstring on
        # _patch_tcpclient_transient_tx_errors for the full rationale
        # — short version: RNS treats any Exception from ``sendall``
        # as unrecoverable and calls ``teardown()``, which flips
        # ``IN``/``OUT`` to False. The read_loop then quietly
        # reconnects the socket but does NOT reset the flags, so
        # ``Transport.outbound()`` refuses to send anything on that
        # interface for the rest of the process's life. This is what
        # made "works fresh, degrades in ~15 min, only container
        # restart fixes it" a stable failure mode for months.
        self._patch_tcpclient_transient_tx_errors(RNS)

        self._rns_init_start_mono = time.monotonic()
        try:
            log.info("Starting Reticulum (config: %s)", self._config_dir or "default")
            self.reticulum = RNS.Reticulum(self._config_dir)

            self._counter_handler = _CountAnnounceHandler(self)
            RNS.Transport.register_announce_handler(self._counter_handler)

            self._announce_handler = _NodeAnnounceHandler(self)
            RNS.Transport.register_announce_handler(self._announce_handler)

            self._rns_init_end_mono = time.monotonic()
            duration = self._rns_init_end_mono - self._rns_init_start_mono
            log.info(
                "NodeBrowser ready — %d node(s) loaded, listening for "
                "announces (RNS init took %.1fs)",
                len(self.nodes), duration,
            )
            self._save_rns_init_history(duration)
            self._ready.set()
        except Exception:
            log.exception("Reticulum init failed — RNS-dependent endpoints "
                          "will return 503 until the process restarts")

    def _patch_tcpclient_transient_tx_errors(self, RNS) -> None:
        """Replace ``TCPClientInterface.process_outgoing`` so transient
        TCP errors (``ConnectionResetError``, ``BrokenPipeError``, etc.)
        don't permanently disable the interface.

        RNS 1.1.3's TCPClientInterface catches every exception from
        ``socket.sendall`` and calls ``self.teardown()``. That flips
        ``IN``/``OUT`` to False. The read_loop separately detects the
        closed socket and fires ``reconnect()`` — which brings
        ``online`` back to True, but does NOT reset ``IN``/``OUT``.
        Result: an interface stuck in a zombie state where
        ``online=True`` (data still flows in) but
        ``Transport.outbound()`` silently refuses to send anything ever
        again on it. The only way to reset the flags is a full RNS
        restart, which in a container context means restarting the
        container.

        Why this happens more than you'd think: any transient TCP RST
        from a PMTU boundary crossing, VPN tunnel churn, or ISP
        middlebox will trigger the exception path. In deployments
        behind a VPN with a low tunnel MTU (Gluetun-with-WireGuard is
        the reproducer we found), it happens within minutes of the
        first fetch that generates any packet close to the MTU limit.

        Fix: catch the transient TCP errors specifically and route
        recovery through the socket-close + read_loop.reconnect path,
        which preserves ``IN``/``OUT``. Truly-unexpected exceptions
        still fall through to the original teardown behaviour.
        """
        import socket

        try:
            # HDLC/KISS are inner classes defined at module scope in
            # TCPInterface.py, not separate modules — import the module
            # itself and pull them off it.
            from RNS.Interfaces import TCPInterface as _tcp_mod
            TCPClientInterface = _tcp_mod.TCPClientInterface
            HDLC = _tcp_mod.HDLC
            KISS = _tcp_mod.KISS
        except Exception:
            log.exception(
                "Could not import TCPClientInterface for tx-error patch; "
                "the RNS zombie-interface bug will still cause the "
                "container to eventually stop sending outbound packets."
            )
            return

        # Idempotent: only patch once, even if _init_reticulum runs again.
        if getattr(TCPClientInterface, "_nomadportal_tx_patched", False):
            return

        def _patched(iface, data):
            if iface.online and not iface.detached:
                try:
                    iface.writing = True
                    if iface.kiss_framing:
                        framed = (bytes([KISS.FEND])
                                  + bytes([KISS.CMD_DATA])
                                  + KISS.escape(data)
                                  + bytes([KISS.FEND]))
                    else:
                        framed = (bytes([HDLC.FLAG])
                                  + HDLC.escape(data)
                                  + bytes([HDLC.FLAG]))
                    iface.socket.sendall(framed)
                    iface.writing = False
                    iface.txb += len(data)
                    if (hasattr(iface, "parent_interface")
                            and iface.parent_interface is not None):
                        iface.parent_interface.txb += len(data)
                except (ConnectionResetError, BrokenPipeError,
                        ConnectionAbortedError, socket.timeout) as exc:
                    # Transient TCP-layer failure. NOT calling
                    # teardown() here is the whole point of this patch.
                    # Close the socket so read_loop.recv() returns
                    # empty and fires reconnect() naturally; IN/OUT
                    # stay True and the interface recovers cleanly.
                    RNS.log(
                        "Transient TX error on " + str(iface)
                        + ", closing socket so read_loop reconnects "
                        + "(IN/OUT preserved): " + str(exc),
                        RNS.LOG_WARNING,
                    )
                    iface.writing = False
                    try:
                        iface.socket.close()
                    except Exception:
                        pass
                    iface.online = False
                except Exception as exc:
                    # Something we didn't anticipate — preserve RNS's
                    # original teardown behaviour so we don't hide
                    # genuinely unrecoverable errors.
                    RNS.log(
                        "Exception occurred while transmitting via "
                        + str(iface) + ", tearing down interface",
                        RNS.LOG_ERROR,
                    )
                    RNS.log(
                        "The contained exception was: " + str(exc),
                        RNS.LOG_ERROR,
                    )
                    iface.teardown()

        TCPClientInterface.process_outgoing = _patched
        TCPClientInterface._nomadportal_tx_patched = True
        log.info(
            "Patched RNS.TCPClientInterface.process_outgoing to treat "
            "ConnectionResetError / BrokenPipeError as transient; "
            "interface IN/OUT flags will survive transient TX failures."
        )

    def _get_default_node_hash(self) -> str:
        """Read ``default_node`` from ``ui_settings.json`` on disk.

        The keepalive thread has no Flask request context, so we can't
        use ``current_app.config['UI_SETTINGS']``. Read the file
        directly instead. Returns the destination hex-hash (lowercased)
        or empty string if no default node is configured.
        """
        ui_file = os.path.join(
            os.path.dirname(self._nodes_file), "ui_settings.json"
        )
        if not os.path.exists(ui_file):
            return ""
        try:
            with open(ui_file, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
            val = data.get("default_node", "") or ""
            return val.strip().lower()
        except (OSError, ValueError):
            return ""

    def _default_node_keepalive_loop(self) -> None:
        """Keep the operator-configured default node's link warm.

        RNS's Link.KEEPALIVE (360 s) sends an internal probe on any
        idle link, but the probe requires the peer to respond. During
        mesh-flaky windows the peer's proof gets lost, the link goes
        STALE at 720 s, and both cached-link reuse and fresh
        establishment fail during the window — even though a fresh
        peer round-trip minutes later would succeed.

        By actively fetching the default node's index page every
        ``DEFAULT_NODE_KEEPALIVE_S`` we accomplish three things at
        once:

        1. Keep the link's ``last_data`` counter fresh — RNS won't
           mark it STALE
        2. Detect breakage EARLY. If the ping fails, the retry loop
           establishes a fresh link before the user clicks
        3. Match MeshChat's warm-link behaviour without needing a
           full LXMF router. MeshChat's constant LXMF activity is
           what lets it navigate through mesh-flaky windows; this
           gives NomadPortal the same for a single high-value target
           (the default node)

        Only fires when ``default_node`` is set in Admin → Settings.
        No-op if unset — we won't establish warm links proactively
        for arbitrary destinations.
        """
        self._ready.wait()
        consecutive_failures = 0
        while True:
            try:
                time.sleep(DEFAULT_NODE_KEEPALIVE_S)
                default_hex = self._get_default_node_hash()
                if not default_hex:
                    # No default node configured — nothing to keep warm.
                    consecutive_failures = 0
                    continue
                if self._browser_is_blocked_dest(default_hex):
                    consecutive_failures = 0
                    continue
                try:
                    content, error = self.fetch_page(default_hex, "/page/index.mu")
                    if content is not None:
                        log.info(
                            "default-node keepalive: %s ok (%d bytes)",
                            default_hex[:16], len(content),
                        )
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                        # Not a crash — the link failure is exactly what
                        # this loop's early-detection is for. Next tick
                        # will try again with a fresh establishment.
                        log.info(
                            "default-node keepalive: %s currently unreachable "
                            "(%s); consecutive_failures=%d, next retry in %ds",
                            default_hex[:16],
                            error or "(unknown error)",
                            consecutive_failures,
                            DEFAULT_NODE_KEEPALIVE_S,
                        )
                        if consecutive_failures >= DEFAULT_NODE_HARD_RESET_FAILURES:
                            self._reset_default_node_rns_state(default_hex)
                            consecutive_failures = 0
                except Exception:
                    consecutive_failures += 1
                    log.exception(
                        "default-node keepalive: fetch_page raised for %s "
                        "(consecutive_failures=%d)",
                        default_hex[:16], consecutive_failures,
                    )
            except Exception:
                log.exception(
                    "default-node keepalive loop error; sleeping 60s"
                )
                time.sleep(60)

    def _reset_default_node_rns_state(self, default_hex: str) -> None:
        """After ``DEFAULT_NODE_HARD_RESET_FAILURES`` consecutive keepalive
        failures, forcibly clear RNS's cached state for the default
        destination and fire a fresh ``request_path``.

        This targets the "long-running Transport state degrades
        reachability for specific destinations" pattern documented in
        [[destination-table-cache-is-load-bearing]]. A fresh RNS
        instance in the same container namespace can reach the
        destination fine; the long-running one can't. Fully restarting
        RNS or the container is heavy; this instead surgically pops
        the specific destination's entries from ``path_table`` and
        ``path_requests`` so subsequent path discovery starts from
        scratch. Does not affect other destinations.

        Cheap and low-risk: worst case we lose a stale path we
        couldn't use anyway. If the mesh is still broken for this
        destination the next keepalive attempts will still fail; if
        the stale state was the blocker, they'll now succeed.
        """
        log.warning(
            "default-node keepalive: resetting RNS state for %s after "
            "%d consecutive failures",
            default_hex[:16], DEFAULT_NODE_HARD_RESET_FAILURES,
        )
        RNS = self._rns
        try:
            dh = bytes.fromhex(default_hex)
        except ValueError:
            log.warning(
                "default-node keepalive: invalid default_node hex %s",
                default_hex,
            )
            return

        # Evict our own cached Link (if any) — the retry loop after
        # this reset should build a fresh one.
        try:
            self._evict_cached_link(dh, teardown=True)
        except Exception:
            log.debug("cached-link evict raised", exc_info=True)

        # Clear RNS's path table entry — has_path() will now return
        # False, forcing the fetch code into full path discovery.
        try:
            if dh in RNS.Transport.path_table:
                del RNS.Transport.path_table[dh]
                log.info(
                    "default-node keepalive: cleared path_table entry "
                    "for %s", default_hex[:16],
                )
        except Exception:
            log.debug("path_table clear raised", exc_info=True)

        # Clear the request_path throttle timestamp so an immediate
        # re-request isn't rate-limited by PATH_REQUEST_MI (20 s).
        try:
            if dh in RNS.Transport.path_requests:
                del RNS.Transport.path_requests[dh]
        except Exception:
            log.debug("path_requests clear raised", exc_info=True)

        # Fire a fresh path_request so the mesh has a chance to
        # answer before the next scheduled keepalive tick. Any answer
        # arriving between now and the next tick populates path_table.
        try:
            RNS.Transport.request_path(dh)
            log.info(
                "default-node keepalive: fired fresh request_path for %s",
                default_hex[:16],
            )
        except Exception:
            log.debug("request_path raised", exc_info=True)

        # Force TCP reconnect on our TCPClientInterface(s). The sidecar
        # probe test on 2026-07-18 showed that a fresh RNS instance in
        # the same container namespace reaches destinations that the
        # long-running RNS cannot at the same moment — same LAN, same
        # source IP, same hub. That points at hub-side session state
        # accumulating over hours of the same TCP connection into a
        # shape that degrades routing for specific destinations. A
        # fresh TCP session gets fresh-client treatment. This is the
        # same mechanism that our zombie-interface patch already uses
        # for recovery — close the socket, read_loop's reconnect()
        # fires, kernel gives us a fresh source port, hub sees a new
        # client session.
        #
        # We do this AFTER path state clearing so the fresh request_path
        # above rides the OLD session's last gasps; the reconnect
        # happens naturally when the next outbound packet hits a
        # dead socket. This preserves the "give the mesh a chance to
        # answer via the current session before we replace it"
        # behaviour.
        for iface in RNS.Transport.interfaces:
            if not getattr(iface, "initiator", False):
                continue
            sock = getattr(iface, "socket", None)
            if sock is None:
                continue
            try:
                iface.online = False
                sock.close()
                log.warning(
                    "default-node hard-reset: forced socket close on %s "
                    "to trigger fresh TCP reconnect (fresh hub session)",
                    iface.name,
                )
            except Exception:
                log.debug(
                    "hard-reset socket close raised on %s", iface.name,
                    exc_info=True,
                )

    def _browser_is_blocked_dest(self, hex_hash: str) -> bool:
        """Skip keepalive traffic to blocklisted destinations."""
        try:
            return self.is_blocked(hex_hash)
        except Exception:
            return False

    _RNS_STATS_FILENAME = "rns_init_stats.json"
    _RNS_STATS_MAX_HISTORY = 5

    def _rns_stats_path(self) -> str:
        return os.path.join(
            os.path.dirname(self._nodes_file), self._RNS_STATS_FILENAME,
        )

    def _load_rns_init_history(self) -> list:
        """Read the rolling window of prior RNS init durations. Returns
        an empty list on any error / first run.
        """
        try:
            path = self._rns_stats_path()
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            raw = data.get("durations", [])
            return [float(v) for v in raw
                    if isinstance(v, (int, float)) and v > 0][-self._RNS_STATS_MAX_HISTORY:]
        except Exception:
            log.debug("Could not read RNS init history", exc_info=True)
            return []

    def _save_rns_init_history(self, new_duration: float) -> None:
        """Append this run's duration and trim to the rolling window."""
        try:
            self._rns_init_history.append(float(new_duration))
            self._rns_init_history = self._rns_init_history[-self._RNS_STATS_MAX_HISTORY:]
            path = self._rns_stats_path()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"durations": self._rns_init_history}, fh)
        except Exception:
            log.debug("Could not persist RNS init history", exc_info=True)

    def rns_init_progress(self) -> dict:
        """Snapshot of the current RNS init state for callers who want
        to show a progress hint or ETA. Returns keys:

          * ``ready``               — True once RNS is up.
          * ``starting``            — True while init is in flight.
          * ``elapsed_seconds``     — int seconds since init began (only
                                      while starting).
          * ``estimated_total_seconds`` — median of the persisted history
                                          (None on first run).
          * ``estimated_remaining_seconds`` — max(0, total-elapsed), or
                                              None if no history.
          * ``history_sample_size`` — number of past runs the estimate
                                       is drawn from.
        """
        if self.is_ready():
            return {"ready": True, "starting": False}

        if self._rns_init_start_mono is None:
            # Background thread hasn't reached its body yet — should
            # basically never be observed but guard against it.
            return {"ready": False, "starting": False}

        elapsed = time.monotonic() - self._rns_init_start_mono
        info = {
            "ready":                        False,
            "starting":                     True,
            "elapsed_seconds":              int(elapsed),
            "history_sample_size":          len(self._rns_init_history),
            "estimated_total_seconds":      None,
            "estimated_remaining_seconds":  None,
            "past_estimate":                False,
        }
        if self._rns_init_history:
            # Median instead of mean — resistant to a single outlier
            # (e.g. a run that got stuck on a hub TCP timeout).
            sorted_h = sorted(self._rns_init_history)
            n = len(sorted_h)
            median = (sorted_h[n // 2] if n % 2 == 1
                      else (sorted_h[n // 2 - 1] + sorted_h[n // 2]) / 2)
            info["estimated_total_seconds"]     = int(median)
            info["estimated_remaining_seconds"] = max(0, int(median - elapsed))
            info["past_estimate"]               = elapsed > median
        return info

    def is_ready(self) -> bool:
        """True once the background RNS init has finished successfully."""
        return self._ready.is_set() and self.reticulum is not None

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """Block up to ``timeout`` seconds for RNS to be ready. Returns
        True on success, False on timeout. ``timeout=None`` waits
        forever — use with care on request-handler paths.
        """
        return self._ready.wait(timeout=timeout)

    # ------------------------------------------------------------------
    # Link cache — reused across page fetches to the same destination
    # ------------------------------------------------------------------

    def _cache_link(self, dest_hash: bytes, link) -> None:
        """Store a successfully-established link so the next fetch to
        this destination can skip the establishment handshake. Enforces
        the LINK_CACHE_MAX_SIZE cap (LRU — oldest insertion goes first).
        Also registers a closed_callback so RNS-side link close removes
        the entry automatically.
        """
        stale_dest_link = None
        evicted_link = None
        with self._link_cache_lock:
            # If dest_hash is already cached, pop it first so the
            # re-insertion below moves it to the end of insertion order.
            # Plain ``self._link_cache[dest_hash] = link`` does NOT
            # reorder an existing key in Python's dict — so a
            # heavily-reused destination first cached at boot would
            # become the perpetual eviction target when the cap is hit,
            # exactly the opposite of what an LRU should do.
            existing = self._link_cache.pop(dest_hash, None)
            if existing is not None and existing is not link:
                # Caller replaced a previous link for this dest with a
                # different one; the old one needs teardown so we don't
                # leak an RNS-side session. If it's the same object
                # (refresh-after-cache-hit case), leave it alone.
                stale_dest_link = existing
            if len(self._link_cache) >= LINK_CACHE_MAX_SIZE:
                # Cap reached (we didn't just pop an existing entry
                # above; that would have left us at cap-1). Evict oldest
                # insertion (dict order preserves that in Python 3.7+).
                first_key = next(iter(self._link_cache))
                evicted_link = self._link_cache.pop(first_key)
            self._link_cache[dest_hash] = link

        # Tear down outside the lock to avoid holding it during a
        # network operation.
        if stale_dest_link is not None:
            try:
                stale_dest_link.teardown()
            except Exception:
                pass
        if evicted_link is not None:
            try:
                evicted_link.teardown()
            except Exception:
                pass

        # Auto-evict on RNS-side close. We ignore failures since the
        # callback registration is optional cleanup, not correctness.
        def _on_close(closed_link):
            with self._link_cache_lock:
                if self._link_cache.get(dest_hash) is closed_link:
                    self._link_cache.pop(dest_hash, None)
        try:
            link.set_link_closed_callback(_on_close)
        except Exception:
            pass

    def _evict_cached_link(self, dest_hash: bytes, teardown: bool = True) -> None:
        """Remove a link from the cache. Tears it down by default; pass
        ``teardown=False`` when the link is already dead / being reused
        by the caller.
        """
        with self._link_cache_lock:
            link = self._link_cache.pop(dest_hash, None)
        if link is not None and teardown:
            try:
                link.teardown()
            except Exception:
                pass

    def _get_cached_link(self, dest_hash: bytes):
        """Return the cached link for this destination if it's still
        marked ACTIVE by RNS, otherwise None (and evict a stale entry).
        """
        RNS = self._rns
        with self._link_cache_lock:
            link = self._link_cache.get(dest_hash)
        if link is None:
            return None
        try:
            if link.status == RNS.Link.ACTIVE:
                return link
        except Exception:
            pass
        # Stale entry — evict without teardown (caller may already own it).
        self._evict_cached_link(dest_hash, teardown=False)
        try:
            link.teardown()
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_nodes(self, user_sub: str = "", default_hash: str = "") -> list:
        """Return nodes sorted: hosted first, default second, then favorites,
        then by last_seen.

        `default_hash` is the operator-configured default node from UI
        settings. We auto-favorite it (alongside the hosted node) for every
        audience — guests and logged-in users alike — and synthesise a
        placeholder if it hasn't announced yet, so visitors always see it
        in the sidebar even before any RNS announce arrives.
        """
        hosted  = self._hosted_hash.lower() if self._hosted_hash else ""
        default = (default_hash or "").lower()
        with self._lock:
            nodes = [dict(n) for n in self.nodes.values()]
            # A node is "favorited" iff (hash, "/") is bookmarked. Page-only
            # bookmarks (path != "/") don't light up the node-list star.
            fav_set = {
                f["hash"]
                for f in self._favorites.get(user_sub, [])
                if (f.get("path") or "/") == "/"
            } if user_sub else set()

        # Synthesise a placeholder if the hosted node hasn't announced yet.
        if hosted and not any(n["hash"] == hosted for n in nodes):
            nodes.append({
                "hash":           hosted,
                "name":           self._hosted_name or "This Node",
                "first_seen":     time.time(),
                "last_seen":      time.time(),
                "announce_count": 0,
                "view_count":     0,
                "rx_bytes":       0,
                "last_load_ms":   None,
                "avg_load_ms":    None,
                "last_ping_ms":   None,
                "last_load_ok":   None,
                "ever_load_ok":   False,
                "favorited":      False,
            })

        # Same treatment for the configured default node — placeholder so
        # visitors see it pinned at the top of the sidebar even before any
        # announce, even if it's never been reached over RNS yet.
        if default and default != hosted and not any(n["hash"] == default for n in nodes):
            nodes.append({
                "hash":           default,
                "name":           "Default Node",
                "first_seen":     time.time(),
                "last_seen":      0.0,
                "announce_count": 0,
                "view_count":     0,
                "rx_bytes":       0,
                "last_load_ms":   None,
                "avg_load_ms":    None,
                "last_ping_ms":   None,
                "last_load_ok":   None,
                "ever_load_ok":   False,
                "favorited":      False,
            })

        needs_persist = False
        for node in nodes:
            node["is_hosted"]  = node["hash"] == hosted
            node["is_default"] = bool(default) and node["hash"] == default
            if node["is_hosted"]:
                # Locally-hosted destinations aren't in the path table, so
                # hops_to() returns the sentinel. Pin to 0 → renders "local".
                node["hops"] = 0
            else:
                live = self._hop_count(node["hash"])
                if live is not None:
                    node["hops"] = live
                    # Cache last known hops so it survives restarts/path-table flushes.
                    # Treat as FYI — the live value always wins when available.
                    with self._lock:
                        stored = self.nodes.get(node["hash"])
                        if stored and stored.get("last_known_hops") != live:
                            stored["last_known_hops"] = live
                            needs_persist = True
                else:
                    node["hops"] = node.get("last_known_hops")
            node["favorited"] = (
                node["is_hosted"]
                or node["is_default"]
                or node["hash"] in fav_set
            )
            # Always reflect the current name for the hosted node.
            if node["is_hosted"] and self._hosted_name:
                node["name"] = self._hosted_name

        if needs_persist:
            self._mark_nodes_dirty()

        nodes.sort(key=lambda n: (
            not n["is_hosted"],
            not n["is_default"],
            not n["favorited"],
            -n["last_seen"],
        ))
        return nodes

    def get_node(self, hash_hex: str) -> Optional[dict]:
        with self._lock:
            r = self.nodes.get(hash_hex.lower())
            return dict(r) if r else None

    def get_status(self) -> dict:
        RNS = self._rns
        interfaces = []
        iface_snapshot: dict = {}
        try:
            for iface in RNS.Transport.interfaces:
                name     = getattr(iface, "name", str(iface))
                sess_rxb = getattr(iface, "rxb", getattr(iface, "rx_bytes", 0)) or 0
                sess_txb = getattr(iface, "txb", getattr(iface, "tx_bytes", 0)) or 0
                base     = self._iface_base.get(name, {"rxb": 0, "txb": 0})
                life_rxb = base["rxb"] + sess_rxb
                life_txb = base["txb"] + sess_txb
                iface_snapshot[name] = {"rxb": life_rxb, "txb": life_txb}
                interfaces.append({
                    "name":      name,
                    "online":    getattr(iface, "online", None),
                    "rxb":       sess_rxb,
                    "txb":       sess_txb,
                    "life_rxb":  life_rxb,
                    "life_txb":  life_txb,
                })
        except Exception:
            pass

        if iface_snapshot:
            self._save_iface_stats(iface_snapshot)

        with self._lock:
            return {
                "interfaces":       interfaces,
                "nodes_discovered": len(self.nodes),
                "total_announces":  self._total_announces,
            }

    def _get_fetch_lock(self, dest_hash: bytes) -> threading.Lock:
        """Get-or-create the per-destination fetch mutex. Lazy —
        first fetch to a given destination creates the lock, later
        fetches to the same destination reuse and contend on it.
        """
        with self._inflight_fetches_lock:
            lock = self._inflight_fetches.get(dest_hash)
            if lock is None:
                lock = threading.Lock()
                self._inflight_fetches[dest_hash] = lock
            return lock

    def fetch_page(
        self,
        destination_hash_hex: str,
        path: str = "/",
        field_data: Optional[dict] = None,
        timeout: int = STALL_TIMEOUT,
        progress_cb=None,
        sizes_cb=None,
        identify_with=None,
    ) -> tuple[Optional[bytes], Optional[str]]:
        """Fetch a page and update per-node stats (views, RX bytes, load time).

        Serializes concurrent fetches to the same destination behind
        a per-destination mutex — see ``self._inflight_fetches``. The
        actual work runs in ``_fetch_page_locked``; this thin wrapper
        only handles readiness checks, dest_hash parsing, and the
        lock. Different-destination fetches still run in parallel.
        """
        # RNS.Reticulum() is initialised in a background thread so the web
        # UI can serve while Transport comes up (~2-4 min on a busy
        # deployment). Reject fetches gracefully during that window rather
        # than crashing on RNS.Transport access. Include the ETA when
        # we have prior-run history to base it on.
        if not self.is_ready():
            prog = self.rns_init_progress()
            remaining = prog.get("estimated_remaining_seconds")
            if prog.get("past_estimate"):
                elapsed = prog.get("elapsed_seconds", 0)
                return None, (f"Reticulum transport is still coming up "
                              f"({elapsed}s elapsed, past the usual estimate)")
            if remaining is not None:
                return None, f"Reticulum transport is still coming up (~{remaining}s remaining)"
            return None, "Reticulum transport is still coming up"

        try:
            dest_hash = bytes.fromhex(destination_hash_hex)
        except ValueError:
            return None, "Invalid destination hash"

        with self._get_fetch_lock(dest_hash):
            return self._fetch_page_locked(
                destination_hash_hex, dest_hash, path, field_data,
                timeout, progress_cb, sizes_cb, identify_with,
            )

    def _fetch_page_locked(
        self,
        destination_hash_hex: str,
        dest_hash: bytes,
        path: str = "/",
        field_data: Optional[dict] = None,
        timeout: int = STALL_TIMEOUT,
        progress_cb=None,
        sizes_cb=None,
        identify_with=None,
    ) -> tuple[Optional[bytes], Optional[str]]:
        """Actual fetch implementation — runs under the
        per-destination fetch mutex acquired by ``fetch_page``.
        """
        RNS = self._rns

        # Only update the node's status dot for the root/index page, not sub-pages.
        _norm = (path or "/").rstrip("/") or "/"
        is_index = _norm in ("/", "/index.mu", "/page/index.mu")

        # Always ensure we have a live path BEFORE recalling the identity.
        # `Identity.recall` succeeds from cache even after the path table has
        # evicted the route, which would cause Link establishment to silently
        # fail. Locally-hosted destinations have no path-table entry but are
        # in Transport.destinations — recall works for those without a path.
        is_local = False
        try:
            for d in RNS.Transport.destinations:
                if getattr(d, "hash", None) == dest_hash:
                    is_local = True
                    break
        except Exception:
            pass

        if not is_local and not RNS.Transport.has_path(dest_hash):
            log.info("Requesting path to %s", destination_hash_hex)
            RNS.Transport.request_path(dest_hash)
            path_start   = time.time()
            deadline     = path_start + PATH_TIMEOUT
            # Re-issue the path request every 15s inside the polling
            # window. RNS.Transport.request_path is idempotent, and
            # broadcast packets do get dropped — sending once and then
            # waiting silently for 60s means a single lost initial
            # request wastes the whole window. Four attempts (t=0/15/30/45)
            # gives the mesh a fair chance to answer even when packets
            # go missing. This is one of the differences between
            # NomadPortal and clients that reach the same destinations
            # more reliably.
            next_retry = path_start + 15
            while not RNS.Transport.has_path(dest_hash):
                now = time.time()
                if now > deadline:
                    log.warning(
                        "fetch_page: path discovery timed out for %s",
                        destination_hash_hex[:16],
                    )
                    self._record_fetch(destination_hash_hex.lower(), 0, 0, ok=False,
                                       update_status=is_index)
                    return None, "Path not found — node may be unreachable"
                if now >= next_retry:
                    log.info(
                        "Re-requesting path to %s (still no path after %ds)",
                        destination_hash_hex[:16],
                        int(now - path_start),
                    )
                    RNS.Transport.request_path(dest_hash)
                    next_retry = now + 15
                time.sleep(0.1)

        identity = RNS.Identity.recall(dest_hash)
        if identity is None:
            # Path arrived (or local), but identity material hasn't been
            # delivered yet. Wait briefly for an announce to fill it in.
            deadline = time.time() + 5
            while identity is None and time.time() < deadline:
                time.sleep(0.1)
                identity = RNS.Identity.recall(dest_hash)

        if identity is None:
            log.warning(
                "fetch_page: identity not recalled for %s (path %s)",
                destination_hash_hex[:16],
                "yes" if RNS.Transport.has_path(dest_hash) else "no",
            )
            self._record_fetch(destination_hash_hex.lower(), 0, 0, ok=False,
                               update_status=is_index)
            return None, "Identity not recalled — try again shortly"

        destination = RNS.Destination(
            identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            APP_NAME,
            NODE_ASPECT,
        )

        t_start = time.monotonic()

        # Fetch is wrapped in a small retry loop. RNS link establishment
        # over an unstable mesh path fails intermittently — a common
        # symptom is "Link closed before response" while other clients
        # (MeshChat, Sideband) on the same mesh reach the same host fine.
        # A single-attempt fetch turned every transient blip into a
        # user-visible failure. Retrying with a fresh path lookup handles
        # the common stale-route case; the total attempts is bounded so a
        # genuinely-unreachable destination still fails within a
        # reasonable time budget.
        MAX_ATTEMPTS      = 3
        RETRYABLE_ERRORS  = ("Link closed before response", "Page request failed")

        def _do_attempt(existing_link=None):
            """Run one link+request+wait attempt. Returns
            ``(result, link)``. On success the caller is expected to
            cache the returned link (skips the establishment handshake
            on the next fetch); on failure the link is torn down inside
            this function and ``None`` is returned in its place.

            When ``existing_link`` is provided, it's treated as already
            established (from the cache) — we skip creating a fresh
            RNS.Link and go straight to the request. Link is NOT torn
            down on failure in that case (the cache-hit caller does
            that so we can leave the fallback to the fresh-flow retry
            loop).

            State (result, done, callbacks) is fresh per call so a
            retry doesn't inherit stale closures.
            """
            result: dict = {"content": None, "error": None}
            done = threading.Event()
            last_activity = [time.monotonic()]
            link_active = [existing_link is not None]
            progress_started = [False]

            def _bump():
                last_activity[0] = time.monotonic()

            def _on_response(receipt):
                # NomadNet ships three different response shapes through the
                # same `receipt.response` slot — bytes for pages, `(name_bytes,
                # data_bytes)` for small files, and `io.BufferedReader` for
                # large files streamed via an RNS Resource. Treating any of
                # them as `bytes(...)` blindly was raising TypeError on file
                # responses; the exception killed the callback before
                # `done.set()`, so the fetch sat waiting until the link closed
                # — producing the misleading "Link closed before response"
                # error even though the file had transferred successfully.
                try:
                    resp = receipt.response
                    if resp is None:
                        result["content"] = b""
                        result["error"] = "Empty response from node"
                    elif isinstance(resp, tuple) and len(resp) >= 2:
                        # (file_name_bytes, file_data_bytes) — mirrors NomadNet's
                        # textui Browser handling at file_received().
                        result["content"] = bytes(resp[1])
                    elif hasattr(resp, "read"):
                        # io.BufferedReader pointing at the streamed temp file.
                        try:
                            resp.seek(0)
                        except Exception:
                            pass
                        result["content"] = resp.read()
                        try:
                            resp.close()
                        except Exception:
                            pass
                    else:
                        # Page response (and anything else that accepts bytes()).
                        result["content"] = bytes(resp)
                except Exception as exc:
                    log.exception("fetch response handler raised on %s",
                                  destination_hash_hex[:16])
                    result["content"] = b""
                    result["error"] = f"Response handler error: {exc}"
                done.set()

            def _on_failed(receipt):
                result["error"] = "Page request failed"
                done.set()

            def _on_progress(receipt):
                _bump()
                progress_started[0] = True
                # Capture byte-level transfer metrics if RNS exposes them on the
                # receipt — these are populated for both page and file requests
                # by RNS as soon as the response size is negotiated. Lets the
                # web UI show "12.3 MB of 4.5 MB transferred" instead of just a
                # percentage.
                try:
                    total = getattr(receipt, "response_size", None)
                    xfer  = getattr(receipt, "response_transfer_size", None)
                    if total is not None:
                        result["response_size"] = int(total)
                    if xfer is not None:
                        result["transfer_size"] = int(xfer)
                    if sizes_cb is not None and (total is not None or xfer is not None):
                        try:
                            sizes_cb(
                                int(total) if total is not None else None,
                                int(xfer)  if xfer  is not None else None,
                            )
                        except Exception:
                            pass
                except Exception:
                    pass
                if progress_cb is not None:
                    try:
                        progress_cb(float(getattr(receipt, "progress", 0.0) or 0.0))
                    except Exception:
                        pass  # never let a progress callback break the fetch

            def _on_link_established(link):
                _bump()
                link_active[0] = True
                # Identify the link BEFORE the request so the site server
                # processes this fetch as an identified request (var_fingerprint,
                # etc). Bare identifies on idle links are typically ignored —
                # NomadNet only acts on identification while serving a page.
                if identify_with is not None:
                    try:
                        link.identify(identify_with)
                        log.info(
                            "Identified link to %s as %s",
                            destination_hash_hex[:16],
                            identify_with.hexhash[:16],
                        )
                    except Exception as exc:
                        log.warning("link.identify failed: %s", exc)
                p = (path or "/").rstrip("/") or "/"
                if p.startswith("/page/") or p.startswith("/file/"):
                    # Path already includes its resource prefix from the URL —
                    # /page/ for NomadNet pages, /file/ for binary files. Both
                    # use the same Link.request mechanism on the NomadNet side.
                    rns_path = p
                else:
                    rns_path = "/page/" + (p.lstrip("/") or "index.mu")

                # Field/var data must be a dict; NomadNet filters keys by prefix.
                req_data = None
                if field_data:
                    req_data = {}
                    for k, v in field_data.items():
                        if k.startswith("field_") or k.startswith("var_"):
                            req_data[k] = v
                        else:
                            req_data[f"field_{k}"] = v

                log.debug("Link established, requesting '%s'", rns_path)
                request_receipt = link.request(
                    rns_path,
                    data=req_data,
                    response_callback=_on_response,
                    failed_callback=_on_failed,
                    progress_callback=_on_progress,
                    timeout=PAGE_HARD_CAP,
                )
                # RNS.Link.request returns False if the send failed
                # outright — link went CLOSED between our cache-hit
                # check and the send, or RNS.Transport.outbound() found
                # no interface to send on. In either case no callback
                # will ever fire, and without this check we sit in the
                # stall watchdog for 30s until it aborts with a
                # misleading "No response from node" error. Report the
                # real failure immediately so the retry loop can try a
                # fresh link.
                if request_receipt is False:
                    log.info(
                        "fetch_page: link.request to %s returned False "
                        "(link closed or interface unavailable at send "
                        "time); aborting attempt",
                        destination_hash_hex[:16],
                    )
                    result["error"] = "Link closed before response"
                    done.set()

            def _on_link_closed(link):
                if not done.is_set():
                    result["error"] = "Link closed before response"
                    done.set()

            if existing_link is not None:
                # Cache-hit path: link is already ACTIVE. Re-register the
                # closed callback (the one registered by _cache_link only
                # handles eviction) and fire the request path directly —
                # effectively simulating what _on_link_established would
                # have done for a fresh link.
                link = existing_link
                try:
                    link.set_link_closed_callback(_on_link_closed)
                except Exception:
                    pass
                _on_link_established(link)
            else:
                link = RNS.Link(
                    destination,
                    established_callback=_on_link_established,
                    closed_callback=_on_link_closed,
                )

            # Stall watchdog: only active AFTER the link has been established.
            # Before establishment, we use LINK_ESTABLISH_TIMEOUT (a shorter
            # window than PAGE_HARD_CAP) as the pre-establishment abort. RNS
            # empirically doesn't always fire _on_link_closed for unreachable
            # destinations — the link sits in HANDSHAKE state and only the
            # 10-minute hard cap eventually breaks us out, producing the
            # "click, wait 30 minutes because retry × 3" UX we've been
            # seeing. 60 s is far longer than a healthy handshake (2-8 s)
            # and comfortably fits multi-hop paths; anything longer is
            # almost certainly a dead route where retry might still catch
            # a fresh path.
            attempt_start = time.monotonic()
            hard_deadline = attempt_start + PAGE_HARD_CAP
            # Once the resource transfer reaches 100%, RNS still needs to do a
            # final ack handshake before response_callback fires with the data.
            # On slow/multi-hop links this finalisation can take much longer
            # than the in-flight stall timeout — applying the regular 30s
            # watchdog at that point falsely aborts otherwise-successful file
            # downloads (resource arrives at 100%, callback is seconds away,
            # we time out anyway). Use a larger timeout once we're in
            # finalisation so we're patient enough for the conclude/teardown.
            FINALISE_TIMEOUT = 180  # seconds — generous post-100% grace
            while not done.is_set():
                now = time.monotonic()
                if not link_active[0]:
                    # Pre-establishment window. Give it up to
                    # LINK_ESTABLISH_TIMEOUT for RNS to fire the
                    # established callback (or the closed callback);
                    # if neither happens the link is silently stuck.
                    # Treat as a retryable link failure so the outer
                    # retry loop can request a fresh path and try again.
                    if now - attempt_start >= LINK_ESTABLISH_TIMEOUT:
                        log.info(
                            "fetch_page: link to %s failed to establish "
                            "in %ds, aborting attempt",
                            destination_hash_hex[:16], LINK_ESTABLISH_TIMEOUT,
                        )
                        result["error"] = "Link closed before response"
                        break
                elif link_active[0]:
                    idle = now - last_activity[0]
                    # Has the transfer reached its full advertised size?
                    rsize = result.get("response_size")
                    tsize = result.get("transfer_size")
                    in_finalise = rsize is not None and tsize is not None and tsize >= rsize > 0
                    stall_cap = FINALISE_TIMEOUT if in_finalise else timeout
                    if idle >= stall_cap:
                        if in_finalise:
                            result["error"] = (
                                f"Resource transfer completed but the node never "
                                f"finalised the response within {FINALISE_TIMEOUT}s"
                            )
                        else:
                            result["error"] = (
                                f"Lost connection — no data for {timeout}s"
                                if progress_started[0]
                                else f"No response from node ({timeout}s)"
                            )
                        break
                if now >= hard_deadline:
                    result["error"] = f"Page fetch exceeded hard cap ({PAGE_HARD_CAP}s)"
                    break
                done.wait(timeout=1.0)

            # Only tear down on failure. Successful attempts return the
            # live link so the caller can cache it — the next fetch to
            # this destination skips the ~2-8 s establishment handshake.
            #
            # Cache-hit case (existing_link is not None): don't tear down
            # regardless; the outer cache-hit caller decides whether the
            # link is still usable or should be evicted-and-fresh.
            if result["content"] is None and existing_link is None:
                try:
                    link.teardown()
                except Exception:
                    pass
                return result, None

            return result, link

        # Cache-hit fast path — skip the whole retry loop if we have a
        # still-live link. If it works, we save the entire establishment
        # handshake (~2-8 s) and the retry-loop overhead. If it fails,
        # we evict the cached link (probably dead despite ACTIVE status)
        # and fall through to the fresh-establishment path below.
        final_result: dict = {"content": None, "error": None}
        cached_link = self._get_cached_link(dest_hash)
        if cached_link is not None:
            # Log at INFO so operators can see cache activity without
            # cranking log level. It's diagnostic gold when someone says
            # "the second click was slow" — presence/absence of this
            # line answers "did the cache hit."
            log.info(
                "fetch_page: reusing cached link for %s",
                destination_hash_hex[:16],
            )
            result, _link = _do_attempt(existing_link=cached_link)
            if result["content"] is not None:
                final_result = result
                # cached_link is still fine; leave it in place. Also
                # re-register the eviction closed_callback — _do_attempt
                # overwrote it with its own local one for the fetch, so
                # if RNS closes this link later we still want the cache
                # entry cleaned up.
                self._cache_link(dest_hash, cached_link)
            else:
                # Cached link failed mid-request. Evict and let the
                # fresh-flow retry loop take over.
                log.info(
                    "fetch_page: cached link for %s failed (%s); "
                    "establishing fresh",
                    destination_hash_hex[:16],
                    result.get("error") or "?",
                )
                self._evict_cached_link(dest_hash, teardown=True)
                cached_link = None

        if final_result["content"] is None:
            # Announce-waiter registered for the duration of the retry
            # loop. Between attempts we fire a path_request and then
            # wait for a fresh announce from the destination — the
            # path_request triggers NomadNet nodes to re-announce, and
            # the announce arriving is our cleanest signal that the
            # mesh has produced a valid path for us to try.
            waiter = _DestinationAnnounceWaiter(dest_hash)
            try:
                RNS.Transport.register_announce_handler(waiter)
            except Exception:
                log.debug(
                    "fetch_page: could not register announce waiter for %s",
                    destination_hash_hex[:16], exc_info=True,
                )
            try:
                for attempt in range(MAX_ATTEMPTS):
                    result, link_obj = _do_attempt()
                    if result["content"] is not None:
                        final_result = result
                        # Cache the successfully-established link so the
                        # next fetch to this destination skips establishment.
                        if link_obj is not None:
                            self._cache_link(dest_hash, link_obj)
                        break
                    final_result = result
                    err = result.get("error") or ""
                    # Only retry on transient link failures. Path-discovery
                    # timeouts, hard-cap breaches, and stall/finalise errors
                    # imply either an unreachable destination or a stuck
                    # transfer — retrying would just wait through it again.
                    if err not in RETRYABLE_ERRORS or attempt == MAX_ATTEMPTS - 1:
                        break
                    log.info(
                        "fetch_page: retrying %s (attempt %d/%d after: %s)",
                        destination_hash_hex[:16], attempt + 2, MAX_ATTEMPTS, err,
                    )
                    try:
                        RNS.Transport.request_path(dest_hash)
                    except Exception:
                        pass
                    if waiter.wait_and_reset(timeout=RETRY_ANNOUNCE_WAIT):
                        log.info(
                            "fetch_page: fresh announce arrived during "
                            "retry wait for %s — retrying immediately",
                            destination_hash_hex[:16],
                        )

                # Final salvage. If the retry budget exhausted with a
                # retryable error, fire one more path_request and wait
                # for a fresh announce; if one arrives, do one bonus
                # attempt. Real-world case that motivated this:
                # attempts failed at 189 s, a fresh announce arrived
                # 7 s later and the diagnostics endpoint immediately
                # showed has_path=true. The stock retry budget was
                # just short of the mesh's round-trip response time.
                if (
                    final_result["content"] is None
                    and (final_result.get("error") or "") in RETRYABLE_ERRORS
                ):
                    try:
                        RNS.Transport.request_path(dest_hash)
                    except Exception:
                        pass
                    if waiter.wait_and_reset(timeout=FINAL_ANNOUNCE_WAIT):
                        log.info(
                            "fetch_page: fresh announce arrived after "
                            "retry budget for %s — one bonus attempt",
                            destination_hash_hex[:16],
                        )
                        result, link_obj = _do_attempt()
                        if result["content"] is not None:
                            final_result = result
                            if link_obj is not None:
                                self._cache_link(dest_hash, link_obj)
            finally:
                try:
                    RNS.Transport.deregister_announce_handler(waiter)
                except Exception:
                    pass

        load_ms = int((time.monotonic() - t_start) * 1000)

        success = final_result["content"] is not None
        self._record_fetch(
            destination_hash_hex.lower(),
            rx_bytes=len(final_result["content"]) if success else 0,
            load_ms=load_ms,
            ok=success,
            update_status=is_index,
        )

        if not success:
            log.warning(
                "fetch_page failed for %s%s after %dms: %s",
                destination_hash_hex[:16], path, load_ms,
                final_result["error"] or "(unknown error)",
            )
        return final_result["content"], final_result["error"]

    # ------------------------------------------------------------------
    # Async page-fetch with progress tracking — drives the polling UI.
    # ------------------------------------------------------------------

    def fetch_page_async(
        self,
        destination_hash_hex: str,
        path: str = "/",
        field_data: Optional[dict] = None,
        identify_with=None,
    ) -> str:
        """Start a page fetch on a background thread and return a job ID.

        Caller polls `get_job(job_id)` until status != 'fetching'.
        Job entries are kept for ~5 min after completion so the client
        has a window to retrieve the result; older entries are evicted
        by `cleanup_jobs()` (called periodically from the housekeeping thread).
        """
        import secrets
        job_id = secrets.token_hex(8)
        # Opportunistic cleanup of any abandoned jobs before adding a new one.
        self.cleanup_jobs()
        with self._jobs_lock:
            self._jobs[job_id] = {
                "status":        "fetching",
                "progress":      0.0,
                "node_hash":     destination_hash_hex.lower(),
                "path":          path,
                "started":       time.time(),
                "completed":     None,
                "content":       None,
                "error":         None,
                "response_size": None,
                "transfer_size": None,
                "scan_result":   None,
            }

        def _set_progress(p):
            with self._jobs_lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["progress"] = p

        def _set_sizes(response_size, transfer_size):
            with self._jobs_lock:
                if job_id in self._jobs:
                    if response_size is not None:
                        self._jobs[job_id]["response_size"] = response_size
                    if transfer_size is not None:
                        self._jobs[job_id]["transfer_size"] = transfer_size

        def _worker():
            try:
                content, error = self.fetch_page(
                    destination_hash_hex, path, field_data,
                    progress_cb=_set_progress,
                    sizes_cb=_set_sizes,
                    identify_with=identify_with,
                )
                # Move to "scanning" before running the (possibly slow)
                # virus scanner so the polling UI can show a distinct
                # state. Page fetches skip scanning entirely; only file
                # fetches go through the scanner.
                scan_dict = None
                if content is not None and not error and path.startswith("/file/"):
                    scanner       = getattr(self, "scanner", None)
                    scan_required = getattr(self, "scan_required", False)
                    if scanner is not None:
                        with self._jobs_lock:
                            if job_id in self._jobs:
                                self._jobs[job_id]["status"] = "scanning"
                        filename = path.rsplit("/", 1)[-1]
                        try:
                            scan = scanner.scan(content, filename)
                        except Exception as exc:
                            log.exception("Virus scanner raised")
                            from .scanner import ScanResult
                            scan = ScanResult(
                                verdict="unavailable",
                                engine=getattr(scanner, "engine_name", "?"),
                                detail=f"scanner exception: {exc}",
                            )
                        scan_dict = scan.to_dict()
                        if scan.blocked:
                            error   = (
                                f"Virus scan blocked download: "
                                f"{scan.signature or 'malicious content detected'}"
                            )
                            content = None
                        elif scan.verdict == "unavailable" and scan_required:
                            error   = (
                                f"Virus scanner unavailable and VIRUS_SCAN=required "
                                f"is set: {scan.detail or 'no detail'}"
                            )
                            content = None
                with self._jobs_lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["content"]     = content
                        self._jobs[job_id]["error"]       = error
                        self._jobs[job_id]["status"]      = "error" if error else "done"
                        self._jobs[job_id]["progress"]    = 1.0 if content else self._jobs[job_id]["progress"]
                        self._jobs[job_id]["completed"]   = time.time()
                        self._jobs[job_id]["scan_result"] = scan_dict
            except Exception as exc:
                log.exception("fetch_page_async worker crashed")
                with self._jobs_lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["status"]    = "error"
                        self._jobs[job_id]["error"]     = f"Internal error: {exc}"
                        self._jobs[job_id]["completed"] = time.time()

        threading.Thread(target=_worker, daemon=True, name=f"fetch-{job_id}").start()
        return job_id

    def get_job(self, job_id: str) -> Optional[dict]:
        """Snapshot of a job's current state, or None if unknown / evicted."""
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            return dict(j) if j else None

    def drop_job(self, job_id: str, grace_seconds: int = 0) -> None:
        """Evict a job entry — call after the client has retrieved the result.

        ``grace_seconds`` defers the actual eviction to
        ``cleanup_jobs()``, keeping the entry serveable for that
        window. Load-bearing for browsers that re-request the
        download URL from a separate context after the initial
        page-context request — DuckDuckGo's Android download
        manager does this (WebView fetches from the page, then
        DDG's download-handler process re-requests the same URL
        to actually persist the file). Without a grace window,
        the second request hits 404 and DDG reports "Failed to
        download. Check Internet connection." even though the
        WebView successfully received all bytes.

        ``grace_seconds=0`` (default) preserves the historical
        behaviour: immediate eviction. Callers that know a legit
        double-fetch is possible pass a small window (30-60 s).
        """
        with self._jobs_lock:
            if grace_seconds <= 0:
                self._jobs.pop(job_id, None)
                return
            j = self._jobs.get(job_id)
            if j is not None:
                j["_drop_after"] = time.time() + grace_seconds

    def cleanup_jobs(self, max_age: int = 300) -> int:
        """Evict completed jobs older than max_age seconds OR past
        their grace expiry set by ``drop_job(grace_seconds=…)``.
        Returns count removed.
        """
        now = time.time()
        cutoff = now - max_age
        with self._jobs_lock:
            stale = [
                jid for jid, j in self._jobs.items()
                if (j.get("completed") and j["completed"] < cutoff)
                or (j.get("_drop_after") and j["_drop_after"] < now)
            ]
            for jid in stale:
                del self._jobs[jid]
            return len(stale)

    def ping_node(
        self, destination_hash_hex: str, timeout: int = PING_TIMEOUT
    ) -> tuple[Optional[int], Optional[str]]:
        """Measure link-establishment time (ms) as a network latency proxy.

        Returns (latency_ms, error_string).  Exactly one will be None.
        """
        RNS = self._rns
        try:
            dest_hash = bytes.fromhex(destination_hash_hex)
        except ValueError:
            return None, "Invalid destination hash"

        is_local = False
        try:
            for d in RNS.Transport.destinations:
                if getattr(d, "hash", None) == dest_hash:
                    is_local = True
                    break
        except Exception:
            pass

        if not is_local and not RNS.Transport.has_path(dest_hash):
            # Same re-issue pattern as fetch_page: broadcast path
            # requests get dropped, so send one every 15s inside the
            # 60s window instead of relying on the initial packet.
            RNS.Transport.request_path(dest_hash)
            path_start = time.time()
            deadline   = path_start + PATH_TIMEOUT
            next_retry = path_start + 15
            while not RNS.Transport.has_path(dest_hash):
                now = time.time()
                if now > deadline:
                    return None, "No path to node"
                if now >= next_retry:
                    RNS.Transport.request_path(dest_hash)
                    next_retry = now + 15
                time.sleep(0.1)

        identity = RNS.Identity.recall(dest_hash)
        if identity is None:
            deadline = time.time() + 5
            while identity is None and time.time() < deadline:
                time.sleep(0.1)
                identity = RNS.Identity.recall(dest_hash)

        if identity is None:
            return None, "Identity not recalled"

        destination = RNS.Destination(
            identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            APP_NAME,
            NODE_ASPECT,
        )

        done   = threading.Event()
        result = {"ms": None, "error": None}
        t0     = time.monotonic()

        def _established(link):
            result["ms"] = int((time.monotonic() - t0) * 1000)
            done.set()
            try:
                link.teardown()
            except Exception:
                pass

        def _closed(link):
            if not done.is_set():
                result["error"] = "Link closed before established"
                done.set()

        RNS.Link(
            destination,
            established_callback=_established,
            closed_callback=_closed,
        )

        if not done.wait(timeout=timeout):
            return None, f"Timeout ({timeout}s)"

        if result["ms"] is not None:
            self._record_ping(destination_hash_hex.lower(), result["ms"])

        return result["ms"], result["error"]

    def set_favorite(
        self,
        hash_hex: str,
        value: bool,
        user_sub: str = "",
        path: str = "/",
        name: str = "",
    ) -> bool:
        """Add or remove a favorite identified by (hash, path).

        For the index-page case (path="/"), if no name is given we fall
        back to the node's announced name, mirroring legacy behaviour.
        """
        hash_hex = hash_hex.lower()
        path = path or "/"
        # The hosted node's index is always favorited and cannot be changed.
        if (
            self._hosted_hash
            and hash_hex == self._hosted_hash.lower()
            and path == "/"
        ):
            return False
        with self._lock:
            # Index-page favorites still require the node to exist (existing
            # behaviour for the node-list star). Page favorites are accepted
            # even if the node hasn't announced yet — useful for bookmarking
            # a manually-typed address.
            if path == "/" and self.nodes.get(hash_hex) is None:
                return False

            if user_sub:
                favs = self._favorites.setdefault(user_sub, [])
                idx = next(
                    (i for i, f in enumerate(favs)
                     if f["hash"] == hash_hex and (f.get("path") or "/") == path),
                    -1,
                )
                if value and idx == -1:
                    fav_name = name.strip() if name else (
                        self.nodes.get(hash_hex, {}).get("name", "")
                        or hash_hex[:16]
                    )
                    favs.append({
                        "hash":  hash_hex,
                        "path":  path,
                        "name":  fav_name,
                        "added": time.time(),
                    })
                elif not value and idx >= 0:
                    favs.pop(idx)
                fav_snapshot = dict(self._favorites)
            else:
                # Anonymous favorites only ever applied to nodes (path="/")
                # and were a debug-grade feature; keep the behaviour intact.
                node = self.nodes[hash_hex]
                node["favorited"] = value
        if user_sub:
            self._persist_favorites(fav_snapshot)
        else:
            self._mark_nodes_dirty()
        return True

    def get_favorites(self, user_sub: str = "") -> list:
        """Return the user's favorites as a list of {hash, path, name, added}.

        The hosted node's index is included implicitly so it always appears
        in the favorites UI without requiring a write.
        """
        with self._lock:
            entries = [dict(f) for f in self._favorites.get(user_sub, [])]

        hosted = self._hosted_hash.lower() if self._hosted_hash else ""
        if hosted:
            has_hosted_index = any(
                f["hash"] == hosted and (f.get("path") or "/") == "/"
                for f in entries
            )
            if not has_hosted_index:
                entries.insert(0, {
                    "hash":  hosted,
                    "path":  "/",
                    "name":  self._hosted_name or "This Node",
                    "added": 0,
                    "is_hosted": True,
                })
        for f in entries:
            if f["hash"] == hosted and (f.get("path") or "/") == "/":
                f["is_hosted"] = True
                if self._hosted_name:
                    f["name"] = self._hosted_name
        return entries

    def stop(self):
        log.info("NodeBrowser stopping")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _hop_count(self, hash_hex: str) -> Optional[int]:
        try:
            hops = self._rns.Transport.hops_to(bytes.fromhex(hash_hex))
            if hops is None or hops >= _HOPS_UNKNOWN:
                return None
            return int(hops)
        except Exception:
            return None

    def get_diagnostics(self, hash_hex: str) -> dict:
        """Network-routing snapshot for a single destination.

        Returns a dict with the fields the dashboard / node-info popup
        can render in one round trip:

          * ``hops``           — integer hop count, or None if no route
          * ``has_path``       — bool, True if RNS.Transport has a path entry
          * ``next_hop_iface`` — name of the interface the first packet
                                 would leave through, or None
          * ``is_local``       — True when the destination is hosted in
                                 this process (no transit needed)

        All errors swallow to ``None`` for the field they affect — this
        is best-effort introspection, never throws.
        """
        RNS = self._rns
        out = {
            "hops":           None,
            "has_path":       False,
            "next_hop_iface": None,
            "is_local":       False,
        }
        try:
            dest_hash = bytes.fromhex(hash_hex)
        except ValueError:
            return out

        try:
            for d in RNS.Transport.destinations:
                if getattr(d, "hash", None) == dest_hash:
                    out["is_local"] = True
                    out["hops"]     = 0
                    out["has_path"] = True
                    return out
        except Exception:
            pass

        try:
            out["has_path"] = bool(RNS.Transport.has_path(dest_hash))
        except Exception:
            pass

        try:
            hops = RNS.Transport.hops_to(dest_hash)
            if hops is not None and hops < _HOPS_UNKNOWN:
                out["hops"] = int(hops)
        except Exception:
            pass

        # next_hop_interface returns the RNS.Interface instance the
        # first packet would leave through. We only surface its .name
        # (display string) — the object itself isn't JSON-friendly.
        try:
            iface = RNS.Transport.next_hop_interface(dest_hash)
            if iface is not None:
                out["next_hop_iface"] = getattr(iface, "name", str(iface))
        except Exception:
            pass

        return out

    def _register_node(self, destination_hash: bytes, app_data: Optional[bytes]):
        hash_hex = destination_hash.hex()
        name = "Unnamed Node"
        if app_data:
            try:
                name = app_data.decode("utf-8").strip()
            except Exception:
                pass

        now = time.time()
        with self._lock:
            existing = self.nodes.get(hash_hex)
            if existing:
                existing["name"]           = name
                existing["last_seen"]      = now
                existing["announce_count"] = existing.get("announce_count", 0) + 1
            else:
                self.nodes[hash_hex] = {
                    "hash":           hash_hex,
                    "name":           name,
                    "first_seen":     now,
                    "last_seen":      now,
                    "announce_count": 1,
                    "view_count":     0,
                    "rx_bytes":       0,
                    "last_load_ms":   None,
                    "avg_load_ms":    None,
                    "last_ping_ms":   None,
                    "last_load_ok":   None,
                    "ever_load_ok":   False,
                    "favorited":      False,
                }

        log.info(
            "Node %s: %s (announces=%d)",
            hash_hex[:12], name,
            self.nodes[hash_hex].get("announce_count", 1),
        )
        self._mark_nodes_dirty()

    def _record_fetch(self, hash_hex: str, rx_bytes: int, load_ms: int,
                      ok: bool = True, update_status: bool = True):
        with self._lock:
            node = self.nodes.get(hash_hex)
            if node is None:
                node = {
                    "hash":           hash_hex,
                    "name":           hash_hex[:16] + "…",
                    "first_seen":     time.time(),
                    "last_seen":      time.time(),
                    "announce_count": 0,
                    "view_count":     0,
                    "rx_bytes":       0,
                    "last_load_ms":   None,
                    "avg_load_ms":    None,
                    "last_ping_ms":   None,
                    "last_load_ok":   None,
                    "ever_load_ok":   False,
                    "favorited":      False,
                }
                self.nodes[hash_hex] = node

            if update_status:
                node["last_load_ok"] = ok
                if ok:
                    node["ever_load_ok"] = True
            node["view_count"]   = node.get("view_count", 0) + 1
            if ok:
                node["rx_bytes"]     = node.get("rx_bytes", 0) + rx_bytes
                node["last_load_ms"] = load_ms
                prev = node.get("avg_load_ms")
                node["avg_load_ms"] = (
                    load_ms if prev is None
                    else int(prev * 0.7 + load_ms * 0.3)
                )

        self._mark_nodes_dirty()

    def _record_ping(self, hash_hex: str, ping_ms: int):
        with self._lock:
            node = self.nodes.get(hash_hex)
            if not node:
                return
            node["last_ping_ms"] = ping_ms
        self._mark_nodes_dirty()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_nodes(self):
        if not os.path.exists(self._nodes_file):
            return
        try:
            with open(self._nodes_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            with self._lock:
                self.nodes = {h: rec for h, rec in data.items()}
            log.info("Loaded %d node(s) from %s", len(self.nodes), self._nodes_file)
        except Exception as exc:
            log.warning("Could not load nodes file: %s", exc)

    def _load_favorites(self):
        if not os.path.exists(self._favorites_file):
            return
        try:
            with open(self._favorites_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            # Migrate legacy format: per-user list of hash strings → objects.
            migrated: dict = {}
            now = time.time()
            for sub, entries in (raw or {}).items():
                out = []
                for e in entries or []:
                    if isinstance(e, str):
                        node_name = self.nodes.get(e, {}).get("name", "")
                        out.append({
                            "hash":  e.lower(),
                            "path":  "/",
                            "name":  node_name or e[:16],
                            "added": now,
                        })
                    elif isinstance(e, dict) and e.get("hash"):
                        out.append({
                            "hash":  e["hash"].lower(),
                            "path":  e.get("path") or "/",
                            "name":  e.get("name") or e["hash"][:16],
                            "added": e.get("added", now),
                        })
                migrated[sub] = out
            self._favorites = migrated
            log.info("Loaded favorites for %d user(s)", len(self._favorites))
        except Exception as exc:
            log.warning("Could not load favorites file: %s", exc)

    def _mark_nodes_dirty(self) -> None:
        """Flag ``self.nodes`` as needing persistence. Callable from the
        RNS read_loop thread — sets a flag under a lock and returns.
        The actual disk write happens later, on the persister thread.
        """
        with self._nodes_dirty_lock:
            self._nodes_dirty = True

    def _nodes_persist_loop(self) -> None:
        """Background thread: every ``NODES_PERSIST_INTERVAL_S`` seconds,
        flush any dirty nodes state to disk. Uses ``Event.wait`` so a
        clean shutdown that sets ``_nodes_stop_event`` breaks out
        promptly rather than sleeping through.
        """
        while not self._nodes_stop_event.is_set():
            if self._nodes_stop_event.wait(timeout=NODES_PERSIST_INTERVAL_S):
                break
            self._flush_nodes_if_dirty()

    def _flush_nodes_if_dirty(self) -> None:
        """Snapshot + write if the dirty flag is set. Re-marks dirty on
        write failure so the next tick tries again.
        """
        with self._nodes_dirty_lock:
            if not self._nodes_dirty:
                return
            self._nodes_dirty = False
        with self._lock:
            snapshot = dict(self.nodes)
        try:
            self._persist(snapshot)
        except Exception as exc:
            log.warning("nodes.json persist failed, will retry: %s", exc)
            with self._nodes_dirty_lock:
                self._nodes_dirty = True

    def _persist(self, snapshot: dict):
        try:
            os.makedirs(os.path.dirname(self._nodes_file), exist_ok=True)
            tmp = f"{self._nodes_file}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp, self._nodes_file)
        except Exception as exc:
            log.warning("Could not save nodes file: %s", exc)

    def _load_iface_stats(self):
        if not os.path.exists(self._iface_stats_file):
            return
        try:
            with open(self._iface_stats_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._iface_base = {
                name: {"rxb": int(v.get("rxb", 0)), "txb": int(v.get("txb", 0))}
                for name, v in data.items()
                if isinstance(v, dict)
            }
            log.info("Loaded lifetime iface stats for %d interface(s)", len(self._iface_base))
        except Exception as exc:
            log.warning("Could not load iface stats: %s", exc)

    def _save_iface_stats(self, snapshot: dict):
        try:
            os.makedirs(os.path.dirname(self._iface_stats_file), exist_ok=True)
            tmp = self._iface_stats_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp, self._iface_stats_file)
        except Exception as exc:
            log.warning("Could not save iface stats: %s", exc)

    def _persist_favorites(self, snapshot: dict):
        try:
            os.makedirs(os.path.dirname(self._favorites_file), exist_ok=True)
            tmp = self._favorites_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp, self._favorites_file)
        except Exception as exc:
            log.warning("Could not save favorites file: %s", exc)

    # ------------------------------------------------------------------
    # Blocklist
    # ------------------------------------------------------------------

    def is_blocked(self, hash_hex: str) -> bool:
        return hash_hex.lower() in self._blocklist

    def block_node(self, hash_hex: str) -> None:
        hash_hex = hash_hex.lower()
        with self._lock:
            self._blocklist.add(hash_hex)
            snapshot = list(self._blocklist)
        self._persist_blocklist(snapshot)
        log.info("Blocked node %s", hash_hex[:16])

    def unblock_node(self, hash_hex: str) -> bool:
        hash_hex = hash_hex.lower()
        with self._lock:
            if hash_hex not in self._blocklist:
                return False
            self._blocklist.discard(hash_hex)
            snapshot = list(self._blocklist)
        self._persist_blocklist(snapshot)
        log.info("Unblocked node %s", hash_hex[:16])
        return True

    def get_blocklist(self) -> list:
        with self._lock:
            return sorted(self._blocklist)

    def _load_blocklist(self):
        if not os.path.exists(self._blocklist_file):
            return
        try:
            with open(self._blocklist_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._blocklist = set(h.lower() for h in data if isinstance(h, str))
            log.info("Loaded %d blocked nodes", len(self._blocklist))
        except Exception as exc:
            log.warning("Could not load blocklist: %s", exc)

    def _persist_blocklist(self, snapshot: list):
        try:
            tmp = self._blocklist_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp, self._blocklist_file)
        except Exception as exc:
            log.warning("Could not save blocklist: %s", exc)


class _CountAnnounceHandler:
    aspect_filter = None

    def __init__(self, browser: NodeBrowser):
        self._browser = browser

    def received_announce(self, destination_hash, announced_identity, app_data):
        with self._browser._lock:
            self._browser._total_announces += 1
        log.debug("Announce: %s (total %d)",
                  destination_hash.hex()[:16], self._browser._total_announces)


class _NodeAnnounceHandler:
    aspect_filter = APP_NAME + "." + NODE_ASPECT

    def __init__(self, browser: NodeBrowser):
        self._browser = browser

    def received_announce(self, destination_hash, announced_identity, app_data):
        self._browser._register_node(destination_hash, app_data)

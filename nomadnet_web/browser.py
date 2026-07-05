"""
Read-only NomadNet node browser.

Connects to the Reticulum network, discovers NomadNet nodes via announces,
and fetches pages from them.
"""

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

        log.info("Starting Reticulum (config: %s)", config_dir or "default")
        self.reticulum = RNS.Reticulum(config_dir)

        self._counter_handler = _CountAnnounceHandler(self)
        RNS.Transport.register_announce_handler(self._counter_handler)

        self._announce_handler = _NodeAnnounceHandler(self)
        RNS.Transport.register_announce_handler(self._announce_handler)

        log.info(
            "NodeBrowser ready — %d node(s) loaded, listening for announces",
            len(self.nodes),
        )

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
            with self._lock:
                snapshot = dict(self.nodes)
            self._persist(snapshot)

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

        Uses a stall-based watchdog: as long as the node keeps sending
        packets, the fetch keeps running and `progress_cb` (if provided)
        is called with a float in [0,1]. If no packet arrives for
        `timeout` seconds, the fetch is aborted — as "no response" if
        nothing ever arrived, otherwise "lost connection".
        """
        RNS = self._rns
        try:
            dest_hash = bytes.fromhex(destination_hash_hex)
        except ValueError:
            return None, "Invalid destination hash"

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
            deadline = time.time() + PATH_TIMEOUT
            while not RNS.Transport.has_path(dest_hash):
                if time.time() > deadline:
                    log.warning(
                        "fetch_page: path discovery timed out for %s",
                        destination_hash_hex[:16],
                    )
                    self._record_fetch(destination_hash_hex.lower(), 0, 0, ok=False,
                                       update_status=is_index)
                    return None, "Path not found — node may be unreachable"
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
        RETRY_SLEEP       = 1.5
        RETRYABLE_ERRORS  = ("Link closed before response", "Page request failed")

        def _do_attempt() -> dict:
            """Run one link+request+wait+teardown attempt. Returns the
            result dict — the outer retry loop decides whether to try again.
            State (result, done, callbacks) is fresh per call so a retry
            doesn't inherit stale closures from the previous attempt.
            """
            result: dict = {"content": None, "error": None}
            done = threading.Event()
            last_activity = [time.monotonic()]
            link_active = [False]
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
                link.request(
                    rns_path,
                    data=req_data,
                    response_callback=_on_response,
                    failed_callback=_on_failed,
                    progress_callback=_on_progress,
                    timeout=PAGE_HARD_CAP,
                )

            def _on_link_closed(link):
                if not done.is_set():
                    result["error"] = "Link closed before response"
                    done.set()

            link = RNS.Link(
                destination,
                established_callback=_on_link_established,
                closed_callback=_on_link_closed,
            )

            # Stall watchdog: only active AFTER the link has been established.
            # Before that, RNS's own per-hop establishment timeout will fire
            # _on_link_closed if the link can't form — applying a 15-second
            # local watchdog at this stage would falsely abort multi-hop or
            # LoRa paths whose establishment legitimately takes 20–40 s.
            hard_deadline = time.monotonic() + PAGE_HARD_CAP
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
                if link_active[0]:
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

            try:
                link.teardown()
            except Exception:
                pass

            return result

        final_result: dict = {"content": None, "error": None}
        for attempt in range(MAX_ATTEMPTS):
            final_result = _do_attempt()
            if final_result["content"] is not None:
                break
            err = final_result.get("error") or ""
            # Only retry on transient link failures. Path-discovery timeouts,
            # hard-cap breaches, and stall/finalise errors imply either an
            # unreachable destination or a stuck transfer — retrying would
            # just wait through it again.
            if err not in RETRYABLE_ERRORS or attempt == MAX_ATTEMPTS - 1:
                break
            log.info(
                "fetch_page: retrying %s (attempt %d/%d after: %s)",
                destination_hash_hex[:16], attempt + 2, MAX_ATTEMPTS, err,
            )
            # Refresh the path in case the advertised route has gone stale.
            # RNS.Transport.request_path is idempotent and cheap; a fresh
            # announce from the destination (or a shorter path via a
            # different hop) can arrive before the next attempt.
            try:
                RNS.Transport.request_path(dest_hash)
            except Exception:
                pass
            time.sleep(RETRY_SLEEP)

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

    def drop_job(self, job_id: str) -> None:
        """Evict a job entry — call after the client has retrieved the result."""
        with self._jobs_lock:
            self._jobs.pop(job_id, None)

    def cleanup_jobs(self, max_age: int = 300) -> int:
        """Evict completed jobs older than max_age seconds. Returns count removed."""
        cutoff = time.time() - max_age
        with self._jobs_lock:
            stale = [
                jid for jid, j in self._jobs.items()
                if j.get("completed") and j["completed"] < cutoff
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
            RNS.Transport.request_path(dest_hash)
            deadline = time.time() + PATH_TIMEOUT
            while not RNS.Transport.has_path(dest_hash):
                if time.time() > deadline:
                    return None, "No path to node"
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
                node_snapshot = dict(self.nodes)
        if user_sub:
            self._persist_favorites(fav_snapshot)
        else:
            self._persist(node_snapshot)
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
            snapshot = dict(self.nodes)

        log.info(
            "Node %s: %s (announces=%d)",
            hash_hex[:12], name,
            self.nodes[hash_hex].get("announce_count", 1),
        )
        self._persist(snapshot)

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
            snapshot = dict(self.nodes)

        self._persist(snapshot)

    def _record_ping(self, hash_hex: str, ping_ms: int):
        with self._lock:
            node = self.nodes.get(hash_hex)
            if node:
                node["last_ping_ms"] = ping_ms
                snapshot = dict(self.nodes)
            else:
                return
        self._persist(snapshot)

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

"""
Flask routes for the NomadNet web viewer.

Public endpoints (no login):
    /api/status, /api/nodes, /api/page, /api/auth/status

Login-required endpoints:
    /api/messages  (send + list sent)
"""

import re
import time
import logging
import urllib.parse
from flask import Blueprint, jsonify, request, abort, current_app
from flask_login import login_required, current_user
from micron2html import MicronConverter, default_url_resolver
from . import rate_limit

log = logging.getLogger(__name__)
bp = Blueprint("nomadnet", __name__)

# Caps on user-supplied form/var data submitted with a page fetch. Larger
# payloads are rejected with 413 — protects upstream NomadNet nodes (and
# our local site_server) from DoS via huge POST bodies. The limits are
# generous for normal forms (chat messages, registration, etc.) but firm.
_MAX_FIELD_DATA_BYTES = 64 * 1024
_MAX_FIELD_VALUE_BYTES = 16 * 1024
_MAX_FIELD_COUNT = 64


def _validate_field_data(field_data):
    """Return (sanitised_dict_or_None, error_str_or_None).

    Accepts the user-submitted fields dict and enforces size/count caps.
    Returns the dict unchanged on success, or an error string suitable
    for a 413 response on failure.
    """
    if not field_data:
        return field_data, None
    if not isinstance(field_data, dict):
        return None, "fields must be an object"
    if len(field_data) > _MAX_FIELD_COUNT:
        return None, f"too many fields (max {_MAX_FIELD_COUNT})"
    total = 0
    for k, v in field_data.items():
        if not isinstance(k, str):
            return None, "field keys must be strings"
        if v is None:
            v = ""
        if not isinstance(v, (str, int, float, bool)):
            return None, f"field '{k}' has unsupported value type"
        s = str(v)
        if len(s) > _MAX_FIELD_VALUE_BYTES:
            return None, f"field '{k}' exceeds {_MAX_FIELD_VALUE_BYTES} bytes"
        total += len(k) + len(s)
        if total > _MAX_FIELD_DATA_BYTES:
            return None, f"total field data exceeds {_MAX_FIELD_DATA_BYTES} bytes"
    return field_data, None


def _web_url_resolver(url: str, node_hash: str, base_path: str) -> str:
    """Wrap canonical NomadNet URLs in this app's /page?url= route.

    File-link special case: Micron2HTML's default resolver returns ``"#"``
    for any URL containing ``/file/`` so external binaries don't render
    as live links. NomadPortal handles file downloads through a separate
    confirm-then-fetch flow (see /api/file/fetch), so we cheat by
    re-running canonicalisation with ``/file/`` rewritten to ``/page/``
    to bypass the block, then swapping back. Avoids re-implementing the
    URL canonicalisation logic locally.
    """
    canonical = default_url_resolver(url, node_hash, base_path)
    if canonical == "#" and "/file/" in url:
        unblocked = default_url_resolver(
            url.replace("/file/", "/page/", 1),
            node_hash, base_path,
        )
        if unblocked.startswith("hash://"):
            file_canonical = unblocked.replace("/page/", "/file/", 1)
            return f"/file?url={urllib.parse.quote(file_canonical, safe='')}"
    if canonical.startswith("hash://"):
        return f"/page?url={urllib.parse.quote(canonical, safe='')}"
    return canonical


_converter = MicronConverter(url_resolver=_web_url_resolver)


def _render_title_html(text: str) -> str:
    """Render Micron markup to inline HTML for the brand element."""
    if not text:
        return ""
    return _converter.convert_inline(text.strip())


_HTML_TAG_RE = re.compile(r'<[^>]+>')

def _render_title_plain(text: str) -> str:
    """Strip Micron markup, returning plain text for use as browser tab title."""
    html = _render_title_html(text)
    return _HTML_TAG_RE.sub('', html).strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _browser():
    return current_app.config["BROWSER"]

def _cache():
    return current_app.config["CACHE"]

def _id_store():
    return current_app.config["IDENTITY_STORE"]

def _contact_store():
    mgr = current_app.config.get("CONTACT_STORE")
    return mgr.for_user(current_user.id) if mgr else None

def _identity_for_fetch(node_hash: str):
    """RNS.Identity to identify with for the current user/node, or None.

    Only set when the user has explicitly toggled persistent identification
    for this node via the address-bar fingerprint button.
    """
    if not current_user.is_authenticated:
        return None
    entry = _id_store().get_for_user(current_user.id)
    if not entry:
        return None
    if not _id_store().is_identified_to(entry["id"], node_hash):
        return None
    return _id_store().load_rns_identity(entry["id"])


def _local_identity_hex(node_hash: str = "") -> str:
    """The logged-in user's RNS identity hex (for local-site executable
    pages). Empty when:

      - the user isn't logged in, or
      - `node_hash` is given and the user hasn't toggled persistent
        fingerprint identification on for that node.

    Mirrors `_identity_for_fetch()` for RNS connections so the Direct
    case honours the same address-bar fingerprint button — turning it
    OFF means executable pages see the user as anonymous, regardless
    of whether the request came over an RNS link or via the local
    short-circuit.
    """
    if not current_user.is_authenticated:
        return ""
    entry = _id_store().get_for_user(current_user.id)
    if not entry:
        return ""
    if node_hash and not _id_store().is_identified_to(entry["id"], node_hash):
        return ""
    return entry["id"]


def _allowed_local_hashes() -> tuple[set[str], dict]:
    """Return the set of locally-served / default node hashes and current UI settings."""
    site_server  = current_app.config.get("SITE_SERVER")
    ui           = current_app.config.get("UI_SETTINGS")
    settings     = ui.get_all() if ui else {}
    hosted_hash  = site_server.node_hash().lower() if (site_server and site_server.node_hash()) else ""
    default_hash = (settings.get("default_node", "") or "").lower()
    return {h for h in (hosted_hash, default_hash) if h}, settings


def _can_browse(node_hash: str) -> bool:
    """Whether the current user is allowed to fetch pages from this node.

    Super admins:  always.
    Admins:        blocked if admins_default_lock.
    Users:         blocked if users_default_lock.
    Guests:        blocked if guests_default_lock.
    """
    nh = (node_hash or "").lower()
    if not nh:
        return False

    if getattr(current_user, "super_admin", False):
        return True

    allowed, settings = _allowed_local_hashes()

    if getattr(current_user, "is_admin", False):
        if settings.get("admins_default_lock"):
            return nh in allowed
        return True

    if current_user.is_authenticated:
        if settings.get("users_default_lock"):
            return nh in allowed
        return True

    if settings.get("guests_default_lock"):
        return nh in allowed
    return True


def _can_interact(node_hash: str) -> bool:
    """Whether the current user is allowed to submit forms on this node.

    Super admins:  always.
    Admins:        any node unless admins_default_lock.
    Users:         any node unless users_default_lock.
    Guests:        always restricted to hosted/default.
    """
    nh = (node_hash or "").lower()
    if not nh:
        return False

    if getattr(current_user, "super_admin", False):
        return True

    allowed, settings = _allowed_local_hashes()

    if getattr(current_user, "is_admin", False):
        if settings.get("admins_default_lock"):
            return nh in allowed
        return True

    if current_user.is_authenticated:
        if settings.get("users_default_lock"):
            return nh in allowed
        return True

    return nh in allowed


def _parse_nomadnet_url(url: str) -> tuple[str, str]:
    """Return (node_hash_hex, path) from a nomadnet URL.

    Accepts:
        hash://<hex>/path/to/page.mu
        hash:/path/to/page.mu          (legacy; hash embedded in path prefix)
        nomadnetwork://<hex>/path
    """
    url = url.strip()
    if url.startswith("nomadnetwork://"):
        url = url[len("nomadnetwork://"):]
        parts = url.split("/", 1)
        node_hash = parts[0]
        path = "/" + parts[1] if len(parts) > 1 else "/"
        return node_hash, path
    if url.startswith("hash://"):
        url = url[len("hash://"):]
        parts = url.split("/", 1)
        node_hash = parts[0]
        path = "/" + parts[1] if len(parts) > 1 else "/"
        return node_hash, path
    if url.startswith("hash:/"):
        # hash:/<hash>/path  (NomadNet terminal format)
        rest = url[len("hash:/"):]
        parts = rest.split("/", 1)
        node_hash = parts[0]
        path = "/" + parts[1] if len(parts) > 1 else "/"
        return node_hash, path
    return "", url


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@bp.get("/api/status")
def api_status():
    browser = _browser()
    cache = _cache()
    return jsonify({
        "status": "ok",
        "nodes_discovered": len(browser.get_nodes()),
        "cache": cache.stats(),
        "uptime": time.time() - current_app.config["START_TIME"],
        "read_only": True,
    })


@bp.get("/healthz")
def healthz():
    """Operational healthcheck — verifies RNS routing is functional, not
    just that gunicorn is listening.

    A container with broken RNS would otherwise report `healthy` (because
    `/api/status` returns 200) while being unable to actually serve any
    NomadNet content. Returns 200 only when at least one configured
    interface is online; 503 otherwise so Docker's healthcheck reflects
    real availability.
    """
    browser = current_app.config.get("BROWSER")
    if browser is None:
        return jsonify({"status": "starting",
                        "reason": "browser not initialised"}), 503

    # RNS.Reticulum() blocks 60-300 s at container start while it replays
    # destination_table and brings up TCP interfaces. Report "starting"
    # until the background init has finished so Docker's healthcheck
    # gives the container a grace period rather than flapping it as
    # unhealthy repeatedly during a legitimate boot.
    if hasattr(browser, "is_ready") and not browser.is_ready():
        # rns_init_progress() adds elapsed_seconds, an ETA derived from
        # persisted prior-run durations, and how many past runs the
        # estimate is based on (0 = first boot on this volume, no ETA).
        progress = (browser.rns_init_progress()
                    if hasattr(browser, "rns_init_progress") else {})
        return jsonify({"status": "starting",
                        "reason": "RNS transport is still coming up",
                        **progress}), 503

    try:
        status = browser.get_status()
    except Exception:
        # Log the exception server-side; return only the high-level
        # status string to the client so an unauthenticated /healthz
        # probe can't lift exception text out of the JSON body.
        log.exception("healthz: browser.get_status() raised")
        return jsonify({"status": "error",
                        "reason": "interface status unavailable"}), 503

    interfaces = status.get("interfaces") or []
    if not interfaces:
        return jsonify({"status": "degraded",
                        "reason": "no interfaces configured",
                        "interfaces": []}), 503

    online = [i for i in interfaces if i.get("online")]
    if not online:
        return jsonify({"status": "degraded",
                        "reason": "no interfaces online",
                        "interfaces": interfaces}), 503

    # Hosting-side staleness check. Catches the specific failure mode
    # where RNS + interfaces are green but the site server's background
    # announce loop has silently died — the operator has a "healthy"
    # container that isn't announcing. Grace of 2× the configured
    # announce_interval before we flip the status; some slop is normal
    # (RNS jitter, network hiccups).
    site_server = current_app.config.get("SITE_SERVER")
    announce_stale = None
    if (site_server is not None
            and getattr(site_server, "auto_announce_enabled", lambda: False)()
            and hasattr(site_server, "last_announce_at")
            and hasattr(site_server, "announce_interval")):
        try:
            last     = site_server.last_announce_at()
            interval = site_server.announce_interval()
            if last > 0:
                idle = time.time() - last
                if idle > 2 * interval:
                    announce_stale = {
                        "last_announce_seconds_ago": int(idle),
                        "announce_interval":         int(interval),
                    }
        except Exception:
            log.exception("healthz: site announce staleness check raised")

    if announce_stale is not None:
        return jsonify({"status":     "degraded",
                        "reason":     "site announce loop appears stalled",
                        "interfaces": interfaces,
                        **announce_stale}), 503

    return jsonify({
        "status": "ok",
        "interfaces_online": len(online),
        "interfaces_total":  len(interfaces),
        "nodes_discovered":  status.get("nodes_discovered", 0),
        "uptime":            time.time() - current_app.config["START_TIME"],
    })


# ---------------------------------------------------------------------------
# Debug — read-only application-state introspection
# ---------------------------------------------------------------------------

@bp.get("/api/_debug/state")
def api_debug_state():
    """Snapshot of application-level dicts / RNS Transport dicts that
    could grow unbounded over a long-running session. Read-only —
    counts and small samples, not full contents.

    Unauthenticated on purpose — same rationale as /healthz. The only
    output is dict counts (integers), interface names (already visible
    via the sidebar), and truncated 4-char hash prefixes (not enough to
    identify a destination). No packet contents, no identity material,
    no session data. Convenience beats theoretical exposure for a
    diagnostic endpoint operators need to poll from ad-hoc terminals.
    """
    browser = current_app.config.get("BROWSER")
    out: dict = {
        "uptime_seconds": int(time.time() - current_app.config["START_TIME"]),
        "rns_ready":      bool(getattr(browser, "is_ready", lambda: False)()),
    }

    # NodeBrowser — fetch jobs + link cache.
    if browser is not None:
        try:
            with browser._jobs_lock:
                job_ages = sorted(
                    (time.time() - j.get("started", time.time()))
                    for j in browser._jobs.values()
                )
            out["fetch_jobs"] = {
                "count":                    len(job_ages),
                "oldest_started_seconds_ago": int(job_ages[-1]) if job_ages else 0,
            }
        except Exception as exc:
            out["fetch_jobs"] = {"error": str(exc)}

        try:
            with browser._link_cache_lock:
                keys = list(browser._link_cache.keys())
            out["link_cache"] = {
                "size":       len(keys),
                "keys_last4": [k.hex()[-4:] for k in keys[:20]],
            }
        except Exception as exc:
            out["link_cache"] = {"error": str(exc)}

    # RNS Transport internal dicts — the ones I want to watch for
    # unbounded growth. Accessed defensively; RNS is not initialised
    # to guarantee these attributes exist across minor versions.
    try:
        import RNS
        transport_snapshot: dict = {}

        for attr in ("path_table", "announce_table", "discovery_pr_tags",
                     "announce_rate_table", "held_announces", "tunnels"):
            try:
                obj = getattr(RNS.Transport, attr, None)
                if obj is None:
                    continue
                if isinstance(obj, dict):
                    transport_snapshot[attr] = {"size": len(obj)}
                elif isinstance(obj, (list, tuple, set)):
                    transport_snapshot[attr] = {"size": len(obj)}
                else:
                    transport_snapshot[attr] = {"type": type(obj).__name__}
            except Exception as exc:
                transport_snapshot[attr] = {"error": str(exc)}

        # Per-interface counters (some are set by RNS itself, some are
        # our announce-frequency deques).
        try:
            ifaces_info = []
            for iface in getattr(RNS.Transport, "interfaces", []) or []:
                info = {
                    "name":            getattr(iface, "name", str(iface)),
                    "online":          bool(getattr(iface, "online", False)),
                    # OUT is the flag RNS checks in Transport.outbound() —
                    # if False, no packets get transmitted on this iface.
                    # Set False by teardown() and by "outgoing = No" in the
                    # config. Observing OUT=False on a client interface
                    # that was never explicitly torn down means something
                    # in RNS decided the interface was unusable.
                    "OUT":             bool(getattr(iface, "OUT", False)),
                    "IN":              bool(getattr(iface, "IN", False)),
                    # Client-only fields; None on server interfaces.
                    "reconnecting":    bool(getattr(iface, "reconnecting", False)),
                    "detached":        bool(getattr(iface, "detached", False)),
                    "never_connected": bool(getattr(iface, "never_connected", False)),
                    "ia_freq_samples": len(getattr(iface, "ia_freq_deque", []) or []),
                    "oa_freq_samples": len(getattr(iface, "oa_freq_deque", []) or []),
                }
                for stat_field in ("rxb", "txb"):
                    val = getattr(iface, stat_field, None)
                    if isinstance(val, (int, float)):
                        info[stat_field] = val
                held = getattr(iface, "held_announces", None)
                if isinstance(held, dict):
                    info["held_announces"] = len(held)
                ifaces_info.append(info)
            transport_snapshot["interfaces"] = ifaces_info
        except Exception as exc:
            transport_snapshot["interfaces_error"] = str(exc)

        out["transport"] = transport_snapshot
    except Exception as exc:
        out["transport_error"] = str(exc)

    # LXMF tracker — bounded? Let's find out.
    tracker = current_app.config.get("LXMF_TRACKER")
    if tracker is not None:
        try:
            peers = getattr(tracker, "peers", None)
            if isinstance(peers, dict):
                out["lxmf_tracker"] = {"peer_count": len(peers)}
        except Exception as exc:
            out["lxmf_tracker"] = {"error": str(exc)}

    # LXMF propagation-node sync service — reports currently-picked
    # propagation node, pool size, per-user sync status. See
    # nomadnet_web/lxmf_sync.py for what these fields mean.
    prop_sync = current_app.config.get("PROP_SYNC")
    if prop_sync is not None:
        try:
            out["lxmf_propagation"] = prop_sync.snapshot()
        except Exception as exc:
            out["lxmf_propagation"] = {"error": str(exc)}

    return jsonify(out)


# ---------------------------------------------------------------------------
# Node discovery
# ---------------------------------------------------------------------------

@bp.get("/api/nodes")
def api_nodes():
    user_sub = current_user.id if current_user.is_authenticated else ""
    ui = current_app.config.get("UI_SETTINGS")
    default_hash = ((ui.get_all().get("default_node") if ui else "") or "").lower()
    nodes = _browser().get_nodes(user_sub=user_sub, default_hash=default_hash)
    if not current_user.is_authenticated:
        # Guests see `favorited` only for hosted/default nodes (the operator's
        # auto-pinned set); strip it from every other node so guests don't
        # see other users' bookmarks via the sidebar.
        nodes = [
            {k: v for k, v in n.items()
             if k not in ("favorited",) or n.get("is_hosted") or n.get("is_default")}
            for n in nodes
        ]
    return jsonify({"nodes": nodes})


@bp.get("/api/nodes/<hash_hex>")
def api_node(hash_hex: str):
    node = _browser().get_node(hash_hex)
    if node is None:
        abort(404, description="Node not found")
    return jsonify(node)


@bp.get("/api/nodes/<hash_hex>/diagnostics")
def api_node_diagnostics(hash_hex: str):
    """Snapshot RNS routing state for a node — hops, has-path, next-hop
    interface, is-local. Cheap, no network round-trip. Public read so
    the node-info popup works for guests too; the live-ping endpoint
    (which DOES emit packets) stays behind login."""
    diag = _browser().get_diagnostics(hash_hex)
    return jsonify(diag)


@bp.post("/api/nodes/<hash_hex>/ping")
@login_required
def api_node_ping(hash_hex: str):
    """Live link-establishment latency measurement. Requires login —
    it sends actual packets on the mesh, so unauthenticated callers
    could be used to amplify probing of arbitrary destinations."""
    ip = request.remote_addr or "unknown"
    if not rate_limit.check(f"ping:{ip}", 30, 60):
        return jsonify({"error": "Rate limit exceeded — slow down"}), 429
    ms, error = _browser().ping_node(hash_hex)
    if error:
        return jsonify({"error": error}), 503
    return jsonify({"ms": ms})


# ---------------------------------------------------------------------------
# Page fetching & rendering
# ---------------------------------------------------------------------------

@bp.get("/api/page")
def api_page_get():
    ip = request.remote_addr or "unknown"
    if not rate_limit.check(f"page:{ip}", 60, 60):
        return jsonify({"error": "Rate limit exceeded — slow down"}), 429
    """Fetch and render a NomadNet page.

    Query params:
        url   — full nomadnet URL  e.g. hash://<node_hash>/index.mu
        hash  — node destination hash (alternative to full URL)
        path  — page path within the node (used with hash param)
        raw   — if "1", return raw Micron instead of HTML
    """
    url = request.args.get("url", "")
    node_hash = request.args.get("hash", "")
    path = request.args.get("path", "/")
    raw = request.args.get("raw", "0") == "1"

    if url:
        node_hash, path = _parse_nomadnet_url(url)

    if not node_hash:
        abort(400, description="node hash required")

    if not _can_browse(node_hash):
        if not current_user.is_authenticated:
            return jsonify({"error": "Login required to browse external nodes"}), 401
        return jsonify({"error": "Browsing external nodes is restricted by the administrator"}), 403

    if _browser().is_blocked(node_hash):
        return jsonify({"error": "This node has been blocked by the administrator."}), 403

    _norm = (path or "/").rstrip("/") or "/"
    is_index = _norm in ("/", "/index.mu", "/page/index.mu")

    site_server = current_app.config.get("SITE_SERVER")
    is_local = site_server and site_server.node_hash() == node_hash.lower()

    cache_key = f"{node_hash}:{path}"
    cached = None if is_local else _cache().get(cache_key)
    if cached:
        micron_text = cached.decode("utf-8", errors="replace")
    else:
        if is_local:
            t0 = time.monotonic()
            content, error = site_server.fetch_page(
                path, local_identity_hex=_local_identity_hex(node_hash),
            )
            load_ms = int((time.monotonic() - t0) * 1000)
            _browser()._record_fetch(
                node_hash.lower(),
                rx_bytes=len(content) if content is not None else 0,
                load_ms=load_ms,
                ok=(content is not None),
                update_status=is_index,
            )
        else:
            content, error = _browser().fetch_page(
                node_hash, path,
                identify_with=_identity_for_fetch(node_hash),
            )
        if error:
            return jsonify({"error": error}), 503
        if not is_local:
            _cache().set(cache_key, content)
        micron_text = content.decode("utf-8", errors="replace")

    if raw:
        return jsonify({"hash": node_hash, "path": path, "micron": micron_text})

    html_body = _converter.convert(
        micron_text, node_hash=node_hash, base_path=path,
        authenticated=_can_interact(node_hash),
    )
    return jsonify({"hash": node_hash, "path": path, "html": html_body})


@bp.post("/api/page")
def api_page_post():
    """Submit form data and fetch a NomadNet page.

    Guests may submit forms only on the hosted/default node — interaction
    with external nodes always requires login.
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    node_hash = data.get("hash", "")
    path = data.get("path", "/")
    field_data = data.get("fields", {})

    field_data, fd_err = _validate_field_data(field_data)
    if fd_err:
        return jsonify({"error": fd_err}), 413

    if url:
        node_hash, path = _parse_nomadnet_url(url)

    if not node_hash:
        abort(400, description="node hash required")

    if not _can_interact(node_hash):
        if not current_user.is_authenticated:
            return jsonify({"error": "Login required to submit forms on external nodes"}), 401
        return jsonify({"error": "Form submissions to external nodes are restricted by the administrator"}), 403

    if _browser().is_blocked(node_hash):
        return jsonify({"error": "This node has been blocked by the administrator."}), 403

    _norm = (path or "/").rstrip("/") or "/"
    is_index = _norm in ("/", "/index.mu", "/page/index.mu")

    site_server = current_app.config.get("SITE_SERVER")
    if site_server and site_server.node_hash() == node_hash.lower():
        t0 = time.monotonic()
        content, error = site_server.fetch_page(
            path,
            local_identity_hex=_local_identity_hex(node_hash),
            field_data=field_data,
        )
        load_ms = int((time.monotonic() - t0) * 1000)
        _browser()._record_fetch(
            node_hash.lower(),
            rx_bytes=len(content) if content is not None else 0,
            load_ms=load_ms,
            ok=(content is not None),
            update_status=is_index,
        )
        # Form submission to a local page invalidates cached renders so a
        # subsequent plain GET sees the post-save state.
        if field_data:
            local_id = _local_identity_hex(node_hash)
            for ident_suffix in (f"id={local_id}", "anon"):
                _cache().invalidate(f"{node_hash}:{path}:{ident_suffix}")
    else:
        content, error = _browser().fetch_page(
            node_hash, path, field_data=field_data,
            identify_with=_identity_for_fetch(node_hash),
        )
    if error:
        return jsonify({"error": error}), 503

    micron_text = content.decode("utf-8", errors="replace")
    html_body = _converter.convert(
        micron_text, node_hash=node_hash, base_path=path,
        authenticated=True,
    )
    return jsonify({"hash": node_hash, "path": path, "html": html_body})


# ---------------------------------------------------------------------------
# Async page fetch — returns a job_id immediately; client polls progress.
# Drives the "Loading X%…" UI in the browser.
# ---------------------------------------------------------------------------

@bp.post("/api/page/fetch")
def api_page_fetch_start():
    """Kick off a background page fetch and return a job_id.

    The browser then polls /api/page/poll?id=<job_id> for progress and the
    final rendered HTML.

    Body (JSON):
        url    — full nomadnet URL  (e.g. hash://<node_hash>/index.mu)
        hash   — node destination hash (alternative to full URL)
        path   — page path within the node (used with hash param)
        fields — optional dict of form field values for `<...>` inputs
        raw    — if true, the poll endpoint returns Micron source instead of HTML
    """
    ip = request.remote_addr or "unknown"
    if not rate_limit.check(f"page:{ip}", 60, 60):
        return jsonify({"error": "Rate limit exceeded — slow down"}), 429

    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    node_hash = data.get("hash", "")
    path = data.get("path", "/")
    field_data = data.get("fields") or None
    raw = bool(data.get("raw", False))

    field_data, fd_err = _validate_field_data(field_data)
    if fd_err:
        return jsonify({"error": fd_err}), 413

    if url:
        node_hash, path = _parse_nomadnet_url(url)
    if not node_hash:
        abort(400, description="node hash required")

    # Forms-on-external-nodes need login; plain page browse uses _can_browse
    if field_data and not _can_interact(node_hash):
        if not current_user.is_authenticated:
            return jsonify({"error": "Login required to submit forms on external nodes"}), 401
        return jsonify({"error": "Form submissions to external nodes are restricted by the administrator"}), 403
    if not field_data and not _can_browse(node_hash):
        if not current_user.is_authenticated:
            return jsonify({"error": "Login required to browse external nodes"}), 401
        return jsonify({"error": "Browsing external nodes is restricted by the administrator"}), 403

    if _browser().is_blocked(node_hash):
        return jsonify({"error": "This node has been blocked by the administrator."}), 403

    site_server = current_app.config.get("SITE_SERVER")
    is_local = site_server and site_server.node_hash() == node_hash.lower()

    # Local site — short-circuit through site_server.fetch_page, no network
    # round-trip. Form submissions (with field_data) also use this path so
    # local executable pages can react to the submitted fields directly.
    if is_local:
        # Executable pages can render per-user output (var_remote_identity etc).
        # Key the cache by the requesting user's identity hex so two users
        # never see each other's rendered context block. Identity is only
        # exposed when the user has toggled fingerprint on for this node —
        # otherwise the cache key uses "anon" and the page sees no identity.
        local_id = _local_identity_hex(node_hash)
        cache_key = f"{node_hash}:{path}:" + (f"id={local_id}" if local_id else "anon")
        # Form submissions skip the cache (every submit must re-run the
        # script) AND invalidate any cached output for this path so a
        # subsequent plain GET returns fresh content reflecting the save.
        if field_data:
            for ident_suffix in (f"id={local_id}", "anon"):
                _cache().invalidate(f"{node_hash}:{path}:{ident_suffix}")
            content_b = None
        else:
            content_b = _cache().get(cache_key)
        if content_b is None:
            t0 = time.monotonic()
            content_b, error = site_server.fetch_page(
                path,
                local_identity_hex=local_id,
                field_data=field_data,
            )
            load_ms = int((time.monotonic() - t0) * 1000)
            _norm = (path or "/").rstrip("/") or "/"
            is_index = _norm in ("/", "/index.mu", "/page/index.mu")
            _browser()._record_fetch(
                node_hash.lower(),
                rx_bytes=len(content_b) if content_b is not None else 0,
                load_ms=load_ms,
                ok=(content_b is not None),
                update_status=is_index,
            )
            if error:
                return jsonify({"error": error}), 503
            # Don't cache form-submission output — the next plain GET should
            # hit the script again with no field_data and render the canonical
            # post-save view.
            if not field_data:
                _cache().set(cache_key, content_b)
        # synthesize a "done" job entry so the polling flow is uniform
        import secrets
        job_id = secrets.token_hex(8)
        with _browser()._jobs_lock:
            _browser()._jobs[job_id] = {
                "status":    "done",
                "progress":  1.0,
                "node_hash": node_hash.lower(),
                "path":      path,
                "started":   time.time(),
                "completed": time.time(),
                "content":   content_b,
                "error":     None,
            }
        return jsonify({"job_id": job_id, "raw": raw})

    # Remote — normal async fetch
    job_id = _browser().fetch_page_async(
        node_hash, path, field_data=field_data,
        identify_with=_identity_for_fetch(node_hash),
    )
    return jsonify({"job_id": job_id, "raw": raw})


@bp.get("/api/page/poll")
def api_page_poll():
    """Poll a fetch job. Returns status/progress, or both html + micron
    when done so the client can toggle raw view without re-fetching."""
    job_id = request.args.get("id", "")
    if not job_id:
        return jsonify({"error": "id required"}), 400

    job = _browser().get_job(job_id)
    if not job:
        return jsonify({"error": "unknown or expired job"}), 404

    base = {
        "status":    job["status"],
        "progress":  round(job.get("progress", 0.0), 3),
        "hash":      job.get("node_hash", ""),
        "path":      job.get("path", ""),
    }

    if job["status"] == "fetching":
        return jsonify(base)

    if job["status"] == "error":
        _browser().drop_job(job_id)
        return jsonify({**base, "error": job.get("error", "Unknown error")}), 503

    # status == "done" — convert and return both html + micron, then drop.
    content = job.get("content") or b""
    node_hash = job["node_hash"]
    path = job["path"]

    # Cache remote content; locals already came from cache in the start path.
    site_server = current_app.config.get("SITE_SERVER")
    is_local = site_server and site_server.node_hash() == node_hash
    if not is_local:
        _cache().set(f"{node_hash}:{path}", content)

    micron_text = content.decode("utf-8", errors="replace")
    _browser().drop_job(job_id)

    html_body = _converter.convert(
        micron_text, node_hash=node_hash, base_path=path,
        authenticated=_can_interact(node_hash),
    )
    return jsonify({**base, "html": html_body, "micron": micron_text})


# ---------------------------------------------------------------------------
# File downloads
# ---------------------------------------------------------------------------
# File transfers reuse the page-fetch job infrastructure in browser.py — the
# only difference at the transport layer is the /file/ vs /page/ path prefix
# on the NomadNet side. JS calls /api/file/fetch to start a job, polls
# /api/file/poll for progress, then triggers the browser save dialog by
# navigating to /api/file/download?id=<job_id>. The result is served with
# Content-Disposition: attachment so users see the native download UI.
#
# Confirm-dialog rationale: file fetches over Reticulum can be slow (LoRa
# multi-hop networks measure throughput in kbps) and may pull arbitrary
# binaries from untrusted nodes. The frontend shows a confirm dialog with
# the filename + MIME type before initiating the fetch; size is reported
# during the fetch as bytes accumulate.

import mimetypes


@bp.post("/api/file/fetch")
def api_file_fetch_start():
    """Kick off a background file fetch and return a job_id.

    Body (JSON):
        url    — full nomadnet URL with /file/ prefix
                 (e.g. hash://<node_hash>/file/foo.zip)
        hash   — node destination hash (alternative to full URL)
        path   — file path within the node, starting with /file/

    Returns: {"job_id": str, "filename": str, "mime_type": str}.
    `filename` and `mime_type` are derived from the URL path; the actual
    transferred bytes are not inspected until the fetch completes.
    """
    ip = request.remote_addr or "unknown"
    if not rate_limit.check(f"file:{ip}", 20, 60):
        return jsonify({"error": "Rate limit exceeded — slow down"}), 429

    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    node_hash = data.get("hash", "")
    path = data.get("path", "")

    if url:
        node_hash, path = _parse_nomadnet_url(url)
    if not node_hash:
        abort(400, description="node hash required")
    if "/file/" not in path:
        abort(400, description="path must contain /file/")

    if not _can_browse(node_hash):
        if not current_user.is_authenticated:
            return jsonify({"error": "Login required to fetch files from external nodes"}), 401
        return jsonify({"error": "Fetching from external nodes is restricted by the administrator"}), 403

    if _browser().is_blocked(node_hash):
        return jsonify({"error": "This node has been blocked by the administrator."}), 403

    # Derive filename and MIME type from the URL for the confirm dialog.
    # NomadNet doesn't transmit Content-Type — extension is the only signal
    # we have without inspecting the bytes after fetch.
    filename = path.rsplit("/", 1)[-1] or "download"
    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type:
        mime_type = "application/octet-stream"

    # Local-site short-circuit. RNS loopback links to our own destination
    # don't reliably complete (the link.request never sees a response
    # callback when both sides are in this process), so fetch the bytes
    # straight off disk through site_server.files_dir() instead. Mirrors
    # the local short-circuit in api_page_fetch_start. The scan still
    # runs — local files are scanned identically to remote ones so the
    # admin can test the scan UX without needing a remote node.
    site_server = current_app.config.get("SITE_SERVER")
    is_local = site_server and site_server.node_hash() == node_hash.lower()
    if is_local:
        import os, secrets
        from werkzeug.utils import safe_join
        # Use Werkzeug's ``safe_join`` for path traversal containment.
        # It rejects any input that escapes ``files_root`` (absolute
        # paths, ``..`` segments, NUL bytes, etc.) by returning None,
        # and CodeQL recognises it as a path-traversal sanitiser so the
        # py/path-injection rule no longer flags downstream uses of
        # the result. Belt-and-braces: also pre-reject obvious traversal
        # patterns so the audit log captures a useful 400 reason.
        files_root = site_server.files_dir()
        rel        = path[len("/file/"):].lstrip("/")
        if not rel or ".." in rel.split("/") or rel.startswith("/"):
            abort(400, description="invalid file path")
        candidate = safe_join(files_root, rel)
        if candidate is None:
            abort(400, description="invalid file path")
        if not os.path.isfile(candidate):
            return jsonify({"error": "file not found on local site"}), 404
        try:
            with open(candidate, "rb") as fh:
                content = fh.read()
        except OSError as exc:
            log.warning("Local file read failed for %s: %s", candidate, exc)
            return jsonify({"error": "could not read file"}), 500

        scan_dict   = None
        local_error = None
        scanner     = getattr(_browser(), "scanner", None)
        scan_required = getattr(_browser(), "scan_required", False)
        if scanner is not None:
            try:
                scan = scanner.scan(content, filename)
            except Exception as exc:
                log.exception("Local-site scanner raised")
                from .scanner import ScanResult
                scan = ScanResult(
                    verdict="unavailable",
                    engine=getattr(scanner, "engine_name", "?"),
                    detail=f"scanner exception: {exc}",
                )
            scan_dict = scan.to_dict()
            if scan.blocked:
                local_error = (
                    f"Virus scan blocked download: "
                    f"{scan.signature or 'malicious content detected'}"
                )
                content = None
            elif scan.verdict == "unavailable" and scan_required:
                local_error = (
                    f"Virus scanner unavailable and VIRUS_SCAN=required "
                    f"is set: {scan.detail or 'no detail'}"
                )
                content = None

        job_id = secrets.token_hex(8)
        with _browser()._jobs_lock:
            _browser()._jobs[job_id] = {
                "status":        "error" if local_error else "done",
                "progress":      1.0,
                "node_hash":     node_hash.lower(),
                "path":          path,
                "started":       time.time(),
                "completed":     time.time(),
                "content":       content,
                "error":         local_error,
                "response_size": len(content) if content is not None else 0,
                "transfer_size": len(content) if content is not None else 0,
                "scan_result":   scan_dict,
            }
        return jsonify({
            "job_id":    job_id,
            "filename":  filename,
            "mime_type": mime_type,
        })

    # Remote — go through the normal async RNS fetch.
    job_id = _browser().fetch_page_async(
        node_hash, path,
        identify_with=_identity_for_fetch(node_hash),
    )
    return jsonify({
        "job_id":    job_id,
        "filename":  filename,
        "mime_type": mime_type,
    })


@bp.get("/api/file/poll")
def api_file_poll():
    """Poll a file-fetch job. Returns {status, progress, bytes_received}.

    Distinct from /api/page/poll because the "done" payload here is a
    *handoff* to /api/file/download rather than inline HTML — the
    binary content is held in the job entry until /api/file/download
    consumes it. Drop-on-error matches the page-poll semantics so
    failed jobs don't accumulate.
    """
    job_id = request.args.get("id", "")
    if not job_id:
        return jsonify({"error": "id required"}), 400

    job = _browser().get_job(job_id)
    if not job:
        return jsonify({"error": "unknown or expired job"}), 404

    content = job.get("content")
    # Once the response has been fully assembled, bytes_received matches
    # len(content). While the resource is in flight, bytes_received tracks
    # the RNS-reported `response_transfer_size` so the UI can show real
    # progress instead of "0 B" the whole way down.
    if content is not None:
        bytes_received = len(content)
    else:
        bytes_received = int(job.get("transfer_size") or 0)
    base = {
        "status":         job["status"],
        "progress":       round(job.get("progress", 0.0), 3),
        "bytes_received": bytes_received,
        "total_size":     int(job["response_size"]) if job.get("response_size") else None,
        "scan_result":    job.get("scan_result"),
    }

    if job["status"] == "error":
        err = job.get("error", "Unknown error")
        _browser().drop_job(job_id)
        return jsonify({**base, "error": err}), 503

    return jsonify(base)


@bp.get("/api/file/download")
def api_file_download():
    """Stream a completed file-fetch job as an attachment, then drop it.

    The job is dropped after a successful read so the in-memory content
    buffer doesn't linger past its useful life. If the client never hits
    this endpoint, the periodic cleanup_jobs() sweep evicts the entry.

    Defense in depth: even though the fetch worker clears `content` and
    sets status=error on an infected scan result, double-check the scan
    verdict here so a manually-crafted GET against a stale done-job ID
    can't bypass the scanner.
    """
    from flask import Response
    job_id = request.args.get("id", "")
    if not job_id:
        return jsonify({"error": "id required"}), 400

    job = _browser().get_job(job_id)
    if not job:
        return jsonify({"error": "unknown or expired job"}), 404
    if job["status"] != "done":
        return jsonify({"error": f"job not ready (status={job['status']})"}), 409

    scan = job.get("scan_result") or {}
    if scan.get("verdict") == "infected":
        _browser().drop_job(job_id)
        return jsonify({
            "error":     "Download blocked: virus scan flagged this file",
            "signature": scan.get("signature", ""),
        }), 403

    content = job.get("content") or b""
    path = job.get("path", "/")
    filename = path.rsplit("/", 1)[-1] or "download"
    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type:
        mime_type = "application/octet-stream"

    # Delayed drop so browsers that re-request the download URL from
    # a separate context can still get the file. DuckDuckGo Android
    # is the concrete reproducer: WebView (Chrome UA) fetches the
    # URL, then DDG's download-manager process (ddg_android UA)
    # re-requests to persist the file. Without the grace window,
    # request #2 gets 404 and DDG reports "Failed to download."
    # 60 s is well beyond any browser's second-request latency and
    # trivial on memory even for multi-MB files.
    _browser().drop_job(job_id, grace_seconds=60)

    resp = Response(content, mimetype=mime_type)
    # RFC 5987 encoding for the filename — keeps non-ASCII names intact
    # without breaking older browsers that ignore filename*.
    safe_ascii = filename.encode("ascii", "replace").decode("ascii")
    quoted_utf8 = urllib.parse.quote(filename, safe="")
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{quoted_utf8}'
    )
    resp.headers["Content-Length"] = str(len(content))
    return resp


# ---------------------------------------------------------------------------
# Auth status  (always public — used by frontend to show/hide UI)
# ---------------------------------------------------------------------------

@bp.get("/api/auth/status")
def api_auth_status():
    if current_user.is_authenticated:
        return jsonify({
            "logged_in":   True,
            "is_admin":    getattr(current_user, "is_admin", False),
            "super_admin": getattr(current_user, "super_admin", False),
            "user": {
                "name":  current_user.name,
                "email": current_user.email,
            },
            "login_url":  "/auth/start",
            "logout_url": "/auth/logout",
            "admin_url":  "/admin",
        })
    return jsonify({
        "logged_in":   False,
        "is_admin":    False,
        "super_admin": False,
        "login_url":   "/auth/start",
    })


# ---------------------------------------------------------------------------
# Messaging  (login required)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Chat attachment caps  (see docs/design/chat-uploads.md, "Locked decisions")
# ---------------------------------------------------------------------------
# LXMF is transported as RNS Resources; there's no hard protocol cap on
# message size but transfer time on multi-hop mesh links makes multi-MB
# payloads impractical. The 500 KB per-attachment / per-message cap
# below is comfortable headroom over a well-compressed JPEG (~50-200
# KB) or a ~90-second Opus voice note at 32 kbps (~360 KB), while
# staying inside a handful of RNS packet round-trips on a healthy link.
# Overridable via env var so operators on faster links can raise it,
# or on slower links can tighten it. Same value gates both dimensions
# so the semantics stay simple.

def _attachment_max_bytes() -> int:
    """Read the configured per-attachment / per-message attachment cap.

    Env var ``LXMF_ATTACHMENT_MAX_BYTES`` overrides the 500 KB default
    (524288). Bad values fall back to the default rather than crashing
    request handling — an operator typo shouldn't wedge messaging.
    """
    import os
    raw = os.environ.get("LXMF_ATTACHMENT_MAX_BYTES", "").strip()
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    return 500 * 1024


_MAX_ATTACHMENT_COUNT = 10


@bp.post("/api/messages")
@login_required
def api_message_send():
    ip = request.remote_addr or "unknown"
    if not rate_limit.check(f"msg:{ip}", 10, 60):
        return jsonify({"error": "Rate limit exceeded — slow down"}), 429

    # Admins are always allowed to send. For other users, the admin can
    # disable LXMF send via Settings → "Can send LXMF messages".
    if not getattr(current_user, "is_admin", False):
        ui = current_app.config.get("UI_SETTINGS")
        if ui and not ui.get_all().get("users_can_message", True):
            return jsonify({"error": "LXMF messaging is disabled by the administrator"}), 403

    # Two request shapes accepted:
    #   1. Text-only (existing) — Content-Type: application/json,
    #      body: {"dest_hash", "title", "content"}.
    #   2. With attachments (v1.3.0 step 4) — Content-Type:
    #      multipart/form-data with the same three text fields plus
    #      one or more file parts named "attachments".
    ct = (request.content_type or "").lower()
    is_multipart = ct.startswith("multipart/form-data")

    attachments = None

    if is_multipart:
        dest_hash = request.form.get("dest_hash", "")
        title     = request.form.get("title", "")
        content   = request.form.get("content", "")
        files     = request.files.getlist("attachments")

        if len(files) > _MAX_ATTACHMENT_COUNT:
            return jsonify({
                "error": f"too many attachments (max {_MAX_ATTACHMENT_COUNT})"
            }), 413

        cap = _attachment_max_bytes()
        total = 0
        parsed = []
        for f in files:
            if not f or not f.filename:
                continue
            data = f.read()
            n = len(data)
            if n > cap:
                return jsonify({
                    "error": (
                        f"attachment '{f.filename}' is {n} bytes; "
                        f"per-file cap is {cap}"
                    )
                }), 413
            total += n
            if total > cap:
                return jsonify({
                    "error": (
                        f"total attachment size exceeds {cap} bytes"
                    )
                }), 413
            # ``mimetype`` on FileStorage is the browser-supplied Content-Type
            # from the multipart part. Trust it as a hint — the wire format
            # doesn't care about MIME per se, only about which LXMF field
            # slot to use, and the frontend's <input type="file"> gives us
            # the browser's best guess. Fall back to octet-stream so an
            # ancient client that omits the header still works.
            parsed.append({
                "data":     data,
                "filename": f.filename,
                "mime":     (f.mimetype or "application/octet-stream").lower(),
            })

        if parsed:
            attachments = parsed
    else:
        data      = request.get_json(silent=True) or {}
        dest_hash = data.get("dest_hash", "")
        title     = data.get("title", "")
        content   = data.get("content", "")

    if not dest_hash:
        abort(400, description="dest_hash is required")
    if len(dest_hash) > 128:
        abort(400, description="dest_hash too long")
    if len(title) > 256:
        abort(400, description="title exceeds 256 characters")
    if len(content) > 65536:
        abort(400, description="content exceeds 64 KB")

    # Reject empty sends — no text AND no attachments is not a message.
    if not (content or "").strip() and not attachments:
        abort(400, description="message must have content or an attachment")

    messaging = current_app.config["MESSAGING"]
    ok, result = messaging.send_message(
        dest_hash, content,
        title=title, user_sub=current_user.id,
        attachments=attachments,
    )
    if not ok:
        return jsonify({"error": result}), 503
    return jsonify({"ok": True, "message_id": result})


@bp.get("/api/messages/sent")
@login_required
def api_messages_sent():
    messaging = current_app.config["MESSAGING"]
    uid = current_user.id
    msgs = [m for m in messaging.sent_messages() if m.get("owner") == uid]
    return jsonify({"messages": msgs})


@bp.get("/api/messages/received")
@login_required
def api_messages_received():
    messaging = current_app.config["MESSAGING"]
    uid = current_user.id
    msgs = [m for m in messaging.received_messages() if m.get("owner") == uid]
    return jsonify({"messages": msgs})


@bp.post("/api/messages/received/<msg_id>/read")
@login_required
def api_message_mark_read(msg_id: str):
    current_app.config["MESSAGING"].mark_read(msg_id, owner=current_user.id)
    return jsonify({"ok": True})


@bp.delete("/api/messages/conversation/<hash_hex>")
@login_required
def api_conversation_delete(hash_hex: str):
    store = current_app.config.get("MESSAGE_STORE")
    removed = store.delete_conversation(hash_hex, owner=current_user.id) if store else 0
    return jsonify({"ok": True, "removed": removed})


@bp.get("/api/messages/<msg_id>/attachments/<int:idx>")
@login_required
def api_message_attachment(msg_id: str, idx: int):
    """Stream an inbound message's attachment blob.

    Auth-gated to the message's owner — a peer can't guess a
    (msg_id, idx) tuple to exfiltrate someone else's attachment
    because the ownership check gates access. Bytes are served from
    ``AttachmentStore`` (see ``docs/design/chat-uploads.md``);
    ``messages.json`` carries only the metadata that lets us find
    the right blob + emit the correct ``Content-Type``.
    """
    from flask import Response

    msg_store = current_app.config.get("MESSAGE_STORE")
    att_store = current_app.config.get("ATTACHMENT_STORE")
    if msg_store is None or att_store is None:
        abort(404)

    # Find the message + verify ownership + verify the attachment slot
    # is real. Scanning `received_messages()` is O(N=500) worst case;
    # trivial for a message store this size and avoids a schema change
    # to key messages by id.
    uid = current_user.id
    entry = next(
        (m for m in msg_store.received_messages()
         if m.get("id") == msg_id and m.get("owner") == uid),
        None,
    )
    # Sent messages can also carry attachments once step 4 lands;
    # allow the endpoint to serve them too for symmetry.
    if entry is None:
        entry = next(
            (m for m in msg_store.sent_messages()
             if m.get("id") == msg_id and m.get("owner") == uid),
            None,
        )
    if entry is None:
        abort(404)

    atts = entry.get("attachments") or []
    if idx < 0 or idx >= len(atts):
        abort(404)
    meta = atts[idx]

    data = att_store.read(msg_id, idx)
    if data is None:
        # Message metadata says the attachment should exist but the
        # blob is gone — inconsistent state (manual disk cleanup,
        # corrupted store). Return 404 rather than a confusing 500.
        log.warning("Attachment metadata but no blob: %s/%s", msg_id[:16], idx)
        abort(404)

    resp = Response(data, mimetype=meta.get("mime") or "application/octet-stream")
    # Inline disposition so images render in-page rather than
    # triggering a download. If we ever add a "download this
    # attachment" button, that can call the same endpoint with a
    # ``?download=1`` query param and swap to ``attachment``.
    filename = meta.get("filename") or f"attachment-{idx}"
    safe_ascii  = filename.encode("ascii", "replace").decode("ascii")
    quoted_utf8 = urllib.parse.quote(filename, safe="")
    resp.headers["Content-Disposition"] = (
        f'inline; filename="{safe_ascii}"; filename*=UTF-8\'\'{quoted_utf8}'
    )
    resp.headers["Content-Length"] = str(len(data))
    return resp


# ---------------------------------------------------------------------------
# Identity  (login required)
# ---------------------------------------------------------------------------

@bp.get("/api/my-identity")
@login_required
def api_my_identity():
    entry = _id_store().ensure_for_user(
        current_user.id, getattr(current_user, "name", "")
    )
    messaging = current_app.config.get("MESSAGING")
    lxmf_address = messaging.lxmf_address(user_sub=current_user.id) if messaging else None
    return jsonify({
        "identity": {
            "id":               entry["id"],
            "name":             entry["name"],
            "last_announced":   entry.get("last_announced"),
            "lxmf_address":     lxmf_address,
            "icon":             entry.get("icon"),
            "identified_nodes": entry.get("identified_nodes") or [],
        }
    })


@bp.get("/api/my-identity/qr")
@login_required
def api_my_identity_qr():
    """SVG QR code for the current user's own LXMF address — "show my
    QR", per explicit direction not paired with any in-app scan flow.
    See identity_qr.py's own doc comment for the lxma://<hash>:<pubkey>
    payload format and why it carries the public key, not just the
    hash."""
    from . import identity_qr

    entry = _id_store().ensure_for_user(
        current_user.id, getattr(current_user, "name", "")
    )
    identity = _id_store().load_rns_identity(entry["id"])
    messaging = current_app.config.get("MESSAGING")
    lxmf_address = messaging.lxmf_address(user_sub=current_user.id) if messaging else None
    if identity is None or not lxmf_address:
        abort(503, description="Identity not ready yet")

    payload = identity_qr.build_identity_qr_payload(
        lxmf_address, identity.get_public_key().hex()
    )
    svg = identity_qr.render_qr_svg(payload)

    from flask import Response
    return Response(svg, mimetype="image/svg+xml")


@bp.post("/api/my-identity/icon")
@login_required
def api_my_identity_icon_set():
    data  = request.get_json(silent=True) or {}
    glyph = (data.get("glyph") or "?").strip()
    fg    = data.get("fg", "")
    bg    = data.get("bg", "")
    entry = _id_store().get_for_user(current_user.id)
    if not entry:
        return jsonify({"ok": False, "error": "Identity not found"}), 404
    _id_store().set_icon_appearance(entry["id"], glyph, fg, bg)
    return jsonify({"ok": True, "icon": _id_store().get_icon_appearance(entry["id"])})


@bp.post("/api/identities/<identity_id>/rename")
@login_required
def api_identity_rename(identity_id: str):
    data = request.get_json(force=True, silent=True) or {}
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return jsonify({"ok": False, "error": "Name is required"}), 400

    # Ownership check. IdentityStore.rename() itself has none — without
    # this, identity_id being taken straight from the URL meant any
    # logged-in user could rename *any other user's* LXMF identity just
    # by knowing/obtaining its hex ID (visible in plenty of places: a
    # message's source hash, a contact record, an admin panel). Only the
    # owning user may rename their own identity; no admin feature relies
    # on renaming someone else's (checked — nothing in admin_routes.py
    # or the admin templates calls this route for another user).
    entry = _id_store().get(identity_id)
    if entry is None:
        return jsonify({"ok": False, "error": "Identity not found"}), 404
    if entry.get("user_sub") != current_user.id:
        return jsonify({"ok": False, "error": "Not authorized to rename this identity"}), 403

    if not _id_store().rename(identity_id, new_name):
        return jsonify({"ok": False, "error": "Identity not found"}), 404

    # Live-refresh so an announce made right after this doesn't still
    # carry the old name — see MessagingService.refresh_router_display_name
    # for the real bug this closes (a rename alone is dead code for any
    # announce until the next process restart). Only when [identity_id]
    # is the account's *currently active* identity: refresh_router_display_name
    # blindly overwrites whichever router is live for this user_sub, and
    # with multi-identity that live router may belong to a *different*
    # identity than the one just renamed — applying it unconditionally
    # would leak this rename onto the active identity's own live display
    # name instead.
    active = _id_store().get_active_for_user(current_user.id)
    if active is not None and active["id"] == identity_id:
        current_app.config["MESSAGING"].refresh_router_display_name(current_user.id, new_name)
    return jsonify({"ok": True})


@bp.get("/api/identities")
@login_required
def api_identities_list():
    """All identities owned by the current account — the "Settings →
    Identities" list. Ensures the account has at least one identity
    first (same bootstrap ``/api/my-identity`` already relies on) so a
    brand-new account never sees an empty list here."""
    store = _id_store()
    store.ensure_for_user(current_user.id, getattr(current_user, "name", ""))
    active = store.get_active_for_user(current_user.id)
    active_id = active["id"] if active else None
    out = []
    for entry in store.list_for_user(current_user.id):
        out.append({
            "id":           entry["id"],
            "name":         entry["name"],
            "lxmf_address": store.get_dest_hash_hex(entry["id"]),
            "icon":         entry.get("icon"),
            "created":      entry.get("created"),
            "is_active":    entry["id"] == active_id,
        })
    return jsonify({"identities": out})


@bp.post("/api/identities")
@login_required
def api_identity_create():
    """Create a new identity owned by the current account. Does not
    switch to it — the UI calls .../activate separately, same
    "create, then explicitly activate" split as every other
    multi-identity mutation here."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:128]
    entry = _id_store().create_for_user(current_user.id, name)
    return jsonify({"ok": True, "identity": {"id": entry["id"], "name": entry["name"]}})


@bp.post("/api/identities/<identity_id>/activate")
@login_required
def api_identity_activate(identity_id: str):
    """Switch the account's active identity — tears down the
    currently-live router and brings up [identity_id]'s instead. See
    MessagingService.deactivate_user()/activate_user() for the real
    LXMRouter lifecycle this drives."""
    store = _id_store()
    entry = store.get(identity_id)
    if entry is None or entry.get("user_sub") != current_user.id:
        return jsonify({"ok": False, "error": "Identity not found"}), 404

    active = store.get_active_for_user(current_user.id)
    if active is not None and active["id"] == identity_id:
        return jsonify({"ok": True})  # already active — no teardown/rebuild needed

    messaging = current_app.config.get("MESSAGING")
    if messaging is not None:
        messaging.deactivate_user(current_user.id)
    if not store.set_active_for_user(current_user.id, identity_id):
        return jsonify({"ok": False, "error": "Could not switch identity"}), 500
    if messaging is not None:
        messaging.activate_user(entry)
    return jsonify({"ok": True})


@bp.delete("/api/identities/<identity_id>")
@login_required
def api_identity_delete(identity_id: str):
    """Delete one of the account's own identities. Refused for an
    identity not owned by this account or the account's only one — see
    IdentityStore.delete_for_user()'s own doc comment for why this
    deliberately does not auto-create a replacement the way the
    NomadPortal-Android sister project's delete does."""
    store = _id_store()
    was_active = False
    active = store.get_active_for_user(current_user.id)
    if active is not None and active["id"] == identity_id:
        was_active = True

    ok, message = store.delete_for_user(current_user.id, identity_id)
    if not ok:
        return jsonify({"ok": False, "error": message}), 400

    messaging = current_app.config.get("MESSAGING")
    if was_active and messaging is not None:
        # The old router (for the identity that just got deleted) is
        # torn down; delete_for_user() already reassigned active status
        # to another of this account's identities, so bring that one's
        # router up now rather than waiting for the next lazy fetch.
        messaging.deactivate_user(current_user.id)
        new_active = store.get_active_for_user(current_user.id)
        if new_active is not None:
            messaging.activate_user(new_active)
    return jsonify({"ok": True})


@bp.post("/api/identities/<identity_id>/icon")
@login_required
def api_identity_icon_set(identity_id: str):
    """Set the icon appearance for a specific (not necessarily active)
    owned identity. .../my-identity/icon remains the shortcut for the
    currently-active one."""
    store = _id_store()
    entry = store.get(identity_id)
    if entry is None or entry.get("user_sub") != current_user.id:
        return jsonify({"ok": False, "error": "Identity not found"}), 404
    data  = request.get_json(silent=True) or {}
    glyph = (data.get("glyph") or "?").strip()
    fg    = data.get("fg", "")
    bg    = data.get("bg", "")
    store.set_icon_appearance(identity_id, glyph, fg, bg)
    return jsonify({"ok": True, "icon": store.get_icon_appearance(identity_id)})


@bp.get("/api/identities/<identity_id>/qr")
@login_required
def api_identity_qr(identity_id: str):
    """SVG QR code for one owned identity's LXMF address — same
    lxma://<hash>:<pubkey> payload as /api/my-identity/qr, but usable
    for any of the account's identities, not just the active one (an
    inactive identity has no live router/announced address, so the
    address is computed offline via get_dest_hash_hex instead of read
    off a live Destination)."""
    from . import identity_qr
    from flask import Response

    store = _id_store()
    entry = store.get(identity_id)
    if entry is None or entry.get("user_sub") != current_user.id:
        abort(404, description="Identity not found")

    identity = store.load_rns_identity(identity_id)
    lxmf_address = store.get_dest_hash_hex(identity_id)
    if identity is None or not lxmf_address:
        abort(503, description="Identity not ready yet")

    payload = identity_qr.build_identity_qr_payload(lxmf_address, identity.get_public_key().hex())
    svg = identity_qr.render_qr_svg(payload)
    return Response(svg, mimetype="image/svg+xml")


@bp.get("/api/identities/<identity_id>/export")
@login_required
def api_identity_export(identity_id: str):
    """Download this identity's raw ``.identity`` key file — the same
    on-disk format Columba's own export/import uses, confirmed
    cross-compatible (see IdentityStore.import_identity's own doc
    comment). The only way to move a keypair to another install; never
    a message/contact backup mechanism."""
    from flask import Response

    store = _id_store()
    entry = store.get(identity_id)
    if entry is None or entry.get("user_sub") != current_user.id:
        abort(404, description="Identity not found")
    key_bytes = store.export_key_bytes(identity_id)
    if key_bytes is None:
        abort(404, description="Key file not found")
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", entry.get("name") or identity_id[:16])
    return Response(
        key_bytes,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.identity"'},
    )


@bp.post("/api/identities/import")
@login_required
def api_identity_import():
    """Import a ``.identity`` key file (uploaded as multipart form data,
    field name ``file``) as a new identity owned by the current
    account. Rejects a keypair another account already owns — see
    IdentityStore.import_identity's own doc comment."""
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    key_bytes = upload.read()
    if not key_bytes or len(key_bytes) > 8192:
        return jsonify({"ok": False, "error": "Invalid identity file"}), 400
    name = (request.form.get("name") or "").strip()[:128]
    try:
        entry = _id_store().import_identity(key_bytes, name=name, user_sub=current_user.id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "identity": {"id": entry["id"], "name": entry["name"]}})


_HEX24_RE = re.compile(r"^[0-9a-f]+$")


@bp.post("/api/fingerprint")
@login_required
def api_fingerprint_on():
    """Turn on persistent identification for a node.

    Every subsequent page fetch from this user to this node will call
    link.identify() with their RNS identity, until disabled. The client
    typically reloads the current page right after toggling on so the
    next fetch (now identified) replaces the anonymous render.
    """
    data = request.get_json(silent=True) or {}
    dest_hash = (data.get("dest_hash") or "").strip().lower()
    if not dest_hash or not _HEX24_RE.match(dest_hash):
        return jsonify({"ok": False, "error": "Invalid hash"}), 400

    if _browser().is_blocked(dest_hash):
        return jsonify({"ok": False, "error": "This node has been blocked by the administrator."}), 403

    ip = request.remote_addr or "unknown"
    if not rate_limit.check(f"fp:{ip}", 30, 60):
        return jsonify({"ok": False, "error": "Rate limit exceeded — slow down"}), 429

    entry = _id_store().ensure_for_user(
        current_user.id, getattr(current_user, "name", "")
    )
    _id_store().set_identified(entry["id"], dest_hash, True)
    return jsonify({"ok": True, "identified": True})


@bp.delete("/api/fingerprint")
@login_required
def api_fingerprint_off():
    """Turn off persistent identification for a node."""
    data = request.get_json(silent=True) or {}
    dest_hash = (data.get("dest_hash") or "").strip().lower()
    if not dest_hash or not _HEX24_RE.match(dest_hash):
        return jsonify({"ok": False, "error": "Invalid hash"}), 400

    entry = _id_store().get_for_user(current_user.id)
    if entry:
        _id_store().set_identified(entry["id"], dest_hash, False)
    return jsonify({"ok": True, "identified": False})


@bp.post("/api/identities/<identity_id>/announce")
@login_required
def api_identity_announce(identity_id: str):
    # Ownership check — without it, any logged-in user could poke
    # another account's identity's announce cooldown (reset its timer
    # early, or read its "next allowed" message) just by knowing its
    # hex ID, even though the actual announce below always goes out
    # under the *caller's own* active identity regardless.
    entry = _id_store().get(identity_id)
    if entry is None or entry.get("user_sub") != current_user.id:
        return jsonify({"ok": False, "message": "Identity not found"}), 404

    ok, message, next_allowed = _id_store().check_cooldown(identity_id)
    if ok:
        messaging = current_app.config.get("MESSAGING")
        if messaging:
            ok, message = messaging.do_announce(user_sub=current_user.id)
        else:
            ok, message = False, "Messaging service not available"
    return jsonify({"ok": ok, "message": message, "next_allowed": next_allowed})


# ---------------------------------------------------------------------------
# Contacts  (login required)
# ---------------------------------------------------------------------------

@bp.get("/api/contacts")
@login_required
def api_contacts():
    store = _contact_store()
    contacts = store.list_contacts() if store else []
    return jsonify({"contacts": contacts})


@bp.post("/api/contacts")
@login_required
def api_contact_upsert():
    data     = request.get_json(silent=True) or {}
    hash_hex = data.get("hash", "").strip()
    if not hash_hex:
        abort(400, description="hash required")
    if len(hash_hex) > 128:
        abort(400, description="hash too long")
    name = data.get("name", "")
    note = data.get("note", "")
    if len(name) > 128:
        abort(400, description="name exceeds 128 characters")
    if len(note) > 1024:
        abort(400, description="note exceeds 1024 characters")
    store = _contact_store()
    if not store:
        abort(503)
    entry = store.upsert(hash_hex, name=name, note=note)
    return jsonify({"ok": True, "contact": entry})


@bp.post("/api/contacts/<path:hash_hex>/favorite")
@login_required
def api_contact_favorite(hash_hex: str):
    data  = request.get_json(silent=True) or {}
    store = _contact_store()
    if not store:
        abort(503)
    if not store.get(hash_hex):
        store.upsert(hash_hex)
    ok = store.set_favorite(hash_hex, bool(data.get("favorited", True)))
    return jsonify({"ok": ok})


@bp.post("/api/contacts/<path:hash_hex>/block")
@login_required
def api_contact_block(hash_hex: str):
    """Block/unblock a sender — enforced in messaging.py's own
    _on_delivery, which silently drops any inbound message from a
    blocked hash before it's stored or processed. Works even for a
    hash with no existing contact record (see ContactStore.
    set_blocked's own doc comment for why)."""
    data  = request.get_json(silent=True) or {}
    store = _contact_store()
    if not store:
        abort(503)
    entry = store.set_blocked(hash_hex, bool(data.get("blocked", True)))
    return jsonify({"ok": True, "contact": entry})


@bp.delete("/api/contacts/<path:hash_hex>")
@login_required
def api_contact_delete(hash_hex: str):
    store = _contact_store()
    if not store:
        abort(503)
    ok = store.delete(hash_hex)
    return jsonify({"ok": ok})


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

@bp.post("/api/nodes/<path:hash_hex>/favorite")
@login_required
def api_node_favorite(hash_hex: str):
    value = request.get_json(silent=True) or {}
    ok = _browser().set_favorite(hash_hex, bool(value.get("favorited", True)), user_sub=current_user.id)
    return jsonify({"ok": ok})


# ---------------------------------------------------------------------------
# Page-level favorites (login-required) — bookmark (hash, path, name) tuples
# ---------------------------------------------------------------------------

_HASH_RE = re.compile(r"^[0-9a-f]+$")


def _normalise_fav_path(path: str) -> str:
    p = (path or "/").strip()
    if not p.startswith("/"):
        p = "/" + p
    # Strip any double-slashes and trailing slash on non-root paths.
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p or "/"


@bp.get("/api/favorites")
@login_required
def api_favorites_list():
    return jsonify({"favorites": _browser().get_favorites(user_sub=current_user.id)})


@bp.post("/api/favorites")
@login_required
def api_favorites_add():
    data = request.get_json(silent=True) or {}
    hash_hex = (data.get("hash") or "").strip().lower()
    path     = _normalise_fav_path(data.get("path") or "/")
    name     = (data.get("name") or "").strip()

    if not hash_hex or not _HASH_RE.match(hash_hex):
        return jsonify({"ok": False, "error": "Invalid hash"}), 400
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    if len(name) > 80:
        name = name[:80]

    ok = _browser().set_favorite(
        hash_hex, True, user_sub=current_user.id, path=path, name=name,
    )
    return jsonify({"ok": ok})


@bp.delete("/api/favorites")
@login_required
def api_favorites_remove():
    data = request.get_json(silent=True) or {}
    hash_hex = (data.get("hash") or "").strip().lower()
    path     = _normalise_fav_path(data.get("path") or "/")
    if not hash_hex or not _HASH_RE.match(hash_hex):
        return jsonify({"ok": False, "error": "Invalid hash"}), 400
    ok = _browser().set_favorite(
        hash_hex, False, user_sub=current_user.id, path=path,
    )
    return jsonify({"ok": ok})


@bp.delete("/api/cache")
@login_required
def api_cache_clear():
    _cache().clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Blocklist  (admin only — enforced in admin_routes)
# ---------------------------------------------------------------------------

@bp.get("/api/blocklist")
@login_required
def api_blocklist_get():
    if not getattr(current_user, "is_admin", False):
        abort(403)
    return jsonify({"blocked": _browser().get_blocklist()})


@bp.post("/api/blocklist")
@login_required
def api_blocklist_add():
    if not getattr(current_user, "is_admin", False):
        abort(403)
    data     = request.get_json(silent=True) or {}
    hash_hex = data.get("hash", "").strip().lower()
    if not hash_hex:
        abort(400, description="hash required")
    if len(hash_hex) > 128:
        abort(400, description="hash too long")
    _browser().block_node(hash_hex)
    return jsonify({"ok": True})


@bp.delete("/api/blocklist/<path:hash_hex>")
@login_required
def api_blocklist_remove(hash_hex: str):
    if not getattr(current_user, "is_admin", False):
        abort(403)
    ok = _browser().unblock_node(hash_hex)
    return jsonify({"ok": ok})


@bp.get("/api/site/info")
def api_site_info():
    """Return home-node info if this instance is hosting a NomadNet site.

    ``auto_announce`` is included so the dashboard / sidebar can
    disable the "Announce now" button when the node is in silent
    mode — manual announces are refused in that state.
    """
    server = current_app.config.get("SITE_SERVER")
    if server and server.node_hash():
        return jsonify({
            "enabled":       True,
            "node_hash":     server.node_hash(),
            "node_name":     server.node_name(),
            "auto_announce": bool(getattr(server, "_auto_announce", False)),
        })
    return jsonify({
        "enabled":       False,
        "node_hash":     None,
        "node_name":     None,
        "auto_announce": False,
    })


@bp.post("/api/site/announce")
@login_required
def api_site_announce():
    """Force the site node to send an announce on the mesh.

    Refuses when the node is configured as a silent host
    (``auto_announce=False``). Silent means silent — no manual escape
    hatch; the operator should flip Admin → Settings → Auto-announce
    → On if they actually want to publish. Defense in depth alongside
    the frontend disabling the button.
    """
    server = current_app.config.get("SITE_SERVER")
    if not server or not server.node_hash():
        return jsonify({"ok": False, "error": "Site server not running"}), 404
    if not getattr(server, "_auto_announce", False):
        return jsonify({
            "ok": False,
            "error": "Node is in silent mode — enable Auto-announce in "
                     "Admin → Settings before publishing.",
        }), 409
    try:
        server.announce()
        return jsonify({
            "ok": True,
            "message": f"Site node announced ({server.node_hash()[:16]})",
        })
    except Exception:
        log.exception("Site announce failed")
        return jsonify({"ok": False,
                        "error": "announce failed (see server log)"}), 500


@bp.get("/api/ui/settings")
def api_ui_settings():
    """Return UI display settings (public — frontend needs these before login)."""
    ui = current_app.config.get("UI_SETTINGS")
    data = ui.get_all() if ui else {}
    data["app_title_html"]  = _render_title_html(data.get("app_title", ""))
    data["app_title_plain"] = _render_title_plain(data.get("app_title", ""))
    data["allow_guest_external_browse"] = bool(
        current_app.config.get("ALLOW_GUEST_EXTERNAL_BROWSE", False)
    )
    return jsonify(data)


@bp.get("/api/lxmf-peers")
def api_lxmf_peers():
    """Public read, same as /api/nodes — per explicit direction, a
    "Network only" guest deployment needs sites/peers/relays all
    viewable without an account. The Messages panel's own Users tab
    still gates itself behind login client-side (refreshLxmfPeers()'s
    own check) — this only opens the Network tab's read of the same
    global peer-announce data; nothing about *messaging* a peer
    changes, that still requires a real identity either way. Panel-
    visibility toggles (guests_network_panel etc.) are a UI curation
    feature, not the security boundary — same pattern /api/nodes
    already established (public regardless of guests_nodes_panel)."""
    tracker = current_app.config.get("LXMF_TRACKER")
    peers   = tracker.get_peers() if tracker else []
    return jsonify({"peers": peers})


@bp.get("/api/relays")
def api_relays():
    """LXMF propagation-node (store-and-forward mesh infrastructure)
    announces heard so far — the Network tab's Relays list. Public
    read, same reasoning as /api/lxmf-peers above (see its own doc
    comment) — mesh topology, not per-account data."""
    prop_sync = current_app.config.get("PROP_SYNC")
    relays = prop_sync.list_known_nodes() if prop_sync else []
    return jsonify({"relays": relays})


# ---------------------------------------------------------------------------
# rnsh (remote shell over Reticulum) — Settings → Terminal. Client
# (initiator) only — see rnsh_client.py's own doc comment for the real
# "never a listener" scoping decision. One session at a time per
# account (RnshManager), authenticated with that account's currently
# active LXMF identity.
# ---------------------------------------------------------------------------

def _rnsh():
    return current_app.config["RNSH"]


@bp.post("/api/rnsh/connect")
@login_required
def api_rnsh_connect():
    data = request.get_json(silent=True) or {}
    dest_hash = (data.get("dest_hash") or "").strip().lower()
    if not dest_hash or not _HEX24_RE.match(dest_hash):
        return jsonify({"ok": False, "error": "Invalid destination hash"}), 400

    messaging = current_app.config.get("MESSAGING")
    identity = messaging.get_identity(current_user.id) if messaging else None
    if identity is None:
        return jsonify({"ok": False, "error": "No delivery identity registered yet — try again shortly"}), 503

    _rnsh().connect(current_user.id, identity, dest_hash)
    return jsonify({"ok": True, "message": "Connecting…"})


@bp.get("/api/rnsh/status")
@login_required
def api_rnsh_status():
    return jsonify(_rnsh().status(current_user.id))


@bp.get("/api/rnsh/output")
@login_required
def api_rnsh_output():
    """{"data_b64": "..."} — base64, never raw bytes, so a chunk that
    happens to split a multi-byte UTF-8 character mid-stream can't
    corrupt the JSON response itself; the browser reassembles and
    decodes it client-side. Empty string when there's nothing new to
    report — never an error, just "nothing to show yet"."""
    import base64
    data = _rnsh().read_output(current_user.id)
    return jsonify({"data_b64": base64.b64encode(data).decode("ascii") if data else ""})


@bp.post("/api/rnsh/input")
@login_required
def api_rnsh_input():
    import base64
    data = request.get_json(silent=True) or {}
    try:
        raw = base64.b64decode(data.get("data_b64") or "", validate=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid input"}), 400
    if len(raw) > 8192:
        return jsonify({"ok": False, "error": "Input too large"}), 400
    _rnsh().send_input(current_user.id, raw)
    return jsonify({"ok": True})


@bp.post("/api/rnsh/resize")
@login_required
def api_rnsh_resize():
    data = request.get_json(silent=True) or {}
    try:
        rows = max(1, min(500, int(data.get("rows", 24))))
        cols = max(1, min(2000, int(data.get("cols", 80))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid size"}), 400
    _rnsh().resize(current_user.id, rows, cols)
    return jsonify({"ok": True})


@bp.post("/api/rnsh/disconnect")
@login_required
def api_rnsh_disconnect():
    _rnsh().disconnect(current_user.id)
    return jsonify({"ok": True})

import logging
import os
import threading
import time
import datetime
from flask import Flask, abort, has_request_context, redirect, render_template, request, send_from_directory
from flask.sessions import SecureCookieSessionInterface

# Bumped per release. Logged at startup so the running image's version is
# visible in `docker logs` without needing `docker inspect`.
__version__ = "1.0.0"
from .routes import bp
from .cache import PageCache
from .browser import NodeBrowser
from .auth import auth_bp, init_auth
from .admin_routes import admin_bp
from .identity_store import IdentityStore
from .messaging import MessagingService
from .lxmf_sync import PropagationSyncService
from .message_store import MessageStore
from .contact_store import ContactStoreManager
from .user_store import UserStore
from .lxmf_tracker import LXMFPeerTracker
from .ui_settings import UISettings
from .log_buffer import buffer as log_buffer
from . import csrf as csrf_mod

log = logging.getLogger(__name__)


def create_app(
    browser: NodeBrowser,
    cache: PageCache = None,
    config: dict = None,
) -> Flask:
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )

    cfg = config or {}
    app.config.update(cfg)
    app.config["BROWSER"]    = browser
    app.config["CACHE"]      = cache or PageCache()
    app.config["START_TIME"] = time.time()

    # Virus scanner — pluggable, defaults to off. Attaches to the browser
    # so the async fetch worker can call into it without a Flask
    # current_app reference (it runs outside the request context).
    from .scanner import build_scanner_from_config
    scanner, scan_required = build_scanner_from_config(cfg)
    app.config["VIRUS_SCANNER"]          = scanner
    app.config["VIRUS_SCAN_REQUIRED"]    = scan_required
    browser.scanner          = scanner
    browser.scan_required    = scan_required

    config_dir = cfg.get("CONFIG_DIR", "/config")
    rns_dir    = cfg.get("RNS_CONFIG_DIR", "/config/reticulum")

    # RNS-dependent setup steps get queued here and run in a background
    # thread AFTER the browser's own RNS init has finished (see the
    # trailing block in create_app that launches that thread). Keeping
    # them off the main path is what lets gunicorn accept connections
    # in seconds instead of waiting the ~3 minutes RNS.Reticulum()
    # blocks for during startup.
    _deferred_rns_actions: list = []

    def _defer_after_rns(name: str, fn):
        """Queue an RNS-touching callable to run after ``browser.wait_ready()``.
        Exceptions inside ``fn`` are caught and logged (with ``name``) so one
        failing step doesn't kill the rest of the background init.
        """
        _deferred_rns_actions.append((name, fn))

    msg_store = MessageStore(config_dir)
    con_store = ContactStoreManager(config_dir)
    app.config["IDENTITY_STORE"] = IdentityStore(rns_dir)
    app.config["MESSAGE_STORE"]  = msg_store
    app.config["CONTACT_STORE"]  = con_store
    messaging = MessagingService(
        storage_path=os.path.join(rns_dir, "lxmf"),
        message_store=msg_store,
        contact_store=con_store,
    )
    app.config["MESSAGING"] = messaging

    # Pre-create the local admin's identity at startup (when local login
    # is enabled) so its LXMF router comes up immediately during
    # setup_delivery — messages addressed to the local admin are then
    # received whenever the container is running, regardless of whether
    # the admin has logged in this session, last week, or ever.
    # Without this, the identity is created lazily on first login, so
    # messages sent before that first login are dropped at the network
    # level (no delivery destination registered).
    if cfg.get("ADMIN_PASSWORD"):
        admin_user = cfg.get("ADMIN_USERNAME", "admin")
        admin_sub  = f"local:{admin_user}"
        try:
            # Side-effect call: ensure_for_user creates the identity if
            # absent. We don't need the returned record any more — v0.9.23
            # dropped admin_sub / entry["id"] from the log line below to
            # quiet a CodeQL false-positive on identity logging.
            app.config["IDENTITY_STORE"].ensure_for_user(
                admin_sub, admin_user,
            )
            # admin_sub is constructed from the ADMIN_USERNAME env var.
            # CodeQL's clear-text-logging-sensitive-data rule
            # heuristically tags it as identity-related and persistently
            # flags any log line that includes it — .replace barriers
            # didn't quiet it. Address by dropping the variable from the
            # message entirely; the diagnostic value (this admin's
            # identity is ready) is preserved without echoing the
            # operator's chosen username.
            log.info(
                "Local admin LXMF identity ready — "
                "messages received whenever the container is running."
            )
        except Exception as exc:
            log.warning("Could not pre-create local admin identity: %s", exc)

    # LXMF delivery setup registers LXMF destinations with the running
    # RNS Transport, so it has to wait for that to be up.
    _defer_after_rns(
        "LXMF delivery setup",
        lambda: messaging.setup_delivery(app.config["IDENTITY_STORE"]),
    )

    # LXMF propagation-node outbound sync — see nomadnet_web/lxmf_sync.py
    # for rationale. Deferred so it starts AFTER setup_delivery has
    # registered the admin router; the service can then include it in
    # its per-tick iteration. Auto-discovers propagation nodes from
    # lxmf.propagation announces; no operator config needed. Runs
    # continuously; belt-and-braces with the default-node keepalive in
    # browser.py (both push RNS in the "warmer" direction independently).
    prop_sync = PropagationSyncService(
        rns=browser._rns,
        messaging_service=messaging,
    )
    app.config["PROP_SYNC"] = prop_sync
    _defer_after_rns("LXMF propagation sync service", prop_sync.start)

    lxmf_tracker = LXMFPeerTracker(config_dir)
    app.config["LXMF_TRACKER"] = lxmf_tracker

    def _register_lxmf_tracker():
        import RNS
        RNS.Transport.register_announce_handler(lxmf_tracker.register_announce_handler())
        log.info("LXMF peer tracker registered")
    _defer_after_rns("LXMF tracker registration", _register_lxmf_tracker)

    users_yml = cfg.get("USERS_YML", "/config/users.yml")
    app.config["USER_STORE"]  = UserStore(users_yml)
    app.config["UI_SETTINGS"] = UISettings(config_dir)   # must be before site server

    # Site hosting — start node server unless explicitly disabled. The
    # operator can configure this at two layers:
    #   1. Admin → Settings UI (persisted to ui_settings.json) — wins
    #      when set explicitly (True/False)
    #   2. SITE_HOSTING / SITE_ANNOUNCE env vars — used when the UI
    #      value is unset (None)
    # The UI-over-env precedence lets ops toggle without editing
    # docker-compose and restarting the host; the env var still works
    # for fresh installs that haven't visited the admin page yet.
    pages_dir = cfg.get("SITE_PAGES_DIR", "/site/pages")
    files_dir = cfg.get("SITE_FILES_DIR", "/site/files")
    ui_all    = app.config["UI_SETTINGS"].get_all()

    ui_hosting = ui_all.get("hosting_enabled")
    if ui_hosting is None:
        hosting_raw     = str(cfg.get("SITE_HOSTING", "true")).strip().lower()
        hosting_enabled = hosting_raw not in ("0", "false", "no", "off", "")
    else:
        hosting_enabled = bool(ui_hosting)

    ui_announce = ui_all.get("auto_announce")
    if ui_announce is None:
        announce_raw  = str(cfg.get("SITE_ANNOUNCE", "false")).strip().lower()
        auto_announce = announce_raw in ("1", "true", "yes", "on")
    else:
        auto_announce = bool(ui_announce)

    # Announce-interval resolution: same precedence as auto_announce —
    # UI > env > default. Clamped to the SiteServer's min/max range.
    from .site_server import (
        DEFAULT_ANNOUNCE_INTERVAL, MIN_ANNOUNCE_INTERVAL,
        MAX_ANNOUNCE_INTERVAL,
    )
    ui_interval = ui_all.get("announce_interval")
    if ui_interval is None:
        try:
            announce_interval = int(cfg.get(
                "SITE_ANNOUNCE_INTERVAL", DEFAULT_ANNOUNCE_INTERVAL))
        except (TypeError, ValueError):
            announce_interval = DEFAULT_ANNOUNCE_INTERVAL
    else:
        announce_interval = int(ui_interval)
    announce_interval = max(MIN_ANNOUNCE_INTERVAL,
                            min(MAX_ANNOUNCE_INTERVAL, announce_interval))

    # SiteServer object is constructed synchronously (its __init__ only
    # stores config), but .start() has to wait for RNS since it registers
    # a Destination on the running Transport. Same for the cookie-name
    # suffix that reads site_server.node_hash().
    if not hosting_enabled:
        log.info("Site hosting disabled (UI/env config)")
        app.config["SITE_SERVER"] = None
    elif os.path.isdir(pages_dir):
        from .site_server import SiteServer
        identity_file = os.path.join(rns_dir, "site_identity.id")
        saved_name    = ui_all.get("site_name", "")
        # node_name=None → SiteServer auto-generates "NomadPortal-<2 hex>"
        # from the destination hash so co-located instances stay distinct.
        site_server   = SiteServer(
            pages_dir=pages_dir,
            files_dir=files_dir,
            identity_file=identity_file,
            node_name=saved_name or cfg.get("SITE_NAME") or None,
            auto_announce=auto_announce,
            announce_interval=announce_interval,
        )
        # Publish the object early so admin routes / dashboard can
        # reference it before start() completes (they'll show "site
        # starting" until node_hash() is populated).
        app.config["SITE_SERVER"] = site_server

        def _start_site_server():
            site_server.start()
            browser._hosted_hash = site_server.node_hash() or ""
            browser._hosted_name = site_server.node_name() or ""
            log.info("Site server active at hash %s", site_server.node_hash()[:16])
            # Now that the hosted node hash is known, retroactively set the
            # session-cookie-name suffix (see the comment near the Flask
            # session interface init below for why this matters).
            node_hash = site_server.node_hash() or ""
            if len(node_hash) >= 4:
                app.config["SESSION_COOKIE_NAME"] = f"session_{node_hash[-4:]}"
        _defer_after_rns("Site server start", _start_site_server)
    else:
        app.config["SITE_SERVER"] = None

    # Reverse proxy trust — must come before any request handling
    trusted_proxies = cfg.get("TRUSTED_PROXIES", 0)
    if trusted_proxies > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trusted_proxies,
            x_proto=1,
            x_host=1,
        )

    # Request size cap — prevents oversized JSON/form bodies
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024  # 512 KB

    https_mode = cfg.get("HTTPS_REDIRECT", False)

    # Match the Secure flag to the actual request scheme. Browsers silently
    # drop Secure cookies from HTTP responses, which would break sessions
    # (and therefore login + CSRF) on plain-HTTP deployments. request.is_secure
    # already respects ProxyFix's X-Forwarded-Proto, so HTTPS-anywhere-in-the-
    # chain deployments still get the Secure flag.
    class _RequestAwareSessionInterface(SecureCookieSessionInterface):
        def get_cookie_secure(self, app):
            return has_request_context() and request.is_secure

    app.session_interface = _RequestAwareSessionInterface()
    app.config["SESSION_COOKIE_HTTPONLY"]    = True
    app.config["SESSION_COOKIE_SAMESITE"]    = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(hours=8)

    # Suffix the session cookie with the last 4 chars of the hosted node hash
    # so co-located NomadPortal instances on the same browser host (different
    # ports) don't clobber each other's sessions. The suffix requires
    # ``site_server.node_hash()``, which is only populated once site_server
    # starts (deferred until RNS is ready). ``_start_site_server`` above
    # updates ``SESSION_COOKIE_NAME`` when that finishes. During the
    # startup window the default ``session`` name is in effect —
    # co-located operators may see a one-time logout when RNS finishes
    # coming up, but that's rare enough and gets self-corrected on the
    # next login. Single-container operators (the common case) are
    # unaffected.

    # Attach log buffer to root logger
    log_buffer.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(log_buffer)

    app.jinja_env.filters["enumerate"] = lambda it: enumerate(it)
    app.jinja_env.filters["strftime"] = lambda ts: (
        datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"
    )

    # CSRF token generation on every request + Jinja2 global
    csrf_mod.init_csrf(app)

    app.register_blueprint(bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

    init_auth(app)

    # Security response headers on every reply
    @app.after_request
    def _security_headers(response):
        response.headers["X-Frame-Options"]           = "DENY"
        response.headers["X-Content-Type-Options"]    = "nosniff"
        response.headers["Referrer-Policy"]            = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]        = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )
        response.headers["Content-Security-Policy"]   = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "  # inline style= attrs used throughout
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "                # no <object>/<embed>/<applet>
            "base-uri 'self'; "                  # no <base href> hijack
            "form-action 'self'; "               # forms can only POST to us
            "frame-ancestors 'none';"            # clickjacking protection
        )
        if https_mode:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    # HTTPS redirect (only meaningful when TRUSTED_PROXIES≥1).
    # When HTTPS_REDIRECT is on, TRUSTED_HOSTS is *required* — without a
    # configured allow-list there is no value we can safely splice into
    # the redirect target. Refusing to install the handler in that case
    # is both the right security posture (no open-redirect surface) and
    # what CodeQL's ``py/url-redirection`` rule needs to verify
    # statically: the redirect target is chosen from a config-derived
    # set, not from the request Host header.
    if https_mode:
        trusted_hosts_raw = (cfg.get("TRUSTED_HOSTS") or "").strip()
        trusted_hosts = tuple(sorted({
            h.strip().lower() for h in trusted_hosts_raw.split(",") if h.strip()
        }))
        if not trusted_hosts:
            log.warning(
                "HTTPS_REDIRECT=true requires TRUSTED_HOSTS to be set "
                "(comma-separated public hostnames). Without it there is "
                "no safe redirect target; HTTPS upgrade handler disabled. "
                "Set TRUSTED_HOSTS=your.public.host and restart."
            )
        else:
            from urllib.parse import urlsplit, urlunsplit
            # First trusted host is the canonical redirect target. With
            # multiple TRUSTED_HOSTS configured, all HTTP requests
            # upgrade to https://{first}/... — operators with several
            # virtual hosts on the same NomadPortal pick the canonical
            # one as the first entry.
            canonical_host = trusted_hosts[0]

            @app.before_request
            def _https_redirect():
                if request.is_secure:
                    return None
                # Verify the inbound Host is one we serve — gates the
                # redirect entirely on the trusted-hosts allow-list
                # (forged Host gets 400, not a redirect).
                request_host = (request.host or "").lower()
                if request_host not in trusted_hosts:
                    return abort(400)
                # urlsplit + urlunsplit is the recognised sanitisation
                # barrier for py/url-redirection. The redirect target
                # is rebuilt with:
                #   - scheme: hardcoded "https" string literal
                #   - netloc: ``canonical_host`` from config
                #   - path/query: parsed off ``request.url`` and passed
                #     through urlsplit (recognised sanitiser)
                # The fragment is dropped — a 301 response can't
                # preserve client-side fragments anyway.
                parts = urlsplit(request.url)
                safe_url = urlunsplit(
                    ("https", canonical_host, parts.path, parts.query, ""),
                )
                return redirect(safe_url, 301)

    # Warn when OIDC is enabled with no admin configured anywhere
    if cfg.get("OIDC_CLIENT_ID") and not cfg.get("OIDC_ADMIN_EMAILS") \
       and not cfg.get("OIDC_ADMIN_SUBJECTS") and not cfg.get("ADMIN_PASSWORD"):
        log.warning(
            "OIDC is enabled but no admin path is configured. "
            "OIDC users will log in as non-admins and there is no local admin "
            "recovery account. Set OIDC_ADMIN_EMAILS / OIDC_ADMIN_SUBJECTS or "
            "ADMIN_PASSWORD to grant admin access."
        )

    # robots.txt
    @app.route("/robots.txt")
    def _robots():
        return send_from_directory(app.static_folder, "robots.txt")

    # Custom error pages
    @app.errorhandler(404)
    def _err404(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def _err500(e):
        return render_template("errors/500.html"), 500

    # Fire the deferred RNS-dependent init in a background thread. Waits for
    # the browser's own RNS init to complete (up to 10 min — pathological
    # RNS storage / hub-timeouts can take a while), then runs each queued
    # step in registration order. Any step that raises is logged and
    # skipped so a downstream failure doesn't block the others.
    def _run_deferred_rns_init():
        if not browser.wait_ready(timeout=600):
            log.error(
                "RNS did not become ready within 10 minutes — deferred "
                "startup (LXMF, site server, etc) skipped. RNS-dependent "
                "endpoints will keep returning 503 until the process is "
                "restarted."
            )
            return
        for name, fn in _deferred_rns_actions:
            try:
                fn()
            except Exception as exc:
                log.warning("Deferred RNS setup step '%s' failed: %s", name, exc)
        log.info("NomadPortal deferred RNS-dependent setup complete")

    threading.Thread(
        target=_run_deferred_rns_init,
        daemon=True,
        name="rns-deferred-init",
    ).start()

    return app

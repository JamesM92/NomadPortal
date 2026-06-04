import logging
import os
import time
import datetime
from flask import Flask, abort, has_request_context, redirect, render_template, request, send_from_directory
from flask.sessions import SecureCookieSessionInterface

# Bumped per release. Logged at startup so the running image's version is
# visible in `docker logs` without needing `docker inspect`.
__version__ = "0.9.22"
from .routes import bp
from .cache import PageCache
from .browser import NodeBrowser
from .auth import auth_bp, init_auth
from .admin_routes import admin_bp
from .identity_store import IdentityStore
from .messaging import MessagingService
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
            entry = app.config["IDENTITY_STORE"].ensure_for_user(
                admin_sub, admin_user,
            )
            # admin_sub is constructed inline (``f"local:{admin_user}"``)
            # but flows from the operator-controlled ADMIN_USERNAME env
            # var; CodeQL flags it via the identity-correlation
            # heuristic. Scrub through .replace so the dataflow exits
            # the sink with a recognised barrier.
            log.info(
                "Local admin LXMF identity ready (sub=%s) — "
                "messages received whenever the container is running.",
                admin_sub.replace("\r", "").replace("\n", ""),
            )
        except Exception as exc:
            log.warning("Could not pre-create local admin identity: %s", exc)

    try:
        messaging.setup_delivery(app.config["IDENTITY_STORE"])
    except Exception as exc:
        log.warning("LXMF delivery setup failed: %s", exc)

    lxmf_tracker = LXMFPeerTracker(config_dir)
    app.config["LXMF_TRACKER"] = lxmf_tracker
    try:
        import RNS
        RNS.Transport.register_announce_handler(lxmf_tracker.register_announce_handler())
        log.info("LXMF peer tracker registered")
    except Exception as exc:
        log.warning("LXMF peer tracker registration failed: %s", exc)

    users_yml = cfg.get("USERS_YML", "/config/users.yml")
    app.config["USER_STORE"]  = UserStore(users_yml)
    app.config["UI_SETTINGS"] = UISettings(config_dir)   # must be before site server

    # Site hosting — start node server unless explicitly disabled. Operators
    # running NomadPortal as a pure browser (no hosted content, minimal
    # network footprint) set SITE_HOSTING=false.
    pages_dir = cfg.get("SITE_PAGES_DIR", "/site/pages")
    files_dir = cfg.get("SITE_FILES_DIR", "/site/files")
    hosting_raw = str(cfg.get("SITE_HOSTING", "true")).strip().lower()
    hosting_enabled = hosting_raw not in ("0", "false", "no", "off", "")
    if not hosting_enabled:
        log.info("Site hosting disabled via SITE_HOSTING env var")
        app.config["SITE_SERVER"] = None
    elif os.path.isdir(pages_dir):
        from .site_server import SiteServer
        identity_file = os.path.join(rns_dir, "site_identity.id")
        saved_name    = app.config["UI_SETTINGS"].get_all().get("site_name", "")
        # node_name=None → SiteServer auto-generates "NomadPortal-<2 hex>"
        # from the destination hash so co-located instances stay distinct.
        # auto_announce=False by default — vanilla NomadPortal installs are
        # silent hosts. Operators who actually publish a site flip
        # SITE_ANNOUNCE=true (or use the saved UI setting once that lands).
        announce_raw = str(cfg.get("SITE_ANNOUNCE", "false")).strip().lower()
        auto_announce = announce_raw in ("1", "true", "yes", "on")
        site_server   = SiteServer(
            pages_dir=pages_dir,
            files_dir=files_dir,
            identity_file=identity_file,
            node_name=saved_name or cfg.get("SITE_NAME") or None,
            auto_announce=auto_announce,
        )
        try:
            site_server.start()
            app.config["SITE_SERVER"] = site_server
            browser._hosted_hash = site_server.node_hash() or ""
            browser._hosted_name = site_server.node_name() or ""
            log.info("Site server active at hash %s", site_server.node_hash()[:16])
        except Exception as exc:
            log.warning("Site server failed to start: %s", exc)
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
            @app.before_request
            def _https_redirect():
                if request.is_secure:
                    return None
                # Resolve the incoming Host to a value chosen *from* the
                # trusted-hosts tuple. ``trusted_target`` is None when
                # the request's Host doesn't match any allowed entry
                # (forged header, dev hostname, etc.) — those get a 400
                # rather than a redirect.
                request_host = (request.host or "").lower()
                trusted_target = None
                for allowed in trusted_hosts:
                    if allowed == request_host:
                        trusted_target = allowed
                        break
                if trusted_target is None:
                    return abort(400)
                # Validate the request path (no CR/LF) before splicing.
                tail = request.full_path
                if "\r" in tail or "\n" in tail:
                    return abort(400)
                if tail.endswith("?"):
                    tail = tail[:-1]
                # ``trusted_target`` is the config-supplied host; the
                # redirect() target never echoes raw Host header content.
                return redirect(f"https://{trusted_target}{tail}", 301)

    # Warn when OIDC is enabled with no admin configured anywhere
    if cfg.get("OIDC_CLIENT_ID") and not cfg.get("OIDC_ADMIN_EMAILS") \
       and not cfg.get("OIDC_ADMIN_SUBJECTS") and not cfg.get("ADMIN_PASSWORD"):
        log.warning(
            "OIDC is enabled but no admin path is configured. "
            "OIDC users will log in as non-admins and there is no local admin "
            "recovery account. Set OIDC_ADMIN_EMAILS / OIDC_ADMIN_SUBJECTS or "
            "ADMIN_PASSWORD to grant admin access."
        )

    # Warn that this is a single-operator tool
    log.info(
        "NomadPortal is a single-operator tool. All logged-in users share "
        "the same identity, message, and contact stores."
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

    return app

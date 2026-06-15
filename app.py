"""
NomadPortal — entry point.

Environment variables:
    RNS_CONFIG_DIR         Path to Reticulum config directory  (default /config/reticulum)
    CONFIG_YML             Path to config.yml                  (default /config/config.yml)
    WEB_HOST               Host for the dev server only        (default 0.0.0.0)
    WEB_PORT               Port for the dev server only        (default 8080)
    CACHE_TTL              Page cache TTL in seconds           (default 300)
    LOG_LEVEL              Python logging level                (default INFO)

    FLASK_SECRET_KEY       Session signing key — auto-generated and saved to
                           /config/secret.key if not set.  Set this explicitly
                           in production so sessions survive container restarts.

    TRUSTED_PROXIES        Integer — number of upstream proxy hops to trust for
                           X-Forwarded-For.  Set to 1 when behind nginx/Caddy.
                           (default 0 — direct exposure, use remote_addr as-is)

    HTTPS_REDIRECT         "true" to redirect HTTP → HTTPS (needs TRUSTED_PROXIES≥1)

    ADMIN_USERNAME         Local admin username    (default admin)
    ADMIN_PASSWORD         Local admin password    (no default — local login disabled if unset)

    OIDC_CLIENT_ID         OIDC client ID
    OIDC_CLIENT_SECRET     OIDC client secret
    OIDC_DISCOVERY_URL     Provider discovery URL
    OIDC_ALLOWED_EMAILS    Comma-separated email allowlist (empty = any authenticated user)
    OIDC_ALLOWED_SUBJECTS  Comma-separated sub allowlist
    OIDC_ADMIN_EMAILS      Comma-separated admin email list
    OIDC_ADMIN_SUBJECTS    Comma-separated admin sub list (users on neither
                           list are standard users; use the local
                           ADMIN_PASSWORD as the bootstrap admin).

Production (Gunicorn — recommended):
    gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:8080 'app:create_wsgi()'

    Use -w 1 (single worker) to prevent multiple RNS instances.
    --threads 8 provides concurrency via gthread worker class.
    --timeout must be > SEND_WAIT (30 s) — 120 s gives comfortable headroom.
"""

import logging
import os
import re
import secrets
import sys

from flask import render_template

from nomadnet_web import create_app
from nomadnet_web.browser import NodeBrowser
from nomadnet_web.cache import PageCache
from nomadnet_web.config_gen import generate

_log = logging.getLogger(__name__)

# Gunicorn calls create_wsgi() once per worker; _app caches the result so
# repeated module imports during preload don't re-initialise RNS.
_app = None


def _log_versions() -> None:
    """Log NomadPortal + key dependency versions at startup.

    Lets `docker logs` confirm which image is running without `docker
    inspect` — particularly useful when bouncing between :latest and :dev
    to compare behaviour.
    """
    from nomadnet_web import __version__ as nomadportal_version
    try:
        import importlib.metadata as _md
        m2h = _md.version("Micron2HTML")
    except Exception:
        m2h = "unknown"
    try:
        import rns
        rns_version = getattr(rns, "__version__", "unknown")
    except Exception:
        rns_version = "unknown"
    _log.info(
        "NomadPortal v%s starting (Micron2HTML %s, RNS %s)",
        nomadportal_version, m2h, rns_version,
    )


def _setup_logging() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    # ── Log-injection guard ───────────────────────────────────────────
    # Strip CR/LF and other ASCII control characters from every record
    # before formatting. Even when callers do
    # ``log.info("user %s did X", user_supplied)`` and ``user_supplied``
    # contains forged newlines, the rendered line that hits stdout (or
    # any other handler) can no longer break out into a forged log
    # entry. CodeQL's py/log-injection rule treats this CR/LF strip as
    # a sanitisation barrier — applying it at the root logger covers
    # every existing call site without changes to individual callers.
    _CTRL_RE = re.compile(r"[\r\n\x00-\x08\x0b-\x1f\x7f]")
    def _scrub(value):
        if isinstance(value, str):
            return _CTRL_RE.sub("?", value)
        return value

    class _StripCRLFFilter(logging.Filter):
        def filter(self, record):
            if isinstance(record.msg, str):
                record.msg = _CTRL_RE.sub("?", record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_scrub(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _scrub(v) for k, v in record.args.items()}
            return True

    logging.getLogger().addFilter(_StripCRLFFilter())

    # Suppress noisy poll/healthcheck access lines from gunicorn's access
    # log. The front-end polls `/api/page/poll` every 500ms while a page
    # fetch is in flight and the healthcheck hits `/api/status` every
    # 30s; both flood `docker logs` and bury everything else. Other
    # `/api/...` traffic still logs normally, including poll *errors*
    # (which come through a different logger).
    class _NoisyEndpointFilter(logging.Filter):
        _NOISY = ("/api/page/poll", "/api/status")
        def filter(self, record):
            msg = record.getMessage()
            return not any(p in msg for p in self._NOISY)

    logging.getLogger("gunicorn.access").addFilter(_NoisyEndpointFilter())


def _get_or_create_secret_key(config_dir: str) -> str:
    key_file = os.path.join(config_dir, "secret.key")
    env_key  = os.environ.get("FLASK_SECRET_KEY", "")
    if env_key:
        return env_key
    if os.path.exists(key_file):
        with open(key_file) as fh:
            return fh.read().strip()
    key = secrets.token_hex(48)
    os.makedirs(config_dir, exist_ok=True)
    with open(key_file, "w") as fh:
        fh.write(key)
    _log.info("Generated new Flask secret key at %s", key_file)
    return key


def _csv_env(name: str) -> list:
    raw = os.environ.get(name, "")
    return [v.strip() for v in raw.split(",") if v.strip()]


def create_wsgi():
    """WSGI application factory used by Gunicorn and tests."""
    global _app
    if _app is not None:
        return _app

    _setup_logging()
    _log_versions()

    config_dir = os.environ.get("RNS_CONFIG_DIR", "/config/reticulum")
    config_yml = os.environ.get("CONFIG_YML",     "/config/config.yml")
    cache_ttl  = int(os.environ.get("CACHE_TTL",  "300"))

    generate(config_yml, os.path.join(config_dir, "config"))

    browser = NodeBrowser(config_dir=config_dir)
    cache   = PageCache(default_ttl=cache_ttl)

    flask_config = {
        "SECRET_KEY":             _get_or_create_secret_key(config_dir),
        "DEBUG":                  False,   # never enable in production
        # Hard cap on request body size — protects against DoS via huge
        # uploads. Page-fetch field data has its own tighter per-call cap
        # in routes._validate_field_data.
        "MAX_CONTENT_LENGTH":     1 * 1024 * 1024,   # 1 MiB
        "RNS_CONFIG_DIR":         config_dir,
        "CONFIG_DIR":             os.path.dirname(config_dir),   # /config
        "CONFIG_YML":             config_yml,
        "USERS_YML":              os.environ.get("USERS_YML", "/config/users.yml"),
        "CACHE_TTL":              cache_ttl,
        "TRUSTED_PROXIES":        int(os.environ.get("TRUSTED_PROXIES", "0")),
        "HTTPS_REDIRECT":         os.environ.get("HTTPS_REDIRECT", "").lower()
                                  in ("1", "true", "yes"),
        # Comma-separated host allow-list applied to the HTTPS_REDIRECT
        # before_request handler. Set to your public hostname(s) — e.g.
        # "nomad.example.com,nomad.example.org" — so a forged Host
        # header gets a 400 instead of a redirect.
        "TRUSTED_HOSTS":          os.environ.get("TRUSTED_HOSTS", ""),
        # Local admin credentials
        "ADMIN_USERNAME":         os.environ.get("ADMIN_USERNAME", "admin"),
        "ADMIN_PASSWORD":         os.environ.get("ADMIN_PASSWORD", ""),
        # OIDC
        "OIDC_CLIENT_ID":         os.environ.get("OIDC_CLIENT_ID",     ""),
        "OIDC_CLIENT_SECRET":     os.environ.get("OIDC_CLIENT_SECRET", ""),
        "OIDC_DISCOVERY_URL":     os.environ.get("OIDC_DISCOVERY_URL", ""),
        "OIDC_ALLOWED_EMAILS":    _csv_env("OIDC_ALLOWED_EMAILS"),
        "OIDC_ALLOWED_SUBJECTS":  _csv_env("OIDC_ALLOWED_SUBJECTS"),
        "OIDC_ADMIN_EMAILS":      _csv_env("OIDC_ADMIN_EMAILS"),
        "OIDC_ADMIN_SUBJECTS":    _csv_env("OIDC_ADMIN_SUBJECTS"),
        "OIDC_INSECURE_SKIP_VERIFY": os.environ.get(
            "OIDC_INSECURE_SKIP_VERIFY", "").lower() in ("1", "true", "yes"),
        # Site hosting
        "SITE_PAGES_DIR":         os.environ.get("SITE_PAGES_DIR", "/site/pages"),
        "SITE_FILES_DIR":         os.environ.get("SITE_FILES_DIR", "/site/files"),
        # SITE_NAME left blank by default so SiteServer auto-generates
        # NomadPortal-<2 hex> from the destination hash (avoids 20
        # vanilla installs all colliding under a single "NomadPortal"
        # name). Set to a custom string to override.
        "SITE_NAME":              os.environ.get("SITE_NAME", ""),
        "SITE_HOSTING":           os.environ.get("SITE_HOSTING", "true"),
        "SITE_ANNOUNCE":          os.environ.get("SITE_ANNOUNCE", "false"),
        # Auto-announce frequency in seconds — falls through to the
        # site_server module's DEFAULT_ANNOUNCE_INTERVAL (6h) when unset.
        # Admin → Settings can override per-instance without restart.
        "SITE_ANNOUNCE_INTERVAL": os.environ.get(
            "SITE_ANNOUNCE_INTERVAL", ""),
        # Virus scanning for file downloads — off by default. See
        # nomadnet_web/scanner.py for accepted values.
        "VIRUS_SCAN":             os.environ.get("VIRUS_SCAN", "off"),
        "CLAMD_SOCKET":           os.environ.get("CLAMD_SOCKET", ""),
        "CLAMD_HOST":             os.environ.get("CLAMD_HOST",   ""),
        "CLAMD_PORT":             int(os.environ.get("CLAMD_PORT", "3310")),
        "VIRUS_SCAN_MAX_BYTES":   int(os.environ.get(
            "VIRUS_SCAN_MAX_BYTES", str(100 * 1024 * 1024),
        )),
        # Set ALLOW_GUEST_EXTERNAL_BROWSE=true to suppress the per-navigation
        # content warning shown to unauthenticated users when they follow links
        # to nodes other than the hosted site.  The warning exists to discourage
        # operators from exposing the unmoderated NomadNet network to anonymous
        # visitors; only set this if you have a specific reason to do so.
        "ALLOW_GUEST_EXTERNAL_BROWSE": os.environ.get(
            "ALLOW_GUEST_EXTERNAL_BROWSE", ""
        ).lower() in ("1", "true", "yes"),
    }

    app = create_app(browser, cache, config=flask_config)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/page")
    def page_view():
        return render_template("index.html")

    @app.get("/robots.txt")
    def robots():
        return (
            "User-agent: *\nDisallow: /\n",
            200,
            {"Content-Type": "text/plain"},
        )

    if not flask_config["ADMIN_PASSWORD"]:
        _log.warning("ADMIN_PASSWORD is not set — local login is disabled.")
    elif flask_config["ADMIN_PASSWORD"] in ("changeme", "admin", "password"):
        _log.warning(
            "ADMIN_PASSWORD is set to a default value — change it before "
            "exposing this service to a network."
        )
    if not flask_config["OIDC_CLIENT_ID"]:
        _log.info("OIDC not configured — only local login is available.")
    if flask_config["TRUSTED_PROXIES"] == 0:
        _log.info(
            "TRUSTED_PROXIES=0: client IPs taken from remote_addr directly. "
            "Set TRUSTED_PROXIES=1 if behind a reverse proxy."
        )

    _app = app
    return app


def main() -> None:
    """Development server. Use Gunicorn for production."""
    app      = create_wsgi()
    web_host = os.environ.get("WEB_HOST", "0.0.0.0")
    web_port = int(os.environ.get("WEB_PORT", "8080"))
    _log.warning(
        "Running on Flask development server — use Gunicorn for production: "
        "gunicorn -w 1 --threads 8 --timeout 120 -b %s:%d 'app:create_wsgi()'",
        web_host, web_port,
    )
    app.run(host=web_host, port=web_port, threaded=True, debug=False)


if __name__ == "__main__":
    main()

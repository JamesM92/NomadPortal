"""
OIDC authentication via Authlib + role-based access.

Roles:
    anonymous  — can browse nodes and pages only
    user       — logged-in; can fingerprint and send messages
    admin      — logged-in + in OIDC_ADMIN_EMAILS/OIDC_ADMIN_SUBJECTS; can edit config

Required env vars:
    OIDC_CLIENT_ID
    OIDC_CLIENT_SECRET
    OIDC_DISCOVERY_URL

Optional:
    OIDC_ALLOWED_EMAILS    comma-separated — restricts who can log in at all
    OIDC_ALLOWED_SUBJECTS  comma-separated sub claims — same purpose
    OIDC_ADMIN_EMAILS      comma-separated — these users get admin role
    OIDC_ADMIN_SUBJECTS    comma-separated sub claims — same purpose for admins
                           Users not on either list are treated as standard
                           users; the local ADMIN_PASSWORD account is the
                           recovery path when no OIDC admins are configured.
"""

import logging
import time
from functools import wraps

from flask import (
    Blueprint, abort, current_app, jsonify, redirect, render_template,
    request, session, url_for,
)
from flask_login import (
    LoginManager, UserMixin, current_user,
    login_user, logout_user, user_logged_in,
)
from authlib.integrations.flask_client import OAuth

from . import rate_limit, csrf as csrf_mod
from .routes import _render_title_html

log = logging.getLogger(__name__)

login_manager = LoginManager()
oauth = OAuth()

SESSION_TTL       = 8 * 3600   # seconds before a cached session expires
_LOGIN_MAX        = 10          # max failed attempts before lockout
_LOGIN_WINDOW     = 300         # seconds window for lockout

# sub -> {"user": OIDCUser, "login_at": ts, "last_seen": ts, "login_ip": str, "last_ip": str}
_user_cache: dict = {}

# (ip, username) -> [timestamp, ...]
_failed_attempts: dict = {}


def _safe_next_or_default(candidate: str, default: str) -> str:
    """Return ``candidate`` only when it's a same-origin relative URL.

    Protects the post-login redirect against the classic open-redirect
    attack — ``/login?next=https://evil.com`` shouldn't punt the user
    off-domain after a successful login.

    Implementation: parse the candidate with ``urlsplit``, ensure it
    has no scheme or netloc, then reconstruct via ``urlunsplit`` with
    those fields forced to empty strings. CodeQL recognises this
    ``urlsplit + reset + urlunsplit`` pattern as a url-redirection
    sanitiser, so the value returned can flow into ``redirect()``
    without tripping the py/url-redirection rule.

    Any deviation falls back to ``default`` (typically the dashboard).
    """
    from urllib.parse import urlsplit, urlunsplit
    if not isinstance(candidate, str) or not candidate:
        return default
    if "\r" in candidate or "\n" in candidate:
        return default
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return default
    # Reject anything with a scheme or netloc — those are the open-redirect
    # vectors. Also reject paths that aren't absolute-on-the-current-host.
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return default
    # Reconstruct with empty scheme/netloc to neutralise any user-supplied
    # host. ``urlunsplit("", "", path, query, fragment)`` returns a clean
    # ``/path?query#fragment`` string.
    return urlunsplit(("", "", parts.path, parts.query, parts.fragment))


def _record_session(user, ip: str) -> None:
    """Insert or refresh a session entry with login metadata."""
    now = time.time()
    _user_cache[user.id] = {
        "user":      user,
        "login_at":  now,
        "last_seen": now,
        "login_ip":  ip or "unknown",
        "last_ip":   ip or "unknown",
    }


def touch_session(sub: str, ip: str) -> None:
    """Update last_seen / last_ip for an existing session."""
    entry = _user_cache.get(sub)
    if entry is not None:
        entry["last_seen"] = time.time()
        if ip:
            entry["last_ip"] = ip


def list_sessions() -> list:
    """Snapshot of active sessions for the admin UI."""
    out = []
    now = time.time()
    for sub, entry in list(_user_cache.items()):
        if now - entry["login_at"] > SESSION_TTL:
            continue
        user = entry["user"]
        out.append({
            "sub":         sub,
            "name":        getattr(user, "name", ""),
            "email":       getattr(user, "email", ""),
            "is_admin":    getattr(user, "is_admin", False),
            "super_admin": getattr(user, "super_admin", False),
            "login_at":    entry["login_at"],
            "last_seen":   entry["last_seen"],
            "login_ip":    entry["login_ip"],
            "last_ip":     entry["last_ip"],
        })
    out.sort(key=lambda r: -r["last_seen"])
    return out


def revoke_session(sub: str) -> bool:
    """Drop a single session by sub. Returns True if it existed."""
    return _user_cache.pop(sub, None) is not None


def _record_failure(ip: str, username: str) -> None:
    key = (ip, username)
    now = time.time()
    attempts = [t for t in _failed_attempts.get(key, []) if now - t < _LOGIN_WINDOW]
    attempts.append(now)
    _failed_attempts[key] = attempts


def _is_locked_out(ip: str, username: str) -> bool:
    key = (ip, username)
    now = time.time()
    attempts = [t for t in _failed_attempts.get(key, []) if now - t < _LOGIN_WINDOW]
    _failed_attempts[key] = attempts
    return len(attempts) >= _LOGIN_MAX


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------

class OIDCUser(UserMixin):
    def __init__(self, sub: str, email: str = "", name: str = "",
                 claims: dict = None, is_admin: bool = False,
                 super_admin: bool = False):
        self.id          = sub
        self.email       = email
        self.name        = name or email
        self.claims      = claims or {}
        self.is_admin    = is_admin
        # Super admin = only the env-var ADMIN_PASSWORD login. Implies is_admin.
        # Used to gate edits to the admin column in Settings; this is the one
        # account that is configured outside the running app and therefore
        # can't be modified from within it.
        self.super_admin = super_admin or False


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def admin_required(f):
    """Require the user to be logged in AND have the admin role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not getattr(current_user, "is_admin", False):
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_auth(app) -> None:
    login_manager.login_view = "auth.login_page"
    login_manager.login_message = ""

    @login_manager.unauthorized_handler
    def _unauthorized():
        best = request.accept_mimetypes.best_match(
            ['application/json', 'text/html']
        )
        if best == 'application/json':
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for('auth.login_page', next=request.url))

    login_manager.init_app(app)
    oauth.init_app(app)
    oauth.register(
        name="oidc",
        client_id=app.config["OIDC_CLIENT_ID"],
        client_secret=app.config["OIDC_CLIENT_SECRET"],
        server_metadata_url=app.config["OIDC_DISCOVERY_URL"],
        client_kwargs={"scope": "openid email profile"},
    )

    if app.config.get("OIDC_INSECURE_SKIP_VERIFY"):
        # Self-signed Authentik / local-LAN setups: disable TLS verification,
        # but ONLY for the OIDC provider's host. Different Authlib versions
        # reach discovery / token / userinfo through different session
        # factories — patching `requests.Session.send` lets us intercept
        # every outbound call uniformly, then we filter by hostname so any
        # other HTTPS call NomadPortal makes still verifies normally.
        import warnings
        from urllib.parse import urlparse
        try:
            from urllib3.exceptions import InsecureRequestWarning
            warnings.filterwarnings("ignore", category=InsecureRequestWarning)
        except Exception:
            pass

        oidc_host = urlparse(app.config["OIDC_DISCOVERY_URL"]).hostname or ""
        if oidc_host:
            import requests
            _orig_send = requests.Session.send

            def _scoped_send(self, request, **kwargs):
                try:
                    host = urlparse(request.url).hostname
                except Exception:
                    host = None
                if host == oidc_host:
                    kwargs["verify"] = False
                return _orig_send(self, request, **kwargs)

            requests.Session.send = _scoped_send
            log.warning(
                "OIDC TLS verification DISABLED for host %s only "
                "(OIDC_INSECURE_SKIP_VERIFY=true). Other HTTPS calls "
                "still verify certificates normally.",
                oidc_host,
            )
        else:
            log.warning(
                "OIDC_INSECURE_SKIP_VERIFY is set but OIDC_DISCOVERY_URL "
                "has no hostname — TLS verification still active."
            )

    log.info("OIDC auth configured (%s)", app.config["OIDC_DISCOVERY_URL"])

    # Reset the per-user fingerprint identify-on-fetch list at every login
    # so the address-bar toggle always defaults to OFF for a fresh session.
    @user_logged_in.connect_via(app)
    def _reset_identified_nodes(sender, user, **extra):
        id_store = app.config.get("IDENTITY_STORE")
        if id_store is None or user is None:
            return
        entry = id_store.get_for_user(getattr(user, "id", "") or "")
        if entry:
            id_store.clear_identified_nodes(entry["id"])


@login_manager.user_loader
def load_user(user_id: str):
    entry = _user_cache.get(user_id)
    if entry is None:
        return None
    if time.time() - entry["login_at"] > SESSION_TTL:
        _user_cache.pop(user_id, None)
        return None
    # Refresh activity timestamp / source IP on every authenticated request.
    entry["last_seen"] = time.time()
    ip = request.remote_addr if request else None
    if ip:
        entry["last_ip"] = ip
    return entry["user"]


def revoke_all_sessions() -> int:
    """Clear all in-memory sessions. Returns number revoked."""
    count = len(_user_cache)
    _user_cache.clear()
    return count


def _is_admin(email: str, sub: str) -> bool:
    """Resolve admin status for an OIDC user.

    Precedence (first match wins):
        1. Per-user is_admin flag set via the admin UI (UserStore).
           Overrides env-var allowlists in both directions — a user
           explicitly demoted in the store stays demoted even if
           later added to OIDC_ADMIN_EMAILS.
        2. OIDC_ADMIN_EMAILS / OIDC_ADMIN_SUBJECTS env-var allowlist.
        3. Fallback: not admin.

    The local ADMIN_PASSWORD account is always admin and is handled
    separately in local_login(); it is the recovery path when no OIDC
    admins have been configured yet.
    """
    user_store = current_app.config.get("USER_STORE")
    if user_store:
        record = user_store.get_user(sub)
        if record is not None and record.get("is_admin") is not None:
            return bool(record["is_admin"])

    admin_emails   = current_app.config.get("OIDC_ADMIN_EMAILS",   [])
    admin_subjects = current_app.config.get("OIDC_ADMIN_SUBJECTS", [])
    return email in admin_emails or sub in admin_subjects


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.context_processor
def _ui_context():
    from flask import current_app
    ui = current_app.config.get("UI_SETTINGS")
    raw = ui.get_all().get("app_title", "`F4af■ NomadPortal`f") if ui else "`F4af■ NomadPortal`f"
    return {"app_title_html": _render_title_html(raw)}


@auth_bp.before_request
def _auth_csrf():
    if request.endpoint == "auth.local_login":
        csrf_mod.check()


@auth_bp.get("/login")
def login_page():
    oidc_enabled = bool(current_app.config.get("OIDC_CLIENT_ID"))
    return render_template("admin/login.html", oidc_enabled=oidc_enabled)


@auth_bp.post("/local")
def local_login():
    """Authenticate with the local admin credentials set via ADMIN_PASSWORD."""
    import hmac as _hmac
    oidc_enabled = bool(current_app.config.get("OIDC_CLIENT_ID"))

    def _fail(error, status=401):
        return render_template("admin/login.html", oidc_enabled=oidc_enabled,
                               error=error), status

    ip       = request.remote_addr or "unknown"
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    # IP-level rate limit (20 attempts per 5 min regardless of username)
    if not rate_limit.check(f"login:{ip}", 20, 300):
        log.warning("Login rate limit exceeded from %s",
                    ip.replace("\r", "").replace("\n", ""))
        return _fail("Too many requests — try again later.", 429)

    # Per-(IP, username) lockout after repeated failures
    if _is_locked_out(ip, username):
        log.warning("Login locked out: ip=%s username=%s",
                    ip.replace("\r", "").replace("\n", ""),
                    username.replace("\r", "").replace("\n", ""))
        return _fail("Too many failed attempts — try again in a few minutes.", 429)

    expected_user = current_app.config.get("ADMIN_USERNAME", "admin")
    expected_pass = current_app.config.get("ADMIN_PASSWORD", "")

    if not expected_pass:
        return _fail("Local login is disabled (ADMIN_PASSWORD is not set).", 403)

    user_ok = _hmac.compare_digest(username.encode(), expected_user.encode())
    pass_ok = _hmac.compare_digest(password.encode(), expected_pass.encode())

    # Check admin-created local users in UserStore first
    user_store = current_app.config.get("USER_STORE")
    if user_store:
        record = user_store.authenticate_local(username, password)
        if record is not None:
            user = OIDCUser(
                sub=record["sub"], email="", name=record["name"],
                is_admin=record.get("is_admin", False),
            )
            _record_session(user, ip)
            login_user(user, remember=False)
            id_store = current_app.config.get("IDENTITY_STORE")
            if id_store:
                id_store.ensure_for_user(user.id, user.name)
            messaging = current_app.config.get("MESSAGING")
            if messaging:
                messaging.setup_user(user.id)
            user_store.register_or_update(record["sub"], "", record["name"])
            log.info("Local user login: %s from %s",
                     username.replace("\r", "").replace("\n", ""),
                     ip.replace("\r", "").replace("\n", ""))
            return redirect(url_for("admin.dashboard") if user.is_admin else "/")

    if user_ok and pass_ok:
        user = OIDCUser(sub=f"local:{username}", email="", name=username,
                        is_admin=True, super_admin=True)
        _record_session(user, ip)
        login_user(user, remember=False)
        id_store = current_app.config.get("IDENTITY_STORE")
        if id_store:
            id_store.ensure_for_user(user.id, user.name)
        messaging = current_app.config.get("MESSAGING")
        if messaging:
            messaging.setup_user(user.id)
        log.info("Local login: %s from %s",
                 username.replace("\r", "").replace("\n", ""),
                 ip.replace("\r", "").replace("\n", ""))
        return redirect(url_for("admin.dashboard") if user.is_admin else "/")

    _record_failure(ip, username)
    log.warning("Failed local login: username=%s ip=%s",
                username.replace("\r", "").replace("\n", ""),
                ip.replace("\r", "").replace("\n", ""))
    return _fail("Invalid username or password.")


@auth_bp.get("/start")
def login_start():
    """Redirect to the OIDC provider."""
    redirect_uri = url_for("auth.callback", _external=True)
    return oauth.oidc.authorize_redirect(redirect_uri)


@auth_bp.get("/callback")
def callback():
    try:
        token = oauth.oidc.authorize_access_token()
    except Exception as exc:
        log.warning("OIDC callback error: %s", exc)
        return render_template("admin/login.html",
                               error=f"Authentication failed: {exc}"), 401

    userinfo = token.get("userinfo") or {}
    sub   = userinfo.get("sub", "")
    email = userinfo.get("email", "")
    name  = userinfo.get("name", "") or userinfo.get("preferred_username", email)

    # Login allowlist
    allowed_emails   = current_app.config.get("OIDC_ALLOWED_EMAILS",   [])
    allowed_subjects = current_app.config.get("OIDC_ALLOWED_SUBJECTS", [])
    if allowed_emails and email not in allowed_emails:
        log.warning("Login denied (email not allowed): %s", email)
        return render_template("admin/login.html",
                               error="Access denied: your account is not authorised."), 403
    if allowed_subjects and sub not in allowed_subjects:
        log.warning("Login denied (sub not allowed): %s", sub)
        return render_template("admin/login.html",
                               error="Access denied: your account is not authorised."), 403

    # Register with user store (creates record on first login, updates on repeat)
    user_store = current_app.config.get("USER_STORE")
    if user_store:
        record, is_new = user_store.register_or_update(sub, email, name)
        if not record["enabled"]:
            log.warning("Login denied (account disabled): %s (%s)", name, email)
            return render_template(
                "admin/login.html",
                oidc_enabled=True,
                error="Your account has been disabled. Contact an administrator.",
            ), 403
        if is_new and not record["enabled"]:
            # New user, policy says disabled by default
            return render_template(
                "admin/login.html",
                oidc_enabled=True,
                error="Your account is pending approval. Contact an administrator.",
            ), 403

    user = OIDCUser(
        sub=sub, email=email, name=name,
        claims=dict(userinfo),
        is_admin=_is_admin(email, sub),
    )
    _record_session(user, request.remote_addr or "unknown")
    login_user(user, remember=False)
    id_store = current_app.config.get("IDENTITY_STORE")
    if id_store:
        id_store.ensure_for_user(user.id, user.name)
    messaging = current_app.config.get("MESSAGING")
    if messaging:
        messaging.setup_user(user.id)
    log.info("Login: %s (%s) admin=%s", name, email, user.is_admin)

    default  = url_for("admin.dashboard") if user.is_admin else "/"
    next_url = _safe_next_or_default(request.args.get("next", ""), default)
    return redirect(next_url)


@auth_bp.get("/logout")
def logout():
    name = getattr(current_user, "name", "unknown")
    logout_user()
    session.clear()
    log.info("Logout: %s", name)

    try:
        meta = oauth.oidc.load_server_metadata()
        end_session = meta.get("end_session_endpoint")
        if end_session:
            post_logout = url_for("auth.login_page", _external=True)
            return redirect(f"{end_session}?post_logout_redirect_uri={post_logout}")
    except Exception:
        pass

    return redirect(url_for("auth.login_page"))

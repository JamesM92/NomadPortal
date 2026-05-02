"""CSRF protection utilities."""

import hmac
import os

from flask import abort, g, request, session


def get_token() -> str:
    """Return (generating if needed) the CSRF token for the current session."""
    if "_csrf" not in session:
        session["_csrf"] = os.urandom(32).hex()
    return session["_csrf"]


def init_csrf(app) -> None:
    """Register the token generator on the app (before_request + Jinja2 global)."""

    @app.before_request
    def _set_csrf():
        g.csrf_token = get_token()

    app.jinja_env.globals["csrf_token"] = get_token


def check() -> None:
    """Validate the CSRF token for the current request.

    - Skips safe methods (GET, HEAD, OPTIONS).
    - JSON requests: checks the ``X-CSRF-Token`` header.
    - Form requests: checks the ``_csrf_token`` field.
    Aborts with 403 on failure.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    expected = session.get("_csrf", "")
    if not expected:
        abort(403)

    # Header wins for AJAX; form field is fallback for plain HTML forms
    submitted = (
        request.headers.get("X-CSRF-Token", "")
        or request.form.get("_csrf_token", "")
    )

    if not submitted or not hmac.compare_digest(submitted, expected):
        abort(403)

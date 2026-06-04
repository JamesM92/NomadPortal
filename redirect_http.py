#!/usr/bin/env python3
"""Redirect all HTTP requests to HTTPS.

Started by entrypoint.sh only when both WEB_PORT and WEB_PORT_HTTPS are
non-empty (the classic HTTPS+redirector deployment). The empty-string
guards below are belt-and-braces in case this script is invoked directly
with a misconfigured environment.
"""
import http.server
import os
import re
import socketserver
import sys


def _port(name: str, default: str) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        raw = default
    try:
        return int(raw)
    except ValueError:
        sys.stderr.write(
            f"[redirect_http] {name}={raw!r} is not a valid port; using {default}\n"
        )
        return int(default)


HTTPS_PORT = _port("WEB_PORT_HTTPS", "8443")
HTTP_PORT  = _port("WEB_PORT", "8080")

# Strict hostname allow-list — letters/digits/dots/hyphens only, optionally
# wrapped in brackets for IPv6 literals. Reflecting the Host header into
# the Location response opens an HTTP-response-splitting / open-redirect
# door if we don't sanitise: a client can send `Host: example.com\r\n
# Set-Cookie: ...\r\n` and inject arbitrary headers into the 301
# response. Reject any Host that doesn't match this pattern and fall
# back to "localhost" so the redirect still works for legitimate clients.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$|^\[[0-9A-Fa-f:.]+\]$")
# Request path likewise must not contain CR/LF before we splice it into
# the Location header.
_PATH_RE = re.compile(r"^[^\r\n]+$")


def _strip_crlf(raw: str) -> str:
    """Explicit CR/LF strip — CodeQL's py/http-response-splitting query
    recognises ``.replace("\\r", "")``/``.replace("\\n", "")`` as a
    sanitisation barrier, and this is what the rule expects to see on
    any value spliced into a response header. The regex allow-lists
    below are belt-and-braces (a value that's not ``[A-Za-z0-9.\\-]+``
    falls through to a safe default), but the CR/LF strip is what
    makes the dataflow analysis green."""
    return raw.replace("\r", "").replace("\n", "")


def _safe_host(raw: str) -> str:
    head = _strip_crlf((raw or "").split(":", 1)[0]).strip()
    return head if _HOST_RE.match(head) else "localhost"


def _safe_path(raw: str) -> str:
    candidate = _strip_crlf(raw or "")
    return candidate if candidate and _PATH_RE.match(candidate) else "/"


class _Redirect(http.server.BaseHTTPRequestHandler):
    def _redirect(self):
        host = _safe_host(self.headers.get("Host", ""))
        path = _safe_path(self.path)
        self.send_response(301)
        self.send_header("Location", f"https://{host}:{HTTPS_PORT}{path}")
        self.end_headers()

    do_GET  = _redirect
    do_POST = _redirect
    do_PUT  = _redirect
    do_HEAD = _redirect

    def log_message(self, *_):
        pass


socketserver.TCPServer.allow_reuse_address = True
try:
    srv = socketserver.TCPServer(("0.0.0.0", HTTP_PORT), _Redirect)
except OSError as exc:
    sys.stderr.write(
        f"[redirect_http] WARNING: cannot bind 0.0.0.0:{HTTP_PORT} ({exc}). "
        f"HTTP→HTTPS redirector will not run; HTTPS on {HTTPS_PORT} is unaffected.\n"
    )
    sys.exit(0)

with srv:
    srv.serve_forever()

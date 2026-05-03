#!/usr/bin/env python3
"""Redirect all HTTP requests to HTTPS.

Started by entrypoint.sh only when both WEB_PORT and WEB_PORT_HTTPS are
non-empty (the classic HTTPS+redirector deployment). The empty-string
guards below are belt-and-braces in case this script is invoked directly
with a misconfigured environment.
"""
import http.server
import os
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


class _Redirect(http.server.BaseHTTPRequestHandler):
    def _redirect(self):
        host = (self.headers.get("Host") or "localhost").split(":")[0]
        self.send_response(301)
        self.send_header("Location", f"https://{host}:{HTTPS_PORT}{self.path}")
        self.end_headers()

    do_GET  = _redirect
    do_POST = _redirect
    do_PUT  = _redirect
    do_HEAD = _redirect

    def log_message(self, *_):
        pass


socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("0.0.0.0", HTTP_PORT), _Redirect) as srv:
    srv.serve_forever()

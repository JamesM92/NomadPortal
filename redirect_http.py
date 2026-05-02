#!/usr/bin/env python3
"""Redirect all HTTP requests to HTTPS."""
import http.server
import os
import socketserver

HTTPS_PORT = int(os.environ.get("WEB_PORT_HTTPS", "8443"))
HTTP_PORT  = int(os.environ.get("WEB_PORT", "8080"))


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

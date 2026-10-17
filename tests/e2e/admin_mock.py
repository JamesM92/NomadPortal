"""Mounts the REAL admin blueprint, templates and stores on the mock backend,
so the browser tests exercise the genuine admin pages and their JavaScript.
Only Reticulum is replaced: a fake browser. Files go to a throwaway directory.

``mount(app)`` is called once by server.py. Every request is signed in as
the admin below, unless the scenario sets ``admin_role`` to "user" (a signed
in non-admin) or "anon".
"""

import os
import tempfile
import time

from flask import request

from nomadnet_web import csrf as csrf_mod, register_template_filters
from nomadnet_web.admin_routes import admin_bp
from nomadnet_web.auth import OIDCUser, auth_bp, login_manager as lm
from nomadnet_web.cache import PageCache
from nomadnet_web.identity_store import IdentityStore
from nomadnet_web.traffic import TrafficCounters
from nomadnet_web.ui_settings import UISettings
from nomadnet_web.user_store import UserStore


class FakeBrowser:
    def get_nodes(self, *a, **k):
        return [{"hash": "ab" * 16, "name": "Test node", "hops": 2, "last_seen": time.time(),
                 "first_seen": time.time() - 3600, "announce_count": 4, "hosted": False,
                 "favorite": False, "is_default": False}]

    def get_status(self):
        return {"total_announces": 4, "interfaces": [
            {"name": "TCP hub", "type": "TCPClientInterface", "online": True, "rxb": 1200, "txb": 800,
             "life_rxb": 5200, "life_txb": 4800,
             "bitrate": 10_000_000, "clients": None, "mode": "full"}]}


def reset(app):
    """Fresh stores in a fresh directory (called at the start of every scenario)."""
    tmp = tempfile.mkdtemp(prefix="np-e2e-admin-")
    os.makedirs(os.path.join(tmp, "rns"), exist_ok=True)
    app.config.update(
        SECRET_KEY="e2e", START_TIME=time.time() - 5000,
        BROWSER=FakeBrowser(), CACHE=PageCache(),
        CONFIG_YML=os.path.join(tmp, "config.yml"), RNS_CONFIG_DIR=os.path.join(tmp, "rns"),
        TRAFFIC=TrafficCounters(tmp), UI_SETTINGS=UISettings(tmp),
        USER_STORE=UserStore(os.path.join(tmp, "users.yml")),
        IDENTITY_STORE=IdentityStore(os.path.join(tmp, "rns")),
    )
    app.config["E2E_TMP"] = tmp


def mount(app, scenario):
    """``scenario`` is a callable returning the current scenario dict."""
    reset(app)

    lm.init_app(app)     # the admin blueprint's guard calls this very object
    lm.login_view = "auth.login_page"
    admin = OIDCUser("root", "root@example.com", "Root", is_admin=True, super_admin=True)
    plain = OIDCUser("bob", "bob@example.com", "Bob", is_admin=False)

    @lm.request_loader
    def _who(_req):
        if not request.path.startswith("/admin"):
            return None
        role = scenario().get("admin_role", "admin")
        return {"admin": admin, "user": plain}.get(role)

    @lm.user_loader
    def _by_id(uid):
        return None

    register_template_filters(app)
    csrf_mod.init_csrf(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

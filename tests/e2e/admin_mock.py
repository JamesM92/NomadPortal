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
from nomadnet_web.admin_account import AdminAccount
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


_scenario = lambda: {}   # noqa: E731  (replaced by mount())


def reset(app):
    """Fresh stores in a fresh directory (called at the start of every scenario).
    Scenario keys used here: ``admin_env_password`` (turns the ADMIN_PASSWORD
    override on) and ``admin_account`` ({"username", "password"} to start with
    an account already set up)."""
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
    scn = _scenario()
    account = AdminAccount(tmp)
    if scn.get("admin_account"):
        account.set_password(scn["admin_account"]["password"], username=scn["admin_account"]["username"])
    app.config.update(ADMIN_ACCOUNT=account, ADMIN_PASSWORD=scn.get("admin_env_password", ""),
                      ADMIN_USERNAME="root")
    account.ensure_setup_code()


def mount(app, scenario):
    """``scenario`` is a callable returning the current scenario dict."""
    global _scenario
    _scenario = scenario
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
        return {"admin": admin, "user": plain}.get(role)     # "real": use the genuine session

    register_template_filters(app)
    csrf_mod.init_csrf(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)


def seed_worst(app):
    """Fill the admin stores with realistic worst-case rows (dev-only; the
    ``/__admin_seed`` route calls this). Long names and addresses that real
    deployments do have: federated emails, IPv6, long hostnames, names with
    markup characters, empty and one-letter names."""
    import yaml
    from nomadnet_web import auth
    store = app.config["USER_STORE"]
    for name in ("aleksandra.wisniewska-kowalczyk", "Jo", "n" * 64, "o'brien<b>"):
        store.create_local_user(name, "pw12345678", is_admin=(name == "Jo"))
    for sub, email, name in (
        ("oidc|1", "bartholomew.fitzgerald@northwind-industries-holdings.example.com", "Bartholomew Fitzgerald-Montgomery III"),
        ("oidc|2", "x@y.io", ""),
        ("oidc|3", "", "Мария Ивановна Петрова-Сидорова"),
        ("oidc|4", "a.very.long.local.part.that.is.still.valid.under.rfc.5321@sub.domain.example.co.uk", "👩🏽‍🚀"),
    ):
        store.register_or_update(sub, email, name)
    for i, (name, ip) in enumerate((
        ("Aleksandra Wiśniewska-Kowalczyk", "2001:0db8:85a3:0000:0000:8a2e:0370:7334"),
        ("Jo", "10.0.0.2"), ("", "203.0.113.200"),
        ("bartholomew.fitzgerald@northwind-industries-holdings.example.com", "198.51.100.17"),
    )):
        auth._record_session(auth.OIDCUser(f"sess{i}", email=name if "@" in name else "", name=name, is_admin=(i == 0)), ip)
    idstore = app.config["IDENTITY_STORE"]
    for sub, name in (("local:aleksandra", "Aleksandra Wiśniewska-Kowalczyk"), ("oidc|1", "B" * 64), ("oidc|2", "")):
        try:
            idstore.ensure_for_user(sub, name)
        except Exception:
            pass
    traffic = app.config["TRAFFIC"]
    for path, n in (("/page/index.mu", 40), ("/page/" + "a-very-long-section-name/" * 6 + "index.mu", 12),
                    ("/page/ünïcode/пример.mu", 5), ("/page/x?q=" + "z" * 120, 1)):
        for _ in range(n):
            traffic.record("ab" * 16, path, "web")
    cfg = {"interfaces": {
        "tcp_clients": [{"name": "Primary northern hub (Winnipeg) - do not remove, contact the operator first",
                         "host": "a-really-long-hostname.subdomain.example-network-operations.co.uk", "port": 4242, "enabled": True},
                        {"name": "H", "host": "h", "port": 1, "enabled": False}],
        "auto": {"enabled": True}}}
    with open(app.config["CONFIG_YML"], "w") as fh:
        yaml.safe_dump(cfg, fh)

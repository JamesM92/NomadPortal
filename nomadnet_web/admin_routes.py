"""
Admin blueprint.

Every route in this blueprint requires both authentication and admin role —
enforcement is centralised in the before_request hook (_check_access).
Per-route @login_required / @admin_required decorators are kept for clarity
but are redundant given the blueprint-wide guard.
"""

import json
import logging
import os
import time

from .routes import _render_title_html

import yaml
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, jsonify, current_app, abort, Response, stream_with_context,
)
from flask_login import login_required, current_user

from .auth import admin_required, login_manager, _is_admin as _effective_admin
from .config_gen import generate
from .log_buffer import buffer as log_buffer
from . import csrf as csrf_mod

log   = logging.getLogger(__name__)
_audit = logging.getLogger("nomadnet.audit")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin",
                     template_folder="../templates")


@admin_bp.context_processor
def _ui_context():
    ui = current_app.config.get("UI_SETTINGS")
    raw = ui.get_all().get("app_title", "`F4af■ NomadPortal`f") if ui else "`F4af■ NomadPortal`f"
    return {"app_title": raw, "app_title_html": _render_title_html(raw)}


@admin_bp.before_request
def _check_access():
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if not getattr(current_user, "is_admin", False):
        abort(403)
    csrf_mod.check()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _browser():       return current_app.config["BROWSER"]
def _cache():         return current_app.config["CACHE"]
def _id_store():      return current_app.config["IDENTITY_STORE"]
def _messaging():     return current_app.config["MESSAGING"]
def _config_yml():    return current_app.config["CONFIG_YML"]
def _rns_dir():       return current_app.config["RNS_CONFIG_DIR"]
def _user_store():    return current_app.config.get("USER_STORE")
def _contact_store(): return current_app.config.get("CONTACT_STORE")


def _load_config() -> dict:
    path = _config_yml()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _save_config(cfg: dict) -> None:
    path = _config_yml()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh, default_flow_style=False, allow_unicode=True)
    generate(path, os.path.join(_rns_dir(), "interfaces.conf"))


# ---------------------------------------------------------------------------
# Dashboard  (any logged-in user)
# ---------------------------------------------------------------------------

@admin_bp.get("")
@admin_bp.get("/")
@login_required
def dashboard():
    site_server = current_app.config.get("SITE_SERVER")
    site_info = None
    if site_server and site_server.node_hash():
        site_info = {
            "node_hash": site_server.node_hash(),
            "node_name": site_server.node_name(),
        }
    return render_template(
        "admin/dashboard.html",
        nodes=_browser().get_nodes(),
        rns_status=_browser().get_status(),
        cache_stats=_cache().stats(),
        uptime_s=int(time.time() - current_app.config["START_TIME"]),
        now=time.time(),
        user=current_user,
        site_info=site_info,
    )


# ---------------------------------------------------------------------------
# Interface configuration
# GET  — login_required, read-only shown to non-admins
# POST — admin_required
# ---------------------------------------------------------------------------

@admin_bp.get("/interfaces")
@admin_required
def interfaces():
    cfg = _load_config()
    return render_template(
        "admin/interfaces.html",
        user=current_user,
        ifaces=cfg.get("interfaces", {}),
        transport_mode=bool(cfg.get("transport_mode", False)),
        ignore_discovery_probes=bool(cfg.get("ignore_discovery_probes", False)),
        shared_instance=cfg.get("shared_instance") or {},
    )


def _trigger_worker_reload(delay: float = 0.5) -> tuple[bool, str]:
    """Schedule a graceful reload of the WSGI worker.

    Detects gunicorn via SERVER_SOFTWARE and sends SIGHUP to the master process
    after `delay` seconds — long enough for the current response to flush.
    Returns (scheduled, reason).

    In Docker, the gunicorn master is PID 1 — we accept that as long as
    /proc/<pid>/cmdline confirms it's actually gunicorn, so we're not
    accidentally signalling init/systemd on a host system."""
    import signal
    import threading

    server = os.environ.get("SERVER_SOFTWARE", "")
    if not server.lower().startswith("gunicorn"):
        return False, "Reload requires gunicorn — current server is " + (server or "Flask dev server")

    ppid = os.getppid()
    if ppid <= 0:
        return False, "No parent process"

    try:
        with open(f"/proc/{ppid}/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", errors="ignore").strip()
    except OSError as exc:
        return False, f"Cannot read /proc/{ppid}/cmdline: {exc}"

    if "gunicorn" not in cmdline.lower():
        return False, f"Parent pid={ppid} is not gunicorn ({cmdline[:80]!r})"

    def _send():
        try:
            os.kill(ppid, signal.SIGHUP)
            log.info("Sent SIGHUP to gunicorn master pid=%d for graceful reload", ppid)
        except Exception as exc:
            log.warning("Failed to signal master pid=%d: %s", ppid, exc)

    threading.Timer(delay, _send).start()
    return True, f"SIGHUP scheduled to pid {ppid} in {delay}s"


@admin_bp.post("/interfaces/reload")
@admin_required
def interfaces_reload():
    """Trigger a graceful gunicorn reload to apply interface changes."""
    ok, reason = _trigger_worker_reload()
    _audit.warning("interfaces_reload: actor=%s ip=%s ok=%s reason=%s",
                   getattr(current_user, "name", "?"), request.remote_addr,
                   ok, reason)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 503
    return jsonify({"ok": True, "message": reason})


@admin_bp.post("/interfaces")
@admin_required
def interfaces_save():
    cfg    = _load_config()
    ifaces = cfg.setdefault("interfaces", {})

    cfg["transport_mode"] = "transport_mode" in request.form
    cfg["ignore_discovery_probes"] = "ignore_discovery_probes" in request.form

    # Shared instance — RNS [reticulum] shared-state knobs
    shared = cfg.setdefault("shared_instance", {})
    shared["enabled"] = "shared_instance_enabled" in request.form
    name_raw = request.form.get("shared_instance_name", "").strip()
    shared["instance_name"] = name_raw  # empty string → strips key in config_gen
    port_raw = request.form.get("shared_instance_port", "").strip()
    ctrl_raw = request.form.get("shared_instance_control_port", "").strip()
    shared["port"]         = int(port_raw) if port_raw.isdigit() and 0 < int(port_raw) < 65536 else None
    shared["control_port"] = int(ctrl_raw) if ctrl_raw.isdigit() and 0 < int(ctrl_raw) < 65536 else None

    # AutoInterface
    ifaces.setdefault("auto", {})["enabled"] = "auto_enabled" in request.form
    if request.form.get("auto_group_id", "").strip():
        ifaces["auto"]["group_id"] = request.form["auto_group_id"].strip()

    # TCP Clients
    names       = request.form.getlist("tcp_client_name")
    hosts       = request.form.getlist("tcp_client_host")
    ports       = request.form.getlist("tcp_client_port")
    modes       = request.form.getlist("tcp_client_mode")
    net_names   = request.form.getlist("tcp_client_network_name")
    passphrases = request.form.getlist("tcp_client_passphrase")
    enabled     = set(request.form.getlist("tcp_client_enabled"))
    clients = []
    for i, name in enumerate(names):
        if not name.strip():
            continue
        entry = {
            "name":    name.strip(),
            "host":    hosts[i].strip() if i < len(hosts) else "",
            "port":    int(ports[i]) if i < len(ports) and ports[i].isdigit() else 4965,
            "enabled": str(i) in enabled,
        }
        if i < len(modes) and modes[i].strip():
            entry["mode"] = modes[i].strip()
        if i < len(net_names) and net_names[i].strip():
            entry["network_name"] = net_names[i].strip()
        if i < len(passphrases) and passphrases[i].strip():
            entry["passphrase"] = passphrases[i].strip()
        clients.append(entry)
    ifaces["tcp_clients"] = clients

    # TCP Servers
    srv_names       = request.form.getlist("tcp_server_name")
    srv_ips         = request.form.getlist("tcp_server_ip")
    srv_ports       = request.form.getlist("tcp_server_port")
    srv_modes       = request.form.getlist("tcp_server_mode")
    srv_net_names   = request.form.getlist("tcp_server_network_name")
    srv_passphrases = request.form.getlist("tcp_server_passphrase")
    srv_enabled     = set(request.form.getlist("tcp_server_enabled"))
    servers = []
    for i, name in enumerate(srv_names):
        if not name.strip():
            continue
        entry = {
            "name":      name.strip(),
            "listen_ip": srv_ips[i].strip() if i < len(srv_ips) else "0.0.0.0",
            "port":      int(srv_ports[i]) if i < len(srv_ports) and srv_ports[i].isdigit() else 4242,
            "enabled":   str(i) in srv_enabled,
        }
        if i < len(srv_modes) and srv_modes[i].strip():
            entry["mode"] = srv_modes[i].strip()
        if i < len(srv_net_names) and srv_net_names[i].strip():
            entry["network_name"] = srv_net_names[i].strip()
        if i < len(srv_passphrases) and srv_passphrases[i].strip():
            entry["passphrase"] = srv_passphrases[i].strip()
        servers.append(entry)
    ifaces["tcp_servers"] = servers

    # UDP
    udp_names    = request.form.getlist("udp_name")
    udp_l_ips    = request.form.getlist("udp_listen_ip")
    udp_l_ports  = request.form.getlist("udp_listen_port")
    udp_f_ips    = request.form.getlist("udp_forward_ip")
    udp_f_ports  = request.form.getlist("udp_forward_port")
    udp_enabled  = set(request.form.getlist("udp_enabled"))
    udp_list = []
    for i, name in enumerate(udp_names):
        if not name.strip():
            continue
        l_port = int(udp_l_ports[i]) if i < len(udp_l_ports) and udp_l_ports[i].isdigit() else 4242
        udp_list.append({
            "name":         name.strip(),
            "listen_ip":    udp_l_ips[i].strip() if i < len(udp_l_ips) else "0.0.0.0",
            "listen_port":  l_port,
            "forward_ip":   udp_f_ips[i].strip() if i < len(udp_f_ips) else "255.255.255.255",
            "forward_port": int(udp_f_ports[i]) if i < len(udp_f_ports) and udp_f_ports[i].isdigit() else l_port,
            "enabled":      str(i) in udp_enabled,
        })
    ifaces["udp"] = udp_list

    # RNodes
    rn_names   = request.form.getlist("rnode_name")
    rn_ports   = request.form.getlist("rnode_port")
    rn_freqs   = request.form.getlist("rnode_frequency")
    rn_bws     = request.form.getlist("rnode_bandwidth")
    rn_pwr     = request.form.getlist("rnode_txpower")
    rn_sf      = request.form.getlist("rnode_sf")
    rn_cr      = request.form.getlist("rnode_cr")
    rn_enabled = set(request.form.getlist("rnode_enabled"))
    rnodes = []
    for i, name in enumerate(rn_names):
        if not name.strip():
            continue
        rnodes.append({
            "name":            name.strip(),
            "port":            rn_ports[i].strip() if i < len(rn_ports) else "/dev/ttyUSB0",
            "frequency":       int(rn_freqs[i]) if i < len(rn_freqs) and rn_freqs[i].isdigit() else 867500000,
            "bandwidth":       int(rn_bws[i])   if i < len(rn_bws)   and rn_bws[i].isdigit()   else 125000,
            "txpower":         int(rn_pwr[i])   if i < len(rn_pwr)   and rn_pwr[i].isdigit()   else 7,
            "spreading_factor":int(rn_sf[i])    if i < len(rn_sf)    and rn_sf[i].isdigit()    else 8,
            "coding_rate":     int(rn_cr[i])    if i < len(rn_cr)    and rn_cr[i].isdigit()    else 5,
            "enabled":         str(i) in rn_enabled,
        })
    ifaces["rnodes"] = rnodes

    # I2P
    i2p_names   = request.form.getlist("i2p_name")
    i2p_peers   = request.form.getlist("i2p_peers")
    i2p_conn    = set(request.form.getlist("i2p_connectable"))
    i2p_enabled = set(request.form.getlist("i2p_enabled"))
    i2p_list = []
    for i, name in enumerate(i2p_names):
        if not name.strip():
            continue
        peer_str = i2p_peers[i] if i < len(i2p_peers) else ""
        peers = [p.strip() for p in peer_str.split(",") if p.strip()]
        i2p_list.append({
            "name":        name.strip(),
            "connectable": str(i) in i2p_conn,
            "peers":       peers,
            "enabled":     str(i) in i2p_enabled,
        })
    ifaces["i2p"] = i2p_list

    _save_config(cfg)
    _audit.warning("interfaces_save: actor=%s ip=%s",
                   getattr(current_user, "name", "?"),
                   request.remote_addr)
    flash("Configuration saved. Restart the container to apply changes.", "ok")
    return redirect(url_for("admin.interfaces"))


# ---------------------------------------------------------------------------
# Identities API  (JSON — any logged-in user)
# ---------------------------------------------------------------------------

@admin_bp.get("/api/identities")
@login_required
def api_identities():
    entries = [
        {
            "id":             e["id"],
            "name":           e["name"],
            "last_announced": e.get("last_announced"),
        }
        for e in _id_store().list_identities()
    ]
    return jsonify({"identities": entries})



@admin_bp.post("/identities/<identity_id>/announce")
@login_required
def identity_announce(identity_id: str):
    # Check / update cooldown via identity store
    ok, message, next_allowed = _id_store().check_cooldown(identity_id)
    if ok:
        # Announce through the LXMRouter so display name is packed into app_data
        messaging = current_app.config.get("MESSAGING")
        if messaging:
            ok, message = messaging.do_announce(user_sub=current_user.id)
        else:
            ok, message = False, "Messaging service not available"
    # Always 200 — client inspects ok/message; HTTP errors would mask the real reason
    return jsonify({"ok": ok, "message": message, "next_allowed": next_allowed})


# ---------------------------------------------------------------------------
# Identities  (admin only — one identity per user, admin can reset)
# ---------------------------------------------------------------------------

@admin_bp.get("/identities")
@admin_required
def identities():
    return render_template(
        "admin/identities.html",
        user=current_user,
        identities=_id_store().list_identities(),
    )


@admin_bp.post("/identities/<identity_id>/reset")
@admin_required
def identity_reset(identity_id: str):
    old_entry = _id_store().get(identity_id)
    new_entry = _id_store().reset(identity_id)
    if new_entry:
        _audit.warning("identity_reset: old=%s new=%s actor=%s ip=%s",
                       identity_id[:16], new_entry["id"][:16],
                       getattr(current_user, "name", "?"), request.remote_addr)
        # Drop the cached router so it is rebuilt with the new keypair.
        messaging = current_app.config.get("MESSAGING")
        if messaging and old_entry:
            user_sub = old_entry.get("user_sub", "")
            if user_sub:
                messaging.reset_user_router(user_sub)
                messaging.setup_user(user_sub)
        flash("Identity reset — a fresh keypair has been generated.", "ok")
    else:
        flash("Identity not found.", "error")
    return redirect(url_for("admin.identities"))




# ---------------------------------------------------------------------------
# Node actions  (any logged-in user)
# ---------------------------------------------------------------------------


@admin_bp.post("/nodes/<path:hash_hex>/ping")
@login_required
def node_ping(hash_hex: str):
    ms, error = _browser().ping_node(hash_hex)
    if error:
        return jsonify({"error": error}), 503
    return jsonify({"ms": ms})


# ---------------------------------------------------------------------------
# Cache  (admin only)
# ---------------------------------------------------------------------------

@admin_bp.get("/cache")
@admin_required
def cache_view():
    return render_template("admin/cache.html", user=current_user,
                           stats=_cache().stats())


@admin_bp.post("/cache/clear")
@admin_required
def cache_clear():
    _cache().clear()
    _audit.warning("cache_clear: actor=%s ip=%s",
                   getattr(current_user, "name", "?"), request.remote_addr)
    flash("Cache cleared.", "ok")
    return redirect(url_for("admin.cache_view"))


# ---------------------------------------------------------------------------
# User management  (admin only — OIDC users only; local admin is not stored)
# ---------------------------------------------------------------------------

@admin_bp.get("/users")
@admin_required
def users():
    store = _user_store()
    if store is None:
        flash("User store is not configured (OIDC not enabled).", "error")
        return redirect(url_for("admin.dashboard"))
    user_list = store.list_users()
    # Enrich with the effective admin state — per-user UI flag wins, falls
    # back to the OIDC_ADMIN_EMAILS / OIDC_ADMIN_SUBJECTS env-var allowlist.
    # Flag `admin_via_env` lets the template show a hint when the admin
    # status doesn't come from an explicit per-user toggle.
    for u in user_list:
        explicit = u.get("is_admin")
        if explicit is None:
            u["effective_admin"] = _effective_admin(u.get("email", ""), u.get("sub", ""))
            u["admin_via_env"]   = bool(u["effective_admin"])
        else:
            u["effective_admin"] = bool(explicit)
            u["admin_via_env"]   = False
    return render_template(
        "admin/users.html",
        user=current_user,
        users=user_list,
        new_users_enabled=store.new_users_enabled,
    )


@admin_bp.post("/users/<path:sub>/enable")
@admin_required
def user_enable(sub: str):
    store = _user_store()
    if store and store.set_enabled(sub, True):
        _audit.warning("user_enable: sub=%s actor=%s ip=%s",
                       sub[:16], getattr(current_user, "name", "?"), request.remote_addr)
        flash("Account enabled.", "ok")
    else:
        flash("User not found.", "error")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<path:sub>/disable")
@admin_required
def user_disable(sub: str):
    store = _user_store()
    if store and store.set_enabled(sub, False):
        _audit.warning("user_disable: sub=%s actor=%s ip=%s",
                       sub[:16], getattr(current_user, "name", "?"), request.remote_addr)
        flash("Account disabled.", "ok")
    else:
        flash("User not found.", "error")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/settings")
@admin_required
def users_settings():
    store = _user_store()
    if store is None:
        flash("User store is not configured.", "error")
        return redirect(url_for("admin.dashboard"))
    store.new_users_enabled = "new_users_enabled" in request.form
    flash("User policy updated.", "ok")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/create")
@admin_required
def user_create():
    store = _user_store()
    if store is None:
        flash("User store is not configured.", "error")
        return redirect(url_for("admin.users"))
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    is_admin = "is_admin" in request.form
    record, err = store.create_local_user(username, password, is_admin=is_admin)
    if err:
        flash(f"Could not create user: {err}", "error")
    else:
        _audit.warning("user_create: name=%s admin=%s actor=%s ip=%s",
                       username, is_admin, getattr(current_user, "name", "?"), request.remote_addr)
        flash(f"User '{username}' created.", "ok")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<path:sub>/set-admin")
@admin_required
def user_set_admin(sub: str):
    store = _user_store()
    is_admin = request.form.get("is_admin") == "1"
    if store and store.set_admin(sub, is_admin):
        _audit.warning("user_set_admin: sub=%s is_admin=%s actor=%s ip=%s",
                       sub[:16], is_admin, getattr(current_user, "name", "?"), request.remote_addr)
        flash("Admin status updated.", "ok")
    else:
        flash("User not found.", "error")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<path:sub>/delete")
@admin_required
def user_delete(sub: str):
    store = _user_store()
    if sub == current_user.id:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin.users"))
    if store and store.delete_user(sub):
        _audit.warning("user_delete: sub=%s actor=%s ip=%s",
                       sub[:16], getattr(current_user, "name", "?"), request.remote_addr)
        flash("User deleted.", "ok")
    else:
        flash("User not found.", "error")
    return redirect(url_for("admin.users"))


# ---------------------------------------------------------------------------
# Sessions  (admin only)
# ---------------------------------------------------------------------------

@admin_bp.get("/sessions")
@admin_required
def sessions():
    from .auth import list_sessions
    return render_template(
        "admin/sessions.html",
        user=current_user,
        sessions=list_sessions(),
        now=time.time(),
    )


@admin_bp.post("/sessions/<path:sub>/revoke")
@admin_required
def session_revoke(sub: str):
    from .auth import revoke_session
    if revoke_session(sub):
        _audit.warning("session_revoke: sub=%s actor=%s ip=%s",
                       sub[:16], getattr(current_user, "name", "?"), request.remote_addr)
        flash("Session revoked.", "ok")
    else:
        flash("Session not found (already expired?).", "error")
    return redirect(url_for("admin.sessions"))


@admin_bp.post("/sessions/revoke")
@admin_required
def sessions_revoke():
    from .auth import revoke_all_sessions
    count = revoke_all_sessions()
    _audit.warning("sessions_revoke: %d sessions cleared by %s ip=%s",
                   count, getattr(current_user, "name", "?"), request.remote_addr)
    flash(f"Revoked {count} active session(s). All users must log in again.", "ok")
    return redirect(url_for("admin.sessions"))


# ---------------------------------------------------------------------------
# UI Settings  (admin only)
# ---------------------------------------------------------------------------

@admin_bp.get("/settings")
@admin_required
def settings():
    ui = current_app.config.get("UI_SETTINGS")
    oidc_enabled = bool(current_app.config.get("OIDC_CLIENT_ID"))
    no_oidc_admin = oidc_enabled and not (
        current_app.config.get("OIDC_ADMIN_EMAILS") or
        current_app.config.get("OIDC_ADMIN_SUBJECTS")
    )
    return render_template(
        "admin/settings.html",
        user=current_user,
        settings=ui.get_all() if ui else {},
        config=current_app.config,
        no_oidc_admin=no_oidc_admin,
        is_super_admin=getattr(current_user, "super_admin", False),
    )


@admin_bp.post("/api/ui/settings")
@admin_required
def api_ui_settings_save():
    data = request.get_json(silent=True) or {}
    ui   = current_app.config.get("UI_SETTINGS")
    if ui is None:
        abort(503)
    # Only forward keys the client actually sent — UISettings.update() merges
    # into the existing snapshot, so missing keys preserve their current value.
    allowed = (
        # Per-audience access controls
        "guests_default_lock", "users_default_lock", "admins_default_lock",
        "guests_address_bar", "users_address_bar", "admins_address_bar",
        "guests_nodes_panel", "users_nodes_panel", "admins_nodes_panel",
        "guests_messages_panel", "users_messages_panel", "admins_messages_panel",
        "users_can_message",
        # General settings
        "app_title", "site_name", "default_node",
        "access_mode", "lockdown_node",  # presets / backwards-compat alias
        "abuse_contact",
    )
    patch = {k: data[k] for k in allowed if k in data}

    # Admin-column edits are reserved for the super admin (env-var ADMIN_PASSWORD
    # login). Silently drop those fields for regular admins so the API doesn't
    # leak which fields exist, but log the attempt for auditing.
    from .ui_settings import ADMIN_GATED_FIELDS
    if not getattr(current_user, "super_admin", False):
        gated = ADMIN_GATED_FIELDS & patch.keys()
        if gated:
            _audit.warning(
                "ui_settings_save: dropped admin-gated fields actor=%s ip=%s fields=%s",
                getattr(current_user, "name", "?"), request.remote_addr, sorted(gated),
            )
            for k in gated:
                patch.pop(k, None)
        # If the preset was selected, it would overwrite admin fields too —
        # apply it manually with the admin fields stripped out.
        if patch.get("access_mode") in {"public", "gated", "locked"}:
            from .ui_settings import _PRESETS
            preset_values = {k: v for k, v in _PRESETS[patch["access_mode"]].items()
                             if k not in ADMIN_GATED_FIELDS}
            patch.pop("access_mode", None)
            for k, v in preset_values.items():
                patch.setdefault(k, v)

    ui.update(patch)

    # Apply node name change immediately — update SiteServer, browser cache, and re-announce.
    site_server = current_app.config.get("SITE_SERVER")
    new_name = patch.get("site_name")
    if site_server and new_name:
        site_server._node_name = new_name
        browser = current_app.config.get("BROWSER")
        if browser:
            browser._hosted_name = new_name
        site_server.announce()

    _audit.warning("ui_settings_save: actor=%s ip=%s patch=%s",
                   getattr(current_user, "name", "?"), request.remote_addr, patch)
    return jsonify({"ok": True, "settings": ui.get_all()})


@admin_bp.post("/api/preview/title")
@admin_required
def api_preview_title():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", ""))[:128]
    return jsonify({"html": _render_title_html(text)})


# ---------------------------------------------------------------------------
# Live log viewer  (admin only)
# ---------------------------------------------------------------------------

@admin_bp.get("/logs")
@admin_required
def logs_page():
    return render_template(
        "admin/logs.html",
        user=current_user,
        snapshot=log_buffer.snapshot(),
    )


def _log_stream(line_filter=None):
    """Build an SSE generator for the in-memory log buffer.

    line_filter: optional callable taking a line dict and returning bool.
    """
    def generate():
        snapshot = log_buffer.snapshot()
        if line_filter:
            filtered = [l for l in snapshot if line_filter(l)]
        else:
            filtered = snapshot

        last_seq = snapshot[-1]["seq"] if snapshot else 0
        for line in filtered:
            yield f"data: {json.dumps(line)}\n\n"

        while True:
            new_lines = log_buffer.wait_for_new(last_seq, timeout=20.0)
            if new_lines:
                for line in new_lines:
                    last_seq = line["seq"]
                    if line_filter and not line_filter(line):
                        continue
                    yield f"data: {json.dumps(line)}\n\n"
            else:
                yield ": keepalive\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@admin_bp.get("/logs/stream")
@admin_required
def logs_stream():
    return _log_stream()


# ---------------------------------------------------------------------------
# Audit log viewer  (admin only — filters log buffer to nomadnet.audit)
# ---------------------------------------------------------------------------

def _is_audit_line(line: dict) -> bool:
    return line.get("logger") == "nomadnet.audit"


@admin_bp.get("/audit")
@admin_required
def audit_page():
    return render_template(
        "admin/audit.html",
        user=current_user,
        snapshot=[l for l in log_buffer.snapshot() if _is_audit_line(l)],
    )


@admin_bp.get("/audit/stream")
@admin_required
def audit_stream():
    return _log_stream(line_filter=_is_audit_line)


# ---------------------------------------------------------------------------
# Backup  (admin only — download an archive of /config)
# ---------------------------------------------------------------------------

@admin_bp.get("/backup")
@admin_required
def backup_download():
    """Stream a tar.gz of the config directory.

    Excludes ephemeral / regenerated files so the archive is small and
    portable. Includes private key material — treat with the same care
    as the source files."""
    import io
    import tarfile
    import datetime as _dt

    config_dir = current_app.config.get("CONFIG_DIR", "/config")
    skip_names = {
        "ssl",                     # regenerated on first start
        "lxmf",                    # LXMF router runtime state
        "storage",                 # Reticulum routing state
        "interfaces.conf",         # regenerated from config.yml
        "iface_stats.json",        # accumulated stats, not config
    }

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(config_dir):
            # Prune skipped directories in-place so os.walk doesn't descend
            dirs[:] = [d for d in dirs if d not in skip_names]
            for fn in files:
                if fn in skip_names:
                    continue
                full = os.path.join(root, fn)
                arc  = os.path.relpath(full, os.path.dirname(config_dir.rstrip("/")))
                try:
                    tar.add(full, arcname=arc)
                except (OSError, IOError) as exc:
                    log.warning("backup: skipping %s: %s", full, exc)

    buf.seek(0)
    fname = f"nomadportal-backup-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    _audit.warning("backup_download: actor=%s ip=%s size=%d",
                   getattr(current_user, "name", "?"), request.remote_addr,
                   buf.getbuffer().nbytes)
    return Response(
        buf.getvalue(),
        mimetype="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )

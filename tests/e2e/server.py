"""Mock backend for the browser tests.

Serves the real ``templates/index.html`` and ``static/`` files, and
scripts the few ``/api`` responses a test needs, so the real front-end
JavaScript runs against known server behaviour with no Reticulum stack.
Everything else under /api returns an empty JSON value.

    python3 tests/e2e/server.py <port>

A test sets the scenario with POST /__scenario (see ``S`` for the keys).
"""

import base64
import logging
import os
import sys
import threading
import time

from flask import Flask, jsonify, render_template, request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"),
            static_folder=os.path.join(ROOT, "static"))
app.jinja_env.globals["csrf_token"] = lambda: "t"

NODE = "ab" * 16

logging.getLogger("werkzeug").setLevel(logging.ERROR)   # keep test output readable

# Scenario state. "poll" is a list of steps served in order, the last one
# repeating: {"error": "..."} -> 503, or {"html": ..., "micron": ...}.
# Every POST /__scenario starts again from these defaults.
DEFAULTS = {
    "access": None,        # /api/ui/settings "access" snapshot (None = omit)
    "ui_fail": False,      # make /api/ui/settings fail
    "admin": False,        # /api/auth/status says admin
    "cached": None,        # saved copy returned by /api/page/fetch
    "poll": [],            # see above
    "delay": 0.0,          # seconds each poll takes
    "fetches": 0,          # /api/page/fetch calls since the last scenario
    "fetch_status": 0,     # force /api/page/fetch to fail with this status
    # "cached" may carry "fresh": true -> served with no job (#!c=N window)
    "pages": {},           # per-path poll steps, e.g. {"/page/news.mu": [step, ...]};
                           # a path not listed here falls back to "poll"
    "job_paths": {},       # job id -> path (filled by /api/page/fetch)
    "path_fetches": {},    # path -> number of /api/page/fetch calls
    "media": {},           # path -> {"status": 200|404|503, "busy_times": n} for /api/media
    "media_delay": 0.0,    # seconds each /api/media call takes
    "media_calls": {},     # path -> calls
    "media_inflight": 0,   # calls running right now
    "media_max_inflight": 0,
    "received": [],        # /api/messages/received (each: source, content, received_at, read)
    "contacts": [],        # /api/contacts (each: hash, name, trust?, pinned?, note?)
    "settings_posts": [],  # bodies POSTed to /api/contacts/<hash>/settings
    "ui_extra": {},        # extra keys merged into /api/ui/settings (e.g. panel visibility)
    "sent": [],            # /api/messages/sent (each: id, dest, content, sent_at, state, resends?)
    "msg_actions": [],     # ["cancel:<id>", "retry:<id>"] in the order they were POSTed
    "msg_refuse": "",      # when set, cancel/retry answer 409 with this text
    "message_posts": [],   # what POST /api/messages received (files seen as name, type, first bytes)
}
S = dict(DEFAULTS)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/page/<path:rest>")
def server_rendered(rest):
    """A page URL as the real server would return it for a crawler."""
    seo = {"title": "About us · Test Site", "description": "We are a small group.",
           "canonical": "http://x/page/" + rest,
           "html": '<div class="mu-page"><p>SSR CONTENT HERE</p></div>'}
    return render_template("index.html", seo=seo)


@app.post("/__scenario")
def scenario():
    S.clear()
    S.update(DEFAULTS)
    S.update(request.get_json())
    S["poll"] = list(S["poll"])
    S["pages"] = {k: list(v) for k, v in S["pages"].items()}
    S["job_paths"] = {}
    S["path_fetches"] = {}
    S["media"] = dict(S["media"])
    S["media_calls"] = {}
    S["media_inflight"] = 0
    S["media_max_inflight"] = 0
    S["contacts"] = [dict(c) for c in S["contacts"]]
    S["sent"] = [dict(m) for m in S["sent"]]
    S["msg_actions"] = []
    S["message_posts"] = []
    S["settings_posts"] = []
    return jsonify(ok=True)


@app.get("/__stats")
def stats():
    return jsonify(fetches=S["fetches"], refresh_flag=S.get("last_refresh_flag"),
                   path_fetches=S["path_fetches"], media_calls=S["media_calls"],
                   media_max_inflight=S["media_max_inflight"],
                   settings_posts=S["settings_posts"], msg_actions=S["msg_actions"],
                   message_posts=S["message_posts"])


@app.get("/api/auth/status")
def auth():
    return jsonify(logged_in=S["admin"], is_admin=S["admin"], user="admin" if S["admin"] else None)


@app.get("/api/ui/settings")
def ui():
    if S["ui_fail"]:
        return jsonify(error="down"), 503
    d = dict(S["ui_extra"])
    if S["access"] is not None:
        d["access"] = S["access"]
    return jsonify(d)


@app.post("/api/page/fetch")
def fetch():
    S["fetches"] += 1
    body = request.get_json(silent=True) or {}
    S["last_refresh_flag"] = bool(body.get("refresh"))
    path = body.get("path") or ""
    if body.get("url"):                       # hash://<hash>/<path>
        parts = body["url"].split("/", 3)
        path = "/" + parts[3] if len(parts) > 3 else "/page/index.mu"
    S["job_paths"][f"j{S['fetches']}"] = path
    S["path_fetches"][path] = S["path_fetches"].get(path, 0) + 1
    if S["fetch_status"]:
        return jsonify(error="Login required to browse external nodes"), S["fetch_status"]
    # A saved copy marked fresh is served with no job, unless the browser
    # asked for a live fetch (Refresh button, auto-reload).
    if S["cached"] and S["cached"].get("fresh") and not body.get("refresh"):
        return jsonify({"job_id": None, "raw": False, "cached": S["cached"]})
    r = {"job_id": f"j{S['fetches']}", "raw": False}
    if S["cached"]:
        r["cached"] = {k: v for k, v in S["cached"].items() if k != "fresh"}
    return jsonify(r)


@app.get("/api/page/poll")
def poll():
    time.sleep(S["delay"])
    steps = S["pages"].get(S["job_paths"].get(request.args.get("id", ""), ""), S["poll"])
    step = steps[0] if len(steps) == 1 else steps.pop(0)
    if step.get("error"):
        return jsonify(status="error", error=step["error"]), 503
    return jsonify(status="done", progress=1, hash=NODE, path="/page/index.mu",
                   html=step["html"], micron=step["micron"])


# A real 1x1 lossless WebP, so the browser has something to decode.
TINY_WEBP = base64.b64decode("UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==")
_media_lock = threading.Lock()


@app.get("/api/media")
def media():
    path = request.args.get("path", "")
    with _media_lock:
        S["media_calls"][path] = S["media_calls"].get(path, 0) + 1
        S["media_inflight"] += 1
        S["media_max_inflight"] = max(S["media_max_inflight"], S["media_inflight"])
        spec = S["media"].get(path, {"status": 200})
        busy = spec.get("busy_times", 0)
        if busy and S["media_calls"][path] <= busy:
            spec = {"status": 503}
    try:
        time.sleep(S["media_delay"])
        if spec["status"] == 200:
            return app.response_class(TINY_WEBP, mimetype="image/webp")
        return jsonify(error="x"), spec["status"]
    finally:
        with _media_lock:
            S["media_inflight"] -= 1


@app.get("/api/messages/received")
def messages_received():
    return jsonify(messages=S["received"])


@app.get("/api/messages/sent")
def messages_sent():
    return jsonify(messages=S["sent"])


@app.post("/api/messages")
def messages_post():
    files = [{"name": f.filename, "type": f.mimetype, "head": f.read(4).decode("latin1"), "size": f.seek(0, 2)}
             for f in request.files.getlist("attachments")] if request.files else []
    S["message_posts"].append({"content_type": request.content_type.split(";")[0],
                               "form": dict(request.form), "files": files})
    return jsonify(ok=True, id="new")


@app.post("/api/messages/<mid>/<action>")
def message_action(mid, action):
    if action not in ("cancel", "retry"):
        return jsonify(error="no"), 404
    S["msg_actions"].append(f"{action}:{mid}")
    if S["msg_refuse"]:
        return jsonify(ok=False, error=S["msg_refuse"]), 409
    for m in S["sent"]:
        if m["id"] == mid:
            m["state"] = "cancelled" if action == "cancel" else "queued"
    return jsonify(ok=True)


@app.get("/api/contacts")
def contacts():
    return jsonify(contacts=S["contacts"])


@app.post("/api/contacts/<h>/settings")
def contact_settings(h):
    body = request.get_json(silent=True) or {}
    S["settings_posts"].append({"hash": h, **body})
    for c in S["contacts"]:
        if c["hash"] == h:
            c.update(body)
            return jsonify(ok=True, contact=c)
    c = {"hash": h, "name": h[:16], **body}
    S["contacts"].append(c)
    return jsonify(ok=True, contact=c)


@app.route("/api/<path:rest>", methods=["GET", "POST", "PUT", "DELETE"])
def fallback(rest):
    return jsonify([]) if request.method == "GET" else jsonify({})


if __name__ == "__main__":
    app.run(port=int(sys.argv[1]), threaded=True)

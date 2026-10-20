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
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admin_mock  # noqa: E402  (sits beside this file)
import chrome_data  # noqa: E402

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
    "chrome": None,        # dev-only: "demo|worst|empty|one|huge" data for the interface around the page
    "admin_role": "admin", # who the real /admin pages see: "admin", "user", "anon", or "real" (use the real session)
    "admin_env_password": "",   # turns the ADMIN_PASSWORD override on
    "admin_account": None, # {"username", "password"}: start with the stored admin account set up
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
}
S = dict(DEFAULTS)
admin_mock.mount(app, lambda: S)


DATA_KINDS = ("demo", "worst", "empty", "one", "huge")
TOGGLE_JS = """<script>(function(){var k=%r,kinds=%r;var b=document.createElement('div');
b.style.cssText='position:fixed;left:50%%;bottom:8px;transform:translateX(-50%%);z-index:99999;display:flex;gap:2px;padding:3px;background:#ddd;border-radius:8px;font:12px system-ui;box-shadow:0 1px 4px #0006';
kinds.forEach(function(n){var a=document.createElement('a');a.textContent=n;a.href='?data='+n;
a.style.cssText='padding:4px 10px;border-radius:6px;text-decoration:none;color:#222;'+(n===k?'background:#fff;font-weight:600':'');b.appendChild(a)});
document.body.appendChild(b)})()</script>"""


@app.get("/")
def index():
    """Dev-only: ``?data=worst|demo|empty|one|huge`` picks the data the
    interface around the page is drawn from, with a small switcher."""
    kind = request.args.get("data")
    if kind in DATA_KINDS:
        S["chrome"] = kind
    html = render_template("index.html")
    if S.get("chrome"):
        html = html.replace("</body>", TOGGLE_JS % (S["chrome"], list(DATA_KINDS)) + "</body>")
    return html


def _kind():
    return S.get("chrome") or "demo"


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
    admin_mock.reset(app)
    return jsonify(ok=True)


@app.get("/__setup_code")
def setup_code():
    """The first-run code, as the operator would read it from the file."""
    path = app.config["ADMIN_ACCOUNT"].setup_code_path
    return jsonify(code=open(path).read().strip() if os.path.exists(path) else None)


@app.post("/__admin_seed")
def admin_seed():
    """Dev-only: fill the admin pages with worst-case rows."""
    admin_mock.seed_worst(app)
    return jsonify(ok=True)


@app.get("/__stats")
def stats():
    return jsonify(fetches=S["fetches"], refresh_flag=S.get("last_refresh_flag"),
                   path_fetches=S["path_fetches"], media_calls=S["media_calls"],
                   media_max_inflight=S["media_max_inflight"])


@app.get("/api/auth/status")
def auth():
    if S.get("chrome"):
        return jsonify(logged_in=True, is_admin=True, super_admin=False,
                       user={"name": chrome_data.user_name(_kind()), "email": ""},
                       login_url="/auth/start", logout_url="/auth/logout", admin_url="/admin")
    return jsonify(logged_in=S["admin"], is_admin=S["admin"], user="admin" if S["admin"] else None)


@app.get("/api/ui/settings")
def ui():
    if S["ui_fail"]:
        return jsonify(error="down"), 503
    d = {}
    if S.get("chrome"):
        d.update(chrome_data.ui(_kind()))
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



@app.get("/api/nodes")
def api_nodes_chrome():
    return jsonify(nodes=chrome_data.nodes(_kind())) if S.get("chrome") else jsonify([])


@app.get("/api/lxmf-peers")
def api_peers_chrome():
    return jsonify(peers=chrome_data.peers(_kind())) if S.get("chrome") else jsonify([])


@app.get("/api/relays")
def api_relays_chrome():
    return jsonify(relays=chrome_data.relays(_kind())) if S.get("chrome") else jsonify([])


@app.get("/api/contacts")
def api_contacts_chrome():
    return jsonify(contacts=chrome_data.contacts(_kind()) if S.get("chrome") else [])


@app.get("/api/messages/received")
def api_received_chrome():
    return jsonify(messages=chrome_data.messages(_kind())[0] if S.get("chrome") else [])


@app.get("/api/messages/sent")
def api_sent_chrome():
    return jsonify(messages=chrome_data.messages(_kind())[1] if S.get("chrome") else [])


@app.route("/api/<path:rest>", methods=["GET", "POST", "PUT", "DELETE"])
def fallback(rest):
    return jsonify([]) if request.method == "GET" else jsonify({})


if __name__ == "__main__":
    app.run(port=int(sys.argv[1]), threaded=True)

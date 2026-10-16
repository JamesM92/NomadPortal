"""The single-page shell, made visible to search engines and link previews.

Every page URL serves the same shell (``index.html``); the browser's
JavaScript takes over and shows the page. That leaves crawlers and link
previews, which often don't run JavaScript, with an empty page. So when
the URL names one of the operator's own pages, the page's content is
rendered into the shell on the server, along with a title, description,
canonical link and sitemap entry.

What is rendered is deliberately narrow:

- only the operator's own nodes (the hosted site and the prescribed
  nodes) and only viewers the access policy lets browse them;
- for the hosted site, static pages only. Executable pages are scripts;
  running one for a crawler, then again when the visitor's browser takes
  over, would double its side effects (hit counters, guestbooks);
- for remote prescribed nodes, the saved copy only. The response never
  waits on the mesh.

Anything else gets the plain shell, as before.
"""

from flask import Response, abort, current_app, render_template, request

from . import seo
from .access import BROWSE
from .kiosk_cache import kiosk_key
from .routes import _kiosk_cache, _policy, _render_micron, _render_title_plain

_RESERVED_PREFIXES = ("api/", "admin/", "auth/", "static/")


def _hosted_hash() -> str:
    site = current_app.config.get("SITE_SERVER")
    return (site.node_hash() or "").lower() if site and site.node_hash() else ""


def _site_name(settings: dict) -> str:
    name = (settings.get("site_name") or "").strip()
    return name or _render_title_plain(settings.get("app_title", ""))


def _settings() -> dict:
    ui = current_app.config.get("UI_SETTINGS")
    return ui.get_all() if ui else {}


def _base_url() -> str:
    """The site's public address, used in canonical links, the sitemap
    and robots.txt. ``PUBLIC_URL`` pins it (recommended for a public
    site, and immune to a forged Host header); otherwise it is taken from
    the request, which is only right behind a proxy if TRUSTED_PROXIES
    is set."""
    pinned = (current_app.config.get("PUBLIC_URL") or "").strip().rstrip("/")
    return pinned or request.url_root.rstrip("/")


def _page_context(pathname: str):
    """What to put in the shell for this URL, or None for a plain shell."""
    policy = _policy()
    hosted = _hosted_hash()
    landing = policy.landing
    resolved = seo.resolve_pathname(pathname, landing, hosted or None)
    if resolved is None:
        return None
    node, path = resolved
    if not path.startswith("/page/"):
        return None
    if not policy.is_trusted(node) or policy.check(node, BROWSE) is not None:
        # Not ours to publish (or not allowed): keep it out of search results.
        return {"noindex": True}

    ctx = {"canonical": _base_url() + seo.pathname_for(node, path, landing, hosted or None)}
    settings = _settings()
    ctx["title"] = _site_name(settings)

    content = None
    if node == hosted:
        site = current_app.config.get("SITE_SERVER")
        content = site.read_static_page(path) if site else None
    else:
        kiosk = _kiosk_cache()
        entry = kiosk.get(kiosk_key(node, path)) if kiosk is not None else None
        content = entry.content if entry is not None else None
    if content is None:
        return ctx

    text = content.decode("utf-8", errors="replace")
    html = _render_micron(text, node, path, False)
    ctx["html"] = html
    ctx["title"] = seo.page_title(text, _site_name(settings), _render_title_plain)
    ctx["description"] = seo.page_description(html)
    return ctx


def _shell(pathname: str):
    return render_template("index.html", seo=_page_context(pathname))


def sitemap_paths() -> list:
    """URL paths of the operator's own pages, landing page first."""
    policy = _policy()
    hosted = _hosted_hash()
    landing = policy.landing
    browser = current_app.config.get("BROWSER")
    nodes = []
    for n in ([landing] if landing else []) + ([hosted] if hosted else []) + list(policy.prescribed):
        n = (n or "").lower()
        if n and n not in nodes and not (browser is not None and browser.is_blocked(n)):
            nodes.append(n)

    out = []
    kiosk = _kiosk_cache()
    cached_keys = kiosk.keys() if kiosk is not None else []
    site = current_app.config.get("SITE_SERVER")
    for node in nodes:
        out.append(seo.pathname_for(node, seo.INDEX_PATH, landing, hosted or None))
        if node == hosted and site is not None:
            for p in site.static_page_paths():
                out.append(seo.pathname_for(node, p, landing, hosted or None))
        prefix = f"{node}:"
        for key in sorted(k for k in cached_keys if k.startswith(prefix)):
            path = key[len(prefix):]
            if path.startswith("/page/"):
                out.append(seo.pathname_for(node, path, landing, hosted or None))
    return out


def register_spa_routes(app) -> None:
    @app.get("/")
    def index():
        return _shell("/")

    @app.get("/page")
    def page_view():
        # Legacy ``?url=`` entry point: a shell with nothing of its own.
        return render_template("index.html", seo={"noindex": True})

    @app.get("/robots.txt")
    def robots():
        return Response(seo.robots_txt(_base_url()), mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap():
        return Response(seo.sitemap_xml(_base_url(), sitemap_paths()),
                        mimetype="application/xml")

    # Catch-all so the SPA owns the URL bar: /page/foo.mu, /file/x.pdf and
    # /n/<hash>/... all serve the shell and the JS navigates. A path under
    # a backend prefix that reached here matched no specific route, so it
    # is a genuine 404, not the SPA.
    @app.get("/<path:_path>")
    def spa_fallback(_path):
        if _path.startswith(_RESERVED_PREFIXES):
            abort(404)
        return _shell("/" + _path)

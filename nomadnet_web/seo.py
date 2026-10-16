"""Helpers that make the public site visible to search engines and link
previews: URL mapping that mirrors the browser's, page titles and
descriptions, the sitemap and robots.txt.

Pure functions with no Flask or Reticulum dependency. The browser's own
URL scheme lives in ``static/js/app.js`` (``_pathnameToUrl`` and
``_urlToPathname``); ``resolve_pathname`` and ``pathname_for`` mirror it
so the server and the browser always agree on what a URL means.
"""

import html as _html
import re
from typing import Callable, Iterable, Optional
from xml.sax.saxutils import escape as _xml_escape

DEFAULT_SITE_NAME = "NomadPortal"
INDEX_PATH = "/page/index.mu"

# The sitemap protocol allows 50,000 URLs per file.
SITEMAP_MAX_URLS = 50000

_EXPLICIT_NODE_RE = re.compile(r"^/n/([0-9a-fA-F]{2,128})(/.*)?$")
_TAG_RE = re.compile(r"<[^>]*>")
_SPACE_RE = re.compile(r"\s+")


def _is_index(path: str) -> bool:
    return path in ("", "/", "/index.mu", INDEX_PATH)


def resolve_pathname(pathname: str, landing: Optional[str], hosted: Optional[str]):
    """Which (node, page path) does this URL path refer to?

    Mirrors the browser: ``/`` is the landing node's home (or the hosted
    site's when there is no landing node); ``/n/<hash>[/path]`` names a
    node explicitly; any other path belongs to the landing node.
    Returns None when the URL cannot be resolved."""
    if not pathname.startswith("/"):
        pathname = "/" + pathname
    if pathname == "/":
        node = landing or hosted
        return (node.lower(), INDEX_PATH) if node else None
    m = _EXPLICIT_NODE_RE.match(pathname)
    if m:
        return m.group(1).lower(), (m.group(2) or INDEX_PATH)
    if pathname in ("/page", "/page/"):
        return None
    return (landing.lower(), pathname) if landing else None


def pathname_for(node: str, path: str, landing: Optional[str], hosted: Optional[str]) -> str:
    """The canonical URL path for a page; the inverse of resolve_pathname."""
    node = node.lower()
    path = path or INDEX_PATH
    if landing and node == landing.lower():
        return "/" if _is_index(path) else path
    if not landing and hosted and node == hosted.lower() and _is_index(path):
        return "/"
    return f"/n/{node}" + ("" if _is_index(path) else path)


def page_title(micron_text: str, site_name: str, plain: Callable[[str], str]) -> str:
    """'<first heading> · <site name>', or just the site name. ``plain``
    turns a line of Micron markup into plain text (the caller supplies
    the app's own converter)."""
    site = (site_name or "").strip() or DEFAULT_SITE_NAME
    for line in (micron_text or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">"):
            heading = plain(stripped.lstrip(">").strip()).strip()
            if heading:
                return f"{heading} · {site}"
            break
    return site


def page_description(rendered_html: str, limit: int = 160) -> str:
    """Plain-text summary of a rendered page, cut at a word boundary."""
    text = _SPACE_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", rendered_html or ""))).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-")
    return cut + "…"


def sitemap_xml(base_url: str, pathnames: Iterable[str]) -> str:
    base = base_url.rstrip("/")
    seen, urls = set(), []
    for p in pathnames:
        if p not in seen:
            seen.add(p)
            urls.append(p)
        if len(urls) >= SITEMAP_MAX_URLS:
            break
    body = "".join(f"<url><loc>{_xml_escape(base + p)}</loc></url>" for p in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + body + "</urlset>")


def robots_txt(base_url: str) -> str:
    return (
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /admin/\n"
        "Disallow: /auth/\n"
        f"Sitemap: {base_url.rstrip('/')}/sitemap.xml\n"
    )

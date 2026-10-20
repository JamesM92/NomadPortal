"""Make the server-rendered copy of a page complete.

The version of a page sent to crawlers and to browsers without JavaScript has
no script to fetch images or partials afterwards (app.js does that for
everyone else, and replaces this copy when it starts). So here the images get
a real address to load from, and each partial that needs no form input is
replaced by its content, looked up by the caller (only pages the portal
already holds: the hosted site or the saved copies).

Pure functions over the HTML the converter produced; no Flask, no network.
"""

import html as _html
import re
import urllib.parse
from typing import Callable, Optional

MAX_PARTIALS = 10          # same as app.js
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_IMG = re.compile(r'<img\b(?P<attrs>[^>]*\bdata-media-hash="(?P<hash>[0-9a-f]{32})"'
                  r'[^>]*\bdata-media-path="(?P<path>[^"]*)"[^>]*)>')
_DYN = re.compile(r'<a\b(?P<attrs>[^>]*\bclass="mu-dynamic"[^>]*)>(?P<label>.*?)</a>', re.S)
_ATTR = re.compile(r'\b([a-z-]+)="([^"]*)"')
_TARGET = re.compile(r"^hash://([0-9a-f]{32})(/.*)?$", re.I)


def add_image_sources(html: str) -> str:
    def fix(m):
        if re.search(r"\ssrc=", m.group("attrs")):
            return m.group(0)
        path = _html.unescape(m.group("path"))
        src = f"/api/media?hash={m.group('hash')}&amp;path={urllib.parse.quote(path, safe='')}"
        return f'<img src="{src}"{m.group("attrs")}>'
    return _IMG.sub(fix, html)


def _attrs(text: str) -> dict:
    return {k: _html.unescape(v) for k, v in _ATTR.findall(text)}


def inline_partials(html: str, load: Callable[[str, str], Optional[str]],
                    max_partials: int = MAX_PARTIALS) -> str:
    """Replace partial links with their content. ``load(node, path)`` returns
    the content's HTML, or None to leave that partial as a plain link."""
    used = 0

    def fix(m):
        nonlocal used
        if used >= max_partials:
            return m.group(0)
        a = _attrs(m.group("attrs"))
        href = a.get("href", "")
        if not href.startswith("/page?url="):
            return m.group(0)
        target = _TARGET.match(urllib.parse.unquote(href[len("/page?url="):]))
        if not target:
            return m.group(0)
        # Only a bare ``pid=`` entry may be present; anything else is form
        # input the page expects, which a crawler cannot give.
        fields = [f for f in a.get("data-fields", "").split("|") if f]
        if any(not f.startswith("pid=") for f in fields):
            return m.group(0)
        try:
            inner = load(target.group(1).lower(), target.group(2) or "/page/index.mu")
        except Exception:
            return m.group(0)
        if inner is None:
            return m.group(0)
        used += 1
        pid = a.get("data-pid", "")
        pid_attr = f' data-pid="{pid}"' if _SAFE_ID.fullmatch(pid) else ""
        return f'<div class="mu-partial"{pid_attr}>{inner}</div>'

    return _DYN.sub(fix, html)

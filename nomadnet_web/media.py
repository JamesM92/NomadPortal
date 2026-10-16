"""NomadNet inline images.

A page shows an image with a block-level tag on a line of its own:

    `(alt text`w=n`h=10`a=c`:/media/logo.webp)

``w`` and ``h`` are the width and height (terminal cells, a percentage,
or ``n`` for the image's own size), and ``a`` is the alignment (``l``,
``c`` or ``r``). The URL is a ``/media/`` path on this node or on
another (``<hash>:/media/x.webp``). Over Reticulum a node serves media
through a ``/media`` request handler that takes ``{"path": ..., "key":
None}`` and answers with the image (WebP on the wire).

The Micron2HTML library does not know this tag, so it is handled here in
two passes around the library's own conversion: image lines are swapped
for unmistakable placeholder lines, the page is converted as usual (which
keeps colour and alignment state intact across the image), and each
placeholder line is then replaced with a ``<figure>``.

The ``<img>`` has no ``src``. app.js fetches each image through
``/api/media`` (which applies the access policy, limits and checks the
bytes) and fills it in, so nothing is requested without the script and
the content security policy is unchanged.
"""

import html as _html
import re
import secrets
from dataclasses import dataclass
from typing import Callable, Optional

from micron2html import default_url_resolver

# Largest image the portal will fetch or serve. WebP is compact; a bigger
# one is more likely a mistake or an attack than a page decoration.
MAX_MEDIA_BYTES = 2 * 1024 * 1024
MAX_PATH_LEN = 512

_SIZE_RE = re.compile(r"^\d{1,6}%?$")
_ALIGN = {"l": "left", "c": "center", "r": "right"}


@dataclass(frozen=True)
class ImageTag:
    alt: str
    url: str
    width: Optional[str] = None
    height: Optional[str] = None
    align: Optional[str] = None


def parse_image_tag(rest: str) -> Optional[ImageTag]:
    """Parse the part of an image line after the leading ``(`` marker.
    Mirrors NomadNet's parser: the last backtick field is the URL, the
    first is the alt text, and the fields between are ``key=value``."""
    end = rest.rfind(")")
    if end <= 0:
        return None
    fields = rest[:end].split("`")
    if len(fields) < 2:
        return None
    alt = fields[0].strip()
    url = fields[-1].strip()
    if not url:
        return None
    width = height = align = None
    for prop in fields[1:-1]:
        if "=" not in prop:
            continue
        key, _, value = prop.partition("=")
        key, value = key.strip(), value.strip()
        if key == "w":
            width = value
        elif key == "h":
            height = value
        elif key == "a":
            align = _ALIGN.get(value, value if value in _ALIGN.values() else None)
    return ImageTag(alt=alt, url=url, width=width, height=height, align=align)


def token(nonce: str, index: int) -> str:
    return f"@@IMG{nonce}-{index}@@"


def replace_image_lines(text: str, nonce: str):
    """Swap each image line for a placeholder line. Returns the new text
    and the list of tags found, in document order. Lines inside a literal
    block (between ``\\`=`` lines) or a table (between ``\\`t`` lines) are
    left alone, as NomadNet leaves them."""
    out, tags = [], []
    literal = table = False
    for line in text.split("\n"):
        bare = line.rstrip("\r")
        if bare == "`=":
            literal = not literal
        elif not literal and bare.startswith("`t"):
            table = not table
        elif not literal and not table and bare.startswith("`("):
            tag = parse_image_tag(bare[2:])
            if tag is not None:
                out.append(token(nonce, len(tags)))
                tags.append(tag)
                continue
        out.append(line)
    return "\n".join(out), tags


def media_target(url: str, node_hash: str, base_path: str):
    """``(node_hash, "/media/..." path)`` for an image URL, or None when
    it is not a plain media path on a Reticulum node (web URLs, other
    prefixes, traversal, overlong paths)."""
    if not url:
        return None
    canonical = default_url_resolver(url, node_hash, base_path)
    if not canonical.startswith("hash://"):
        return None
    rest = canonical[len("hash://"):]
    target, sep, path = rest.partition("/")
    path = "/" + path if sep else "/"
    if not re.fullmatch(r"[0-9a-f]{2,128}", target):
        return None
    if (not path.startswith("/media/") or len(path) > MAX_PATH_LEN
            or ".." in path.split("/") or path.endswith("/")):
        return None
    return target, path


def _clamped(value: Optional[str], low: int, high: int, percent_ok: bool):
    """Numeric part of a size, or None. ``n`` and anything odd means
    'natural size'."""
    if not value or not _SIZE_RE.match(value):
        return None
    is_percent = value.endswith("%")
    if is_percent and not percent_ok:
        return None
    n = max(low, min(high, int(value.rstrip("%"))))
    return n, is_percent


def _style(tag: ImageTag) -> str:
    parts = ["max-width:100%"]
    w = _clamped(tag.width, 1, 200, True)
    if w:
        n, pct = w
        parts.append(f"width:{min(n, 100)}%" if pct else f"width:{n}ch")
    h = _clamped(tag.height, 1, 100, False)
    if h:
        parts.append(f"height:{h[0] * 1.3:.1f}em")
    return ";".join(parts)


def figure_html(tag: ImageTag, target, can_load: Callable[[str], bool]) -> str:
    cls = "mu-image" + (f" mu-image-{tag.align}" if tag.align else "")
    label = tag.alt or (tag.url.rsplit("/", 1)[-1] or "image")
    caption = f'<figcaption class="mu-image-alt">{_html.escape(label)}</figcaption>'
    if target is None or not can_load(target[0]):
        return f'<figure class="{cls} mu-image-unavailable">{caption}</figure>'
    node, path = target
    img = (
        f'<img class="mu-image-el" alt="{_html.escape(label, quote=True)}" '
        f'data-media-hash="{node}" data-media-path="{_html.escape(path, quote=True)}" '
        f'style="{_style(tag)}" loading="lazy" decoding="async">'
    )
    return f'<figure class="{cls}">{img}{caption}</figure>'


def render_with_images(
    converter,
    text: str,
    *,
    node_hash: str,
    base_path: str,
    authenticated: bool,
    can_load: Callable[[str], bool],
) -> str:
    """Convert Micron to HTML, with image lines rendered as figures."""
    nonce = secrets.token_hex(4)
    replaced, tags = replace_image_lines(text, nonce)
    if not tags:
        return converter.convert(text, node_hash=node_hash, base_path=base_path,
                                 authenticated=authenticated)
    body = converter.convert(replaced, node_hash=node_hash, base_path=base_path,
                             authenticated=authenticated)
    for i, tag in enumerate(tags):
        fig = figure_html(tag, media_target(tag.url, node_hash, base_path), can_load)
        pattern = (r'<div class="mu-line[^"]*"[^>]*>(?:(?!</div>).)*?'
                   + re.escape(token(nonce, i)) + r'(?:(?!</div>).)*?</div>')
        body = re.sub(pattern, lambda _m, f=fig: f, body, count=1, flags=re.DOTALL)
    return body


def is_webp(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def sniff_image_type(data: bytes) -> Optional[str]:
    """MIME type from the bytes themselves, for the formats a browser can
    draw safely. Anything else (SVG included) is None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if is_webp(data):
        return "image/webp"
    return None

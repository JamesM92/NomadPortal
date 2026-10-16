"""The NomadNet page cache directive.

A page can begin with ``#!c=<seconds>`` to tell readers how long a copy
stays fresh. NomadNet's own browser caches a page for that long (twelve
hours when there is no directive) and re-fetches after. ``#!c=0`` means
"do not treat a copy as fresh".

Here the directive controls only freshness, never availability: a page
inside its window is shown from the saved copy without asking the mesh,
and a page outside it (or with ``#!c=0``) is refreshed behind the saved
copy. Saved copies are still kept for outages either way.
"""

import re
from typing import Optional, Union

# Longest freshness window honoured: 30 days.
MAX_SECONDS = 30 * 86400

# How far down a page a header directive may appear.
_HEADER_LINES = 20

_DIRECTIVE_RE = re.compile(r"^#!c=(\d+)\s*$")


def parse_cache_seconds(data: Union[bytes, str]) -> Optional[int]:
    """Seconds from the page's ``#!c=N`` header, 0 for "never fresh", or
    None when the page has no valid directive."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    if not data:
        return None
    text = data.lstrip("﻿")
    for line in text.splitlines()[:_HEADER_LINES]:
        m = _DIRECTIVE_RE.match(line.strip())
        if m:
            return min(int(m.group(1)), MAX_SECONDS)
    return None

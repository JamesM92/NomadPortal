"""Background pre-warming of the kiosk cache.

A first-time visitor to a page with no saved copy waits on the mesh. The
pre-warmer removes most of those waits: on a schedule it fetches each
prescribed node's landing page and follows same-node links a couple of
hops, storing every page in the kiosk cache at low priority.

It is deliberately gentle. One page at a time, a pause between fetches,
a hard page limit per node, and it stands down at once if the mesh isn't
up. Pre-warmed pages are the first to be evicted when the cache is full,
and never replace a page a visitor asked for with a lower priority.

Nothing here imports Reticulum or Flask: the fetch function, the cache
and the node list are handed in, so it is tested with fakes.
"""

import logging
import re
import threading
from typing import Callable, Iterable, Optional

from micron2html import default_url_resolver

from .kiosk_cache import PRIORITY_PREWARM, kiosk_key

log = logging.getLogger(__name__)

LANDING_PATH = "/page/index.mu"

# `[label`destination]  or  `[label`destination`field spec]
_LINK_RE = re.compile(r"`\[([^`\]]*)`([^`\]]*)(`[^\]]*)?\]")

_NOT_READY_PREFIX = "Reticulum transport is still coming up"


def extract_page_links(micron_text: str, node_hash: str, base_path: str) -> list:
    """Paths of same-node pages linked from a Micron page, in first-seen
    order. Links that carry form fields or a query string are skipped
    (they are actions, not pages), as are file, web and other-node links."""
    prefix = f"hash://{node_hash.lower()}"
    seen, out = set(), []
    for _label, dest, fields in _LINK_RE.findall(micron_text or ""):
        if fields or not dest:
            continue
        canonical = default_url_resolver(dest, node_hash, base_path)
        if not canonical.startswith(prefix + "/page/") or "?" in canonical:
            continue
        path = canonical[len(prefix):]
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


class PreWarmer:
    def __init__(
        self,
        fetch: Callable[[str, str], tuple],
        cache,
        nodes: Callable[[], Iterable[str]],
        *,
        depth: int = 2,
        max_pages: int = 25,
        gap_s: float = 2.0,
        interval_s: float = 3600.0,
        initial_delay_s: float = 30.0,
    ):
        self._fetch = fetch
        self._cache = cache
        self._nodes = nodes
        self._depth = depth
        self._max_pages = max_pages
        self._gap_s = gap_s
        self._interval_s = interval_s
        self._initial_delay_s = initial_delay_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- control ---------------------------------------------------------

    def start(self) -> None:
        """Begin the background schedule. An interval of zero disables it."""
        if self._interval_s <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="kiosk-prewarm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._wait(self._initial_delay_s):
            return
        while True:
            try:
                stats = self.run_once()
                log.info("Kiosk pre-warm: %d fetched, %d failed",
                         stats["fetched"], stats["failed"])
            except Exception:
                log.exception("Kiosk pre-warm run failed")
            if self._wait(self._interval_s):
                return

    def _wait(self, seconds: float) -> bool:
        """Sleep, but wake early when stopped. True means 'stop now'."""
        return self._stop.wait(seconds)

    # -- one pass --------------------------------------------------------

    def run_once(self) -> dict:
        stats = {"fetched": 0, "failed": 0}
        first = True
        for node in self._nodes():
            node = (node or "").strip().lower()
            if not node:
                continue
            outcome = self._walk(node, stats, first)
            first = False
            if outcome == "stop":
                break
        return stats

    def _walk(self, node: str, stats: dict, first: bool) -> str:
        """Breadth-first from the landing page. Returns 'stop' when the
        whole run should end (stopped, or the mesh isn't up)."""
        queue = [(LANDING_PATH, 0)]
        visited = {LANDING_PATH}
        attempted = 0
        while queue and attempted < self._max_pages:
            path, level = queue.pop(0)
            if not first and self._wait(self._gap_s):
                return "stop"
            first = False
            attempted += 1
            content, error = self._fetch(node, path)
            if error or not content:
                stats["failed"] += 1
                if error and error.startswith(_NOT_READY_PREFIX):
                    return "stop"
                if path == LANDING_PATH:
                    return "next"        # unreachable node: skip its links
                continue
            self._cache.set(kiosk_key(node, path), content, priority=PRIORITY_PREWARM)
            stats["fetched"] += 1
            if level < self._depth:
                text = content.decode("utf-8", errors="replace")
                for link_path in extract_page_links(text, node, path):
                    if link_path not in visited:
                        visited.add(link_path)
                        queue.append((link_path, level + 1))
        return "next"

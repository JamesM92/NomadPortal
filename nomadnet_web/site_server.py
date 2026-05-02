"""
NomadNet node server.

Serves pages and files from a local directory over the Reticulum network,
making this instance a first-class NomadNet node that any NomadNet client
can browse.

Pages live in <pages_dir>/ and are served at request path /page/<filename>.
Files live in <files_dir>/ and are served at request path /file/<filename>.
Sub-directories are supported; they are served at their relative path.

The node identity is persisted to <identity_file> so the destination hash
stays constant across restarts.
"""

import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

ANNOUNCE_INTERVAL  = 6 * 60 * 60  # re-announce every 6 hours
RESCAN_INTERVAL    = 5  * 60   # re-scan pages/files every 5 minutes
START_ANNOUNCE_DELAY = 6        # seconds after start before first announce

_DEFAULT_INDEX = """>Welcome

This node is serving pages, but no `*index.mu`* was found in the pages directory.

If you are the node operator, create a file named `*index.mu`* in the pages directory to customise this page.
"""


class SiteServer:
    """Hosts a NomadNet node, serving pages and files over Reticulum."""

    def __init__(
        self,
        pages_dir: str,
        files_dir: str,
        identity_file: str,
        node_name: str = "NomadPortal",
    ):
        self._pages_dir     = pages_dir
        self._files_dir     = files_dir
        self._identity_file = identity_file
        self._node_name     = node_name
        self._dest          = None
        self._identity      = None
        self._node_hash: Optional[str] = None
        self._last_announce = 0.0
        self._last_rescan   = 0.0
        self._running       = False

    def start(self) -> str:
        """Start the node server. Returns the destination hexhash."""
        import RNS

        os.makedirs(self._pages_dir, exist_ok=True)
        os.makedirs(self._files_dir, exist_ok=True)

        # Load or create the persistent node identity
        if os.path.exists(self._identity_file):
            self._identity = RNS.Identity.from_file(self._identity_file)
            log.info("Loaded site identity from %s", self._identity_file)
        else:
            self._identity = RNS.Identity()
            self._identity.to_file(self._identity_file)
            log.info("Created new site identity → %s", self._identity_file)

        # Register the nomadnetwork.node destination
        self._dest = RNS.Destination(
            self._identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            "nomadnetwork",
            "node",
        )
        self._dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
        self._dest.set_link_established_callback(self._peer_connected)

        self._node_hash = self._dest.hexhash

        self._register_pages()
        self._register_files()

        log.info(
            "Site node ready — hash %s, name %r",
            self._node_hash[:16], self._node_name,
        )

        # Announce shortly after start and then on a timer
        self._running = True
        t = threading.Thread(target=self._background_jobs, daemon=True)
        t.start()

        return self._node_hash

    def node_hash(self) -> Optional[str]:
        return self._node_hash

    def node_name(self) -> str:
        return self._node_name

    def fetch_page(
        self,
        path: str,
        local_identity_hex: str = "",
        field_data: Optional[dict] = None,
    ) -> tuple:
        """Serve a page directly from the filesystem (bypasses RNS link).

        Returns (content_bytes, error_str) — exactly one will be None.
        path should be the page path, e.g. '/index.mu' or '/page/index.mu'.

        `local_identity_hex` (optional) is the logged-in NomadPortal user's
        RNS identity hex. When provided, executable pages see it as
        `remote_identity` so they can render the user's fingerprint even
        though no Reticulum link is in play.

        `field_data` (optional) is a dict of `field_*` / `var_*` values to
        expose as env vars to executable pages. Lets local form submissions
        round-trip without going over Reticulum.
        """
        # Normalise to bare filename (strip /page/ prefix if present)
        p = path.strip("/")
        if p.startswith("page/"):
            p = p[len("page/"):]
        if not p:
            p = "index.mu"

        file_path = os.path.realpath(os.path.join(self._pages_dir, p))
        pages_root = os.path.realpath(self._pages_dir)
        if not file_path.startswith(pages_root + os.sep) and file_path != pages_root:
            return None, "Invalid path"
        if not os.path.isfile(file_path):
            return None, f"Page not found: {p}"

        try:
            if not _is_windows() and os.access(file_path, os.X_OK):
                import subprocess
                # _build_env only forwards keys prefixed with `field_` /
                # `var_` to the executable's env. Over-RNS requests come
                # through browser.fetch_page which already prefixes; local
                # form submits arrive with bare keys (`action`, `username`,
                # …) so we apply the same prefix here for parity.
                norm_data = None
                if field_data:
                    norm_data = {}
                    for k, v in field_data.items():
                        if k.startswith("field_") or k.startswith("var_"):
                            norm_data[k] = v
                        else:
                            norm_data[f"field_{k}"] = v
                result = subprocess.run(
                    [file_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=_build_env(
                        None,
                        local_identity_hex or None,
                        norm_data,
                        node_destination=self._node_hash,
                    ),
                )
                return result.stdout, None
            with open(file_path, "rb") as fh:
                return fh.read(), None
        except Exception as exc:
            log.error("Error serving local page %s: %s", file_path, exc)
            return None, str(exc)

    def announce(self) -> None:
        if self._dest is None:
            return
        try:
            self._dest.announce(app_data=self._node_name.encode("utf-8"))
            self._last_announce = time.time()
            log.info("Site node announced (%s)", self._node_hash[:16] if self._node_hash else "?")
        except Exception as exc:
            log.warning("Site announce failed: %s", exc)

    # ------------------------------------------------------------------
    # Page / file registration  (mirrors NomadNet's Node.register_pages)
    # ------------------------------------------------------------------

    def _register_pages(self) -> None:
        if self._dest is None:
            return

        pages: list[str] = []
        self._scan_dir(self._pages_dir, pages)

        # Register a default index if none exists
        has_index = any(p.endswith("/index.mu") or p.endswith(os.sep + "index.mu") for p in pages)
        root_index = os.path.join(self._pages_dir, "index.mu")
        if not has_index and not os.path.isfile(root_index):
            self._dest.register_request_handler(
                "/page/index.mu",
                response_generator=self._serve_default_index,
                allow=self._dest.ALLOW_ALL,
            )

        for full_path in pages:
            rel = full_path[len(self._pages_dir):]
            request_path = "/page" + rel.replace(os.sep, "/")
            try:
                self._dest.register_request_handler(
                    request_path,
                    response_generator=self._serve_page,
                    allow=self._dest.ALLOW_ALL,
                )
            except Exception as exc:
                log.debug("Could not register page %s: %s", request_path, exc)

        self._last_rescan = time.time()
        log.debug("Registered %d page(s) from %s", len(pages), self._pages_dir)

    def _register_files(self) -> None:
        if self._dest is None:
            return

        files: list[str] = []
        self._scan_dir(self._files_dir, files)

        for full_path in files:
            rel = full_path[len(self._files_dir):]
            request_path = "/file" + rel.replace(os.sep, "/")
            try:
                self._dest.register_request_handler(
                    request_path,
                    response_generator=self._serve_file,
                    allow=self._dest.ALLOW_ALL,
                    auto_compress=32_000_000,
                )
            except Exception as exc:
                log.debug("Could not register file %s: %s", request_path, exc)

        log.debug("Registered %d file(s) from %s", len(files), self._files_dir)

    def _scan_dir(self, base: str, result: list) -> None:
        if not os.path.isdir(base):
            return
        for entry in os.listdir(base):
            if entry.startswith("."):
                continue
            full = os.path.join(base, entry)
            if os.path.isfile(full) and not entry.endswith(".allowed"):
                result.append(full)
            elif os.path.isdir(full):
                self._scan_dir(full, result)

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    def _peer_connected(self, link) -> None:
        log.debug("Peer connected to site node")

    def _serve_page(self, path, data, request_id, link_id, remote_identity, requested_at):
        file_path = path.replace("/page", self._pages_dir, 1)
        log.debug("Page request: %s → %s", path, file_path)
        try:
            if not os.path.isfile(file_path):
                return b">Page Not Found\n\nThe requested page does not exist."

            # Executable pages: run as a script and return stdout
            if not _is_windows() and os.access(file_path, os.X_OK):
                env = _build_env(link_id, remote_identity, data,
                                 node_destination=self._node_hash)
                import subprocess
                result = subprocess.run(
                    [file_path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
                )
                return result.stdout

            with open(file_path, "rb") as fh:
                return fh.read()

        except Exception as exc:
            log.error("Error serving page %s: %s", path, exc)
            return None

    def _serve_file(self, path, data, request_id, link_id, remote_identity, requested_at):
        file_path = path.replace("/file", self._files_dir, 1)
        file_name = path.replace("/file/", "", 1)
        log.debug("File request: %s → %s", path, file_path)
        try:
            return [open(file_path, "rb"), {"name": file_name.encode("utf-8")}]
        except Exception as exc:
            log.error("Error serving file %s: %s", path, exc)
            return None

    def _serve_default_index(self, path, data, request_id, link_id, remote_identity, requested_at):
        return _DEFAULT_INDEX.encode("utf-8")

    # ------------------------------------------------------------------
    # Background jobs
    # ------------------------------------------------------------------

    def _background_jobs(self) -> None:
        time.sleep(START_ANNOUNCE_DELAY)
        self.announce()

        while self._running:
            time.sleep(60)
            now = time.time()
            if now - self._last_announce > ANNOUNCE_INTERVAL:
                self.announce()
            if now - self._last_rescan > RESCAN_INTERVAL:
                self._register_pages()
                self._register_files()


def _is_windows() -> bool:
    import sys
    return sys.platform == "win32"


def _build_env(link_id, remote_identity, data, node_destination=None) -> dict:
    """Build the env passed to executable pages.

    `remote_identity` may be an RNS.Identity (for link-served requests)
    or a hex string (for local NomadPortal users where the identity comes
    from the logged-in account, not from link.identify()).
    """
    env: dict = {}
    if "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]
    # Propagate PYTHONPATH so executable .mu pages can import packages
    # from the persistent /site/lib/ directory (set by entrypoint.sh).
    if "PYTHONPATH" in os.environ:
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    if node_destination:
        env["node_destination"] = node_destination
    if link_id is not None:
        import RNS
        env["link_id"] = RNS.hexrep(link_id, delimit=False)
    if remote_identity is not None:
        if isinstance(remote_identity, str):
            env["remote_identity"] = remote_identity
        else:
            import RNS
            env["remote_identity"] = RNS.hexrep(remote_identity.hash, delimit=False)
    if data and isinstance(data, dict):
        for k, v in data.items():
            if not isinstance(k, str):
                continue
            # NomadNet's convention: form submissions arrive with `field_X`
            # keys but executable pages read them as `var_X`. We expose both
            # forms so authors can use either prefix; the `var_` form is
            # the documented one.
            if k.startswith("field_"):
                env[k] = v
                env["var_" + k[len("field_"):]] = v
            elif k.startswith("var_"):
                env[k] = v
    return env

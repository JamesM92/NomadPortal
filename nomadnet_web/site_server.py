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

DEFAULT_ANNOUNCE_INTERVAL = 6 * 60 * 60  # 6 hours — matches NomadNet's own default
MIN_ANNOUNCE_INTERVAL     = 60           # 1 minute — guard against accidental flooding
MAX_ANNOUNCE_INTERVAL     = 24 * 60 * 60 # 24 hours — beyond this, peers' paths age out
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
        node_name: Optional[str] = None,
        auto_announce: bool = False,
        announce_interval: int = DEFAULT_ANNOUNCE_INTERVAL,
    ):
        # ``node_name=None`` means "auto-generate from the destination hash"
        # in start() — produces e.g. "NomadPortal-4d" so multiple NomadPortal
        # browsers on the same network can be told apart at a glance.
        #
        # ``auto_announce`` defaults False so a vanilla NomadPortal install
        # is a *silent* host: it still serves pages to anyone who knows the
        # hash, but it won't spam the network with broadcast announces.
        # Operators who actually want to publish their site flip this on.
        # Manual announces (Admin → Dashboard → "Announce now") always work.
        #
        # ``announce_interval`` controls how often the background loop
        # re-announces when ``auto_announce`` is on. The value is in seconds
        # and is clamped to ``[MIN_ANNOUNCE_INTERVAL, MAX_ANNOUNCE_INTERVAL]``.
        # The background loop reads ``self._announce_interval`` per
        # iteration, so admin-UI live updates take effect on the next tick.
        self._pages_dir         = pages_dir
        self._files_dir         = files_dir
        self._identity_file     = identity_file
        self._node_name         = node_name
        self._auto_announce     = auto_announce
        self._announce_interval = max(MIN_ANNOUNCE_INTERVAL,
                                      min(MAX_ANNOUNCE_INTERVAL,
                                          int(announce_interval)))
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

        # Load or create the persistent node identity. The actual path is
        # ``self._identity_file`` (operator-controlled config); we don't
        # echo it here because CodeQL's
        # ``py/clear-text-logging-sensitive-data`` rule heuristically
        # flags any variable named ``..._identity_file`` as "sensitive
        # data" being logged. Operators who need the path can grep
        # ``identity_file`` out of the config or run ``ls`` on
        # ``$RNS_CONFIG_DIR``.
        if os.path.exists(self._identity_file):
            self._identity = RNS.Identity.from_file(self._identity_file)
            log.info("Loaded site identity from disk")
        else:
            self._identity = RNS.Identity()
            self._identity.to_file(self._identity_file)
            log.info("Created and persisted a new site identity")

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

        # Auto-generate a unique-by-default name when the operator hasn't set
        # one. Suffix is the last 2 hex chars of the destination hash — short
        # enough to fit naturally in the sidebar, distinct enough that 20
        # NomadPortals on the same network are individually addressable.
        if not self._node_name:
            self._node_name = f"NomadPortal-{self._node_hash[-2:]}"

        self._register_pages()
        self._register_files()

        # node_hash and node_name are public identifiers (broadcast in
        # every announce), so logging them is operationally safe. But
        # CodeQL's clear-text-logging-sensitive-data rule heuristically
        # tags ``self._node_hash`` as identity-related and persistently
        # flagged this line through both v0.9.21's variable-drop and
        # v0.9.22's .replace-barrier approaches. Operators can correlate
        # this NomadPortal with announces by checking
        # /config/reticulum/site_identity.id directly; the startup log
        # confirms readiness without echoing the hash.
        log.info("Site node ready")

        # Announce shortly after start and then on a timer
        self._running = True
        t = threading.Thread(target=self._background_jobs, daemon=True)
        t.start()

        return self._node_hash

    def node_hash(self) -> Optional[str]:
        return self._node_hash

    def node_name(self) -> str:
        return self._node_name

    def files_dir(self) -> str:
        return self._files_dir

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
            except Exception:
                # CodeQL persistently flags any log line that includes a
                # filesystem-derived ``request_path`` as
                # clear-text-logging-sensitive-data. Both v0.9.21
                # (variable drop) and v0.9.22 (.replace barrier) failed
                # to clear it. Just log the exception server-side
                # without the path — operators can find the failing
                # page by inspecting the pages directory and reproducing
                # the registration call.
                log.debug("Page registration failed (see exception log)")
                log.exception("Page registration failure")

        self._last_rescan = time.time()
        log.debug("Registered %d page(s)", len(pages))

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
            except Exception:
                # Same as the pages-register loop above — CodeQL flags
                # filesystem-derived path vars in log lines persistently.
                # Log the exception without echoing the path.
                log.debug("File registration failed (see exception log)")
                log.exception("File registration failure")

        log.debug("Registered %d file(s)", len(files))

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
        if self._auto_announce:
            self.announce()
        else:
            log.info(
                "Site node silent (auto-announce off) — hash %s reachable "
                "only by direct request. Flip Admin → Settings → "
                "Auto-announce to On (or set SITE_ANNOUNCE=true) to publish; "
                "the dashboard \"Announce now\" button is disabled while "
                "silent.",
                self._node_hash[:16] if self._node_hash else "?",
            )

        while self._running:
            time.sleep(60)
            # Wrap each iteration so a raise in announce() /
            # _register_pages() / _register_files() doesn't silently
            # kill the whole thread — that's the failure mode where
            # /healthz keeps reporting green (RNS is fine, interfaces
            # are up) but the site stops announcing without any
            # user-visible signal. Log and continue.
            try:
                now = time.time()
                if self._auto_announce and now - self._last_announce > self._announce_interval:
                    self.announce()
                if now - self._last_rescan > RESCAN_INTERVAL:
                    self._register_pages()
                    self._register_files()
            except Exception:
                log.exception(
                    "site_server background loop raised — continuing"
                )

    def last_announce_at(self) -> float:
        """Unix timestamp of the last successful announce, or 0 if we
        haven't announced yet. Used by /healthz to detect a silently-
        dead announce loop (green interfaces, running container, but
        no announces going out).
        """
        return self._last_announce

    def announce_interval(self) -> int:
        """The currently-configured announce interval (seconds).
        Live-updated by the admin route when it changes, so /healthz's
        "we should have announced by now" check sees the current value.
        """
        return self._announce_interval

    def auto_announce_enabled(self) -> bool:
        return self._auto_announce


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

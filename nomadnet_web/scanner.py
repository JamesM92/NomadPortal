"""
Pluggable virus-scanning abstraction for file downloads.

NomadPortal does not bundle a scanner — operators wire in an external one
(typically ``clamav-daemon`` on the host or as a sidecar) and turn it on
via the ``VIRUS_SCAN`` env var. When no scanner is configured, downloads
proceed but the UI flags that no scan was performed.

The Scanner contract:

    scan(content: bytes, filename: str = "") -> ScanResult

``ScanResult.verdict`` is one of:

    "clean"       — content passed the scan
    "infected"    — scanner reported a signature match; block the download
    "skipped"     — scanning is off in config; no scan was attempted
    "too-large"   — content exceeded VIRUS_SCAN_MAX_BYTES; skipped on size
    "unavailable" — scanner was configured but unreachable (daemon down,
                    socket missing, IO error). Treated as fail-open unless
                    VIRUS_SCAN=required is set, in which case the consumer
                    blocks the download.
"""

from __future__ import annotations

import logging
import socket
import struct
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    verdict:   str
    engine:    str   = "none"
    signature: str   = ""
    detail:    str   = ""

    @property
    def clean(self) -> bool:
        return self.verdict == "clean"

    @property
    def blocked(self) -> bool:
        return self.verdict == "infected"

    def to_dict(self) -> dict:
        return {
            "verdict":   self.verdict,
            "engine":    self.engine,
            "signature": self.signature,
            "detail":    self.detail,
        }


class Scanner:
    engine_name: str = "none"
    enabled:     bool = False

    def scan(self, content: bytes, filename: str = "") -> ScanResult:
        raise NotImplementedError


class NullScanner(Scanner):
    """Stand-in scanner used when VIRUS_SCAN is off. Returns 'skipped'
    so consumers can surface a clear "no scan was performed" flag in the
    UI rather than misrepresenting the file as clean."""
    engine_name = "none"
    enabled     = False

    def scan(self, content: bytes, filename: str = "") -> ScanResult:
        return ScanResult(verdict="skipped", engine="none")


class ClamdScanner(Scanner):
    """clamd INSTREAM client over Unix socket or TCP.

    INSTREAM protocol summary (see ClamAV docs):

      1. Send b"zINSTREAM\\0".
      2. For each chunk: 4-byte big-endian length, then chunk bytes.
      3. End with a 4-byte length of 0.
      4. Read response — a NUL-terminated string ending in "OK", "FOUND",
         or "ERROR".

    The implementation is intentionally dependency-free (raw sockets) so
    operators don't need to add a clamd python library to their venv.
    """
    engine_name = "clamd"
    enabled     = True

    def __init__(
        self,
        socket_path: Optional[str] = None,
        host: Optional[str] = None,
        port: int = 3310,
        timeout: float = 60.0,
        max_bytes: int = 100 * 1024 * 1024,
    ):
        if not socket_path and not host:
            raise ValueError("ClamdScanner needs socket_path or host")
        self.socket_path = socket_path
        self.host        = host
        self.port        = port
        self.timeout     = timeout
        self.max_bytes   = max_bytes

    def _connect(self) -> socket.socket:
        if self.socket_path:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect(self.socket_path)
        else:
            s = socket.create_connection(
                (self.host, self.port), timeout=self.timeout,
            )
        return s

    def scan(self, content: bytes, filename: str = "") -> ScanResult:
        if len(content) > self.max_bytes:
            return ScanResult(
                verdict="too-large",
                engine=self.engine_name,
                detail=(
                    f"File is {len(content)} bytes; exceeds "
                    f"VIRUS_SCAN_MAX_BYTES ({self.max_bytes})"
                ),
            )

        try:
            s = self._connect()
        except Exception as exc:
            log.warning("clamd connect failed (%s): %s",
                        self.socket_path or f"{self.host}:{self.port}", exc)
            return ScanResult(
                verdict="unavailable",
                engine=self.engine_name,
                detail=f"clamd unreachable: {exc}",
            )

        try:
            s.sendall(b"zINSTREAM\0")
            CHUNK = 65536
            for i in range(0, len(content), CHUNK):
                chunk = content[i:i + CHUNK]
                s.sendall(struct.pack("!I", len(chunk)) + chunk)
            s.sendall(struct.pack("!I", 0))

            buf = b""
            while b"\0" not in buf:
                got = s.recv(4096)
                if not got:
                    break
                buf += got
                if len(buf) > 65536:    # paranoia cap; real responses are tiny
                    break
        except Exception as exc:
            log.warning("clamd scan IO failed: %s", exc)
            return ScanResult(
                verdict="unavailable",
                engine=self.engine_name,
                detail=f"clamd IO error: {exc}",
            )
        finally:
            try:
                s.close()
            except Exception:
                pass

        resp = buf.split(b"\0", 1)[0].decode("ascii", errors="replace").strip()

        if "FOUND" in resp:
            sig = (
                resp.replace("stream:", "", 1)
                    .rsplit("FOUND", 1)[0]
                    .strip()
            )
            log.warning(
                "clamd flagged file %r as infected: %s",
                filename or "<unnamed>", sig,
            )
            return ScanResult(
                verdict="infected",
                engine=self.engine_name,
                signature=sig,
                detail=resp,
            )
        if "ERROR" in resp:
            log.warning("clamd reported error: %s", resp)
            return ScanResult(
                verdict="unavailable",
                engine=self.engine_name,
                detail=resp,
            )
        if "OK" in resp:
            return ScanResult(verdict="clean", engine=self.engine_name)

        return ScanResult(
            verdict="unavailable",
            engine=self.engine_name,
            detail=f"Unexpected clamd response: {resp[:200]!r}",
        )


def build_scanner_from_config(cfg: dict) -> tuple[Scanner, bool]:
    """Construct the scanner from the flask config / env vars.

    Returns ``(scanner, required)`` where ``required`` is True if a clean
    scan is mandatory — when the scanner is unavailable in that mode,
    consumers must block the download rather than fail open.
    """
    mode_raw = str(cfg.get("VIRUS_SCAN", "off")).strip().lower()
    required = mode_raw == "required"

    if mode_raw in ("off", "", "false", "no", "0"):
        return NullScanner(), False
    if mode_raw not in ("clamd", "required"):
        # Don't echo the raw value back — CodeQL flags any env-derived
        # value being logged as potentially-sensitive. Operators with a
        # typo can verify by re-reading VIRUS_SCAN from their config.
        log.warning(
            "Unknown VIRUS_SCAN mode (expected off | clamd | required); "
            "treating as off"
        )
        return NullScanner(), False

    socket_path = (cfg.get("CLAMD_SOCKET") or "").strip() or None
    host        = (cfg.get("CLAMD_HOST")   or "").strip() or None
    port        = int(cfg.get("CLAMD_PORT", 3310))
    max_bytes   = int(cfg.get("VIRUS_SCAN_MAX_BYTES", 100 * 1024 * 1024))

    if not socket_path and not host:
        # Default to the standard Debian/Ubuntu clamav-daemon socket path
        # so a vanilla `apt install clamav-daemon` works without extra env.
        socket_path = "/var/run/clamav/clamd.ctl"
        log.info(
            "No CLAMD_SOCKET/CLAMD_HOST set — defaulting to standard "
            "clamav-daemon Unix socket",
        )

    scanner = ClamdScanner(
        socket_path=socket_path,
        host=host,
        port=port,
        max_bytes=max_bytes,
    )
    # Don't echo the resolved socket path / host in this startup line —
    # CodeQL flags ``log.info("...at %s", socket_path)`` as
    # clear-text-logging-sensitive-data even though clamd locations are
    # operator infra. The dispatch ("clamd via Unix socket" vs "clamd
    # via TCP") is the part operators need; the exact target lives in
    # config.yml or the env.
    transport = "Unix socket" if socket_path else "TCP"
    log.info(
        "Virus scanner: clamd via %s (max_bytes=%d, required=%s)",
        transport, max_bytes, required,
    )
    return scanner, required

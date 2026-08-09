"""On-disk storage for LXMF message attachments (images, audio, files).

Attachments arrive on the LXMF fields ``FIELD_IMAGE`` (0x06),
``FIELD_AUDIO`` (0x07), and ``FIELD_FILE_ATTACHMENTS`` (0x05) — see
``docs/design/chat-uploads.md`` for the full design context. Bytes
land in this store; the message-store JSON keeps only lightweight
metadata (kind / filename / mime / size / disk path) so that
``messages.json`` doesn't inflate with base64-encoded blobs the way
it would if attachments lived in-line — that's the same NAS/GIL
pathology v0.9.x had to fix on the peer + node trackers, and we're
not repeating it.

Lifecycle:
- Write from the LXMF receive path (``MessagingService._on_delivery``)
  or the send path (``/api/messages`` after successful outbound handoff).
- Read via ``GET /api/messages/<msg_id>/attachments/<idx>`` — auth-gated
  to the message's owner.
- Evict when the parent message goes away — either via explicit
  ``MessageStore.delete_conversation`` or the silent ``MAX_MESSAGES``
  overflow that pushes an old message off the top of the LIFO list.
  ``MessageStore`` calls ``AttachmentStore.evict(msg_id)`` in both
  cases so no orphaned blobs accumulate.

Layout on disk:
    <base_dir>/attachments/
        <msg_id>/
            0.jpg
            1.opus
            2.pdf

Files inside ``<msg_id>/`` are indexed 0..N-1 to match the message
entry's ``attachments`` array ordering. Extensions are preserved from
the original filename (sanitized) so the browser can MIME-sniff on
download; the ``Content-Type`` we emit is authoritative via the stored
metadata's ``mime`` field, the extension is a fallback / diagnostic aid.
"""

import logging
import os
import re
import threading
from typing import Optional

log = logging.getLogger(__name__)


# Message IDs from LXMF are hex hashes (typically 32 hex chars for the
# 128-bit id, sometimes longer). Restricting to hex + a length cap
# rejects any attempt to smuggle path separators or shell metachars
# through a poisoned msg_id — see _msg_dir's implementation.
_MSG_ID_MAX_LEN = 64

# File extension whitelist for on-disk names. Preserving the extension
# helps browsers MIME-sniff downloads and keeps ``ls`` output legible;
# but we don't blindly copy any string a peer supplied — anything that
# doesn't look like a normal 1-10 char alphanumeric extension falls back
# to no extension at all. The metadata's ``mime`` field is authoritative
# for content-type, so a missing extension doesn't break rendering.
_EXT_PATTERN = re.compile(r"^\.[a-zA-Z0-9]{1,10}$")


class AttachmentStore:
    """Filesystem-backed blob store for message attachments."""

    def __init__(self, base_dir: str):
        self._root = os.path.join(base_dir, "attachments")
        # 0o700 matches the rest of ``config/`` — attachments carry
        # personal data (photos, voice clips) and shouldn't be world-
        # readable even on a shared-user host. Race-safe: if another
        # process created it first, exist_ok=True absorbs the error.
        os.makedirs(self._root, exist_ok=True, mode=0o700)
        # Lock across write / evict paths so concurrent evictions
        # from ``MessageStore.save_*`` overflow and an explicit
        # ``delete_conversation`` don't race on the same ``msg_id``.
        # Contention is low (evictions are rare) so a single lock is
        # simpler than per-msg locks.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _msg_dir(self, msg_id: str) -> str:
        """Return the on-disk directory for a message's attachments.

        Sanitizes ``msg_id`` to hex characters only and caps its length.
        Any non-hex chars (path separators, dots, nulls, unicode) are
        stripped rather than causing an error — a peer sending a
        garbage msg_id is a bug on their side, not something we should
        crash on. Empty result after stripping falls back to the
        literal string ``"invalid"`` so we always have a stable path
        (that then just holds nothing meaningful).
        """
        safe = re.sub(r"[^a-fA-F0-9]", "", msg_id or "")[:_MSG_ID_MAX_LEN]
        if not safe:
            safe = "invalid"
        return os.path.join(self._root, safe)

    def _safe_extension(self, filename: str) -> str:
        """Return a whitelisted extension (``.jpg``, ``.opus``, ``.pdf``)
        or ``""`` if the filename has none or it's suspicious.

        Peers control ``filename`` (it's echoed back from LXMF field
        contents) so we treat it as untrusted input — don't concatenate
        it into a path, don't preserve odd characters, cap at short
        alphanumeric extensions.
        """
        _, ext = os.path.splitext(filename or "")
        if _EXT_PATTERN.match(ext):
            return ext.lower()
        return ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, msg_id: str, idx: int, filename: str, data: bytes) -> str:
        """Write ``data`` for the given ``(msg_id, idx)`` and return the
        absolute on-disk path. Creates the ``msg_id`` subdirectory if
        needed. Overwrites any existing file at the same slot — LXMF
        receive is idempotent at the message level (same ``msg_id``
        twice = same message), so an overwrite is a no-op in practice
        and lets us not carry a "does this exist?" branch upstream.
        """
        d = self._msg_dir(msg_id)
        os.makedirs(d, exist_ok=True, mode=0o700)
        path = os.path.join(d, f"{int(idx)}{self._safe_extension(filename)}")
        # Write atomically so a partial file doesn't leak on crash.
        # The rename is atomic on POSIX for same-filesystem targets;
        # ``msg_id/`` and the tmp are always on the same filesystem
        # since we created the msg_id dir just above.
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return path

    def read(self, msg_id: str, idx: int) -> Optional[bytes]:
        """Return the bytes for ``(msg_id, idx)``, or ``None`` if there's
        no attachment at that slot. Callers should distinguish None
        (not found) from an empty bytes object (found, but zero-length —
        which is a legal, if odd, state).
        """
        d = self._msg_dir(msg_id)
        if not os.path.isdir(d):
            return None
        # Match by leading "<idx>." or "<idx>" — the extension is
        # variable and we don't want to bake it into the lookup key.
        prefix_dot   = f"{int(idx)}."
        exact_no_ext = f"{int(idx)}"
        for name in os.listdir(d):
            if name.startswith(prefix_dot) or name == exact_no_ext:
                try:
                    with open(os.path.join(d, name), "rb") as fh:
                        return fh.read()
                except OSError as exc:
                    log.warning("Could not read attachment %s/%s: %s",
                                msg_id[:16], idx, exc)
                    return None
        return None

    def evict(self, msg_id: str) -> int:
        """Remove every attachment for a message. Returns the number of
        files deleted. Missing directory is a no-op returning 0 — this
        is called for every deleted message, and many won't have had
        attachments in the first place.
        """
        d = self._msg_dir(msg_id)
        with self._lock:
            if not os.path.isdir(d):
                return 0
            count = 0
            for name in os.listdir(d):
                path = os.path.join(d, name)
                try:
                    os.unlink(path)
                    count += 1
                except OSError as exc:
                    log.warning("Could not unlink %s: %s", path, exc)
            # ``rmdir`` succeeds only if the directory is empty — the
            # only file we'd leave behind is a ``.tmp`` from a
            # concurrent write, and racing an evict against an
            # in-progress write is already a misuse; leaving the empty-
            # ish dir behind for the next cleanup pass is fine.
            try:
                os.rmdir(d)
            except OSError:
                pass
            return count

    def evict_many(self, msg_ids) -> int:
        """Bulk evict. Returns total files removed across all messages.
        Empty / None input is a no-op returning 0.
        """
        if not msg_ids:
            return 0
        return sum(self.evict(mid) for mid in msg_ids)

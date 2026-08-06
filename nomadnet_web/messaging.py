"""
LXMF messaging service.

Each user gets their own LXMRouter instance (with its own storage path) so
that send and receive both use that user's LXMF delivery address.  Messages
are always accepted for any registered delivery identity — there is no
reception window; the server stores messages on behalf of users regardless
of whether they are currently logged in.
"""

import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

PATH_WAIT = 10  # seconds to wait for identity recall after path request


def _hex_to_bytes(value: str) -> bytes:
    """Convert '#rrggbb' (or 'rrggbb') to a 3-byte color tuple. Falls back to grey."""
    if not isinstance(value, str):
        return b'\x80\x80\x80'
    s = value.lstrip("#")
    if len(s) != 6:
        return b'\x80\x80\x80'
    try:
        return bytes.fromhex(s)
    except ValueError:
        return b'\x80\x80\x80'


def _detect_image_mime(data: bytes) -> str:
    if data[:2] == b'\xff\xd8':                              return "image/jpeg"
    if data[:8] == b'\x89PNG\r\n\x1a\n':                     return "image/png"
    if data[:6] in (b'GIF87a', b'GIF89a'):                   return "image/gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':        return "image/webp"
    if data[:5] == b'<?xml' or data[:4] == b'<svg':          return "image/svg+xml"
    return "image/png"


def _render_appearance_svg(name, fg, bg) -> tuple:
    """Render LXMF FIELD_ICON_APPEARANCE to (base64_svg, mime).

    appearance is [icon_name, fg_color_bytes(3), bg_color_bytes(3)]. We produce
    a 32×32 colored circle with the first letter of the icon name as a glyph
    placeholder — material-symbol rendering would require shipping a webfont.
    """
    import base64
    def _hex(c):
        if isinstance(c, (bytes, bytearray)) and len(c) >= 3:
            return "#%02x%02x%02x" % (c[0], c[1], c[2])
        return "#888888"
    fg_hex  = _hex(fg)
    bg_hex  = _hex(bg)
    initial = ((name[:1] if isinstance(name, str) else "") or "?").upper()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<circle cx="16" cy="16" r="16" fill="{bg_hex}"/>'
        f'<text x="16" y="22" text-anchor="middle" font-size="18" '
        f'font-family="sans-serif" font-weight="bold" fill="{fg_hex}">{initial}</text>'
        '</svg>'
    )
    return base64.b64encode(svg.encode("utf-8")).decode("ascii"), "image/svg+xml"


class MessagingService:
    def __init__(self, storage_path: str, message_store=None, contact_store=None):
        self._storage        = storage_path
        self._msg_store      = message_store
        self._contact_mgr    = contact_store  # ContactStoreManager (param kept for compat)
        self._lock           = threading.Lock()
        self._identity_store = None
        # user_sub -> {"router": LXMRouter, "dest": Destination}
        self._user_routers: dict = {}
        os.makedirs(storage_path, exist_ok=True)

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def setup_user(self, user_sub: str) -> None:
        """Ensure a delivery identity is registered for this user.

        Call at login so incoming messages are routed immediately rather
        than waiting for the user's first outbound send.
        """
        data = self._get_user_router(user_sub)
        if data is None:
            log.warning(
                "Could not set up delivery for user %s — "
                "identity may not exist yet",
                user_sub[:16] if user_sub else "?",
            )

    # ------------------------------------------------------------------
    # Delivery setup
    # ------------------------------------------------------------------

    def setup_delivery(self, identity_store) -> None:
        """Register a delivery identity + LXMRouter for every stored user identity."""
        self._identity_store = identity_store
        for entry in identity_store.list_identities():
            self._init_user_router(entry)

    def _init_user_router(self, entry: dict) -> Optional[dict]:
        """Create (or reuse) an LXMRouter for the given identity entry."""
        import LXMF

        identity_id = entry["id"]
        user_sub    = entry.get("user_sub", "")

        with self._lock:
            if user_sub and user_sub in self._user_routers:
                return self._user_routers[user_sub]

        if self._identity_store is None:
            return None
        identity = self._identity_store.load_rns_identity(identity_id)
        if identity is None:
            return None

        user_storage = os.path.join(self._storage, f"u_{identity_id[:16]}")
        os.makedirs(user_storage, exist_ok=True)

        try:
            router = LXMF.LXMRouter(storagepath=user_storage)
            # Match MeshChat's LXMRouter job cadence (1s vs the library
            # default of 4s). Faster processing of pending outbound
            # messages and quicker ``clean_links`` runs. Trivial CPU
            # cost. Guarded by try/except in case a future LXMF version
            # moves or removes the attribute — we don't want a Nomad-
            # Portal boot to break on an upstream refactor.
            try:
                router.PROCESSING_INTERVAL = 1
            except Exception:
                pass
            registered = router.register_delivery_identity(
                identity, display_name=entry.get("name", "")
            )
            if registered is None:
                log.warning(
                    "Could not register delivery for %s "
                    "(LXMRouter already has a delivery identity)", identity_id[:16],
                )
                return None

            router.register_delivery_callback(
                lambda msg, sub=user_sub: self._on_delivery(msg, sub)
            )

            data = {"router": router, "dest": registered, "identity": identity}
            with self._lock:
                if user_sub:
                    self._user_routers[user_sub] = data
            log.info(
                "Registered delivery identity %s → LXMF addr %s (user %s)",
                identity_id[:16], registered.hexhash[:16],
                user_sub[:16] if user_sub else "anon",
            )
            return data

        except Exception as exc:
            log.warning("Failed to init router for %s: %s", identity_id[:16], exc)
            return None

    def _get_user_router(self, user_sub: str) -> Optional[dict]:
        """Return the router/dest pair for a user, lazily initialising if needed."""
        with self._lock:
            data = self._user_routers.get(user_sub)
        if data is not None:
            return data

        if not user_sub or self._identity_store is None:
            return None

        entry = self._identity_store.get_for_user(user_sub)
        if entry is None:
            return None
        return self._init_user_router(entry)

    def reset_user_router(self, user_sub: str) -> None:
        """Drop the cached router for a user so it is rebuilt on next use.

        Call after the user's RNS identity is regenerated (e.g. admin reset)
        so the new keypair's LXMF address takes effect immediately.
        """
        with self._lock:
            self._user_routers.pop(user_sub, None)

    def active_routers(self) -> list:
        """Return a snapshot list of currently-registered routers as
        ``[(user_sub, {"router": ..., "dest": ..., "identity": ...}), ...]``.

        Consumed by ``PropagationSyncService`` — each tick it iterates
        this list and fires an outbound sync per router. Snapshot
        semantics: safe to iterate outside the lock, but a router
        removed after this call may still get one more sync tick.
        Harmless — the sync operation itself is idempotent and
        LXMRouter handles stale references gracefully.

        Admin's router is always present (created at container
        startup); user routers appear on login and disappear when
        ``reset_user_router`` is called.
        """
        with self._lock:
            return list(self._user_routers.items())

    def lxmf_address(self, user_sub: str = "") -> Optional[str]:
        """Return the hexhash of the user's LXMF delivery destination, or None."""
        data = self._get_user_router(user_sub)
        return data["dest"].hexhash if data else None

    def do_announce(self, user_sub: str = "") -> tuple[bool, str]:
        """Announce via the user's LXMRouter so app_data (display name) is included."""
        data = self._get_user_router(user_sub)
        if data is None:
            return False, "No delivery identity registered for this user"
        try:
            data["router"].announce(data["dest"].hash)
            log.info(
                "Announced LXMF delivery destination %s",
                data["dest"].hexhash[:16],
            )
            return True, "Announced"
        except Exception as exc:
            log.error("Announce failed: %s", exc)
            return False, str(exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message(
        self,
        dest_hash_hex: str,
        content: str,
        title: str = "",
        user_sub: str = "",
    ) -> tuple[bool, str]:
        return self._send(
            dest_hash_hex=dest_hash_hex,
            title=title,
            content=content,
            user_sub=user_sub,
        )

    def sent_messages(self) -> list:
        if self._msg_store:
            return self._msg_store.sent_messages()
        return []

    def received_messages(self) -> list:
        if self._msg_store:
            return self._msg_store.received_messages()
        return []

    def mark_read(self, msg_id: str, owner: str = "") -> None:
        if self._msg_store:
            self._msg_store.mark_read(msg_id, owner=owner)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_delivery(self, message, user_sub: str = "") -> None:
        """Called by a user's LXMRouter when an inbound message arrives."""
        source_hex = message.source_hash.hex() if message.source_hash else ""
        msg_id     = message.hash.hex()         if message.hash         else ""

        def _decode(val) -> str:
            if val is None:
                return ""
            return val.decode("utf-8", errors="replace") if isinstance(val, bytes) else str(val)

        # Extract icon from LXMF fields. Two formats are supported:
        #   FIELD_ICON_APPEARANCE (0x04) = [icon_name_str, fg_bytes(3), bg_bytes(3)]
        #     — the MeshChat vector-icon descriptor. Rendered server-side to an SVG.
        #   FIELD_IMAGE          (0x06) = [image_type_str, image_bytes]
        #     — a raw image. Stored directly with detected MIME type.
        # Some older clients may also send raw bytes under 0x04; we accept that too.
        icon_b64  = None
        icon_mime = "image/png"
        try:
            import base64
            fields = getattr(message, "fields", None) or {}
            appearance = fields.get(0x04)
            image      = fields.get(0x06)

            if isinstance(appearance, list) and len(appearance) >= 3:
                icon_b64, icon_mime = _render_appearance_svg(
                    appearance[0], appearance[1], appearance[2]
                )
            elif isinstance(appearance, (bytes, bytearray)) and appearance:
                icon_b64  = base64.b64encode(appearance).decode("ascii")
                icon_mime = _detect_image_mime(appearance)
            elif isinstance(image, list) and len(image) >= 2 and isinstance(image[1], (bytes, bytearray)):
                icon_b64  = base64.b64encode(image[1]).decode("ascii")
                ext = (image[0] or "").lower() if isinstance(image[0], str) else ""
                icon_mime = {
                    "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png",  "gif":  "image/gif",
                    "webp":"image/webp", "svg":  "image/svg+xml",
                }.get(ext, _detect_image_mime(image[1]))
        except Exception as exc:
            log.debug("Icon extraction skipped: %s", exc)

        entry = {
            "id":          msg_id,
            "source":      source_hex,
            "title":       _decode(message.title),
            "content":     _decode(message.content),
            "received_at": time.time(),
            "read":        False,
            "owner":       user_sub,
        }

        log.info(
            "Received LXMF message from %s: %s",
            source_hex[:16] if source_hex else "?",
            entry["title"] or "(no subject)",
        )

        if self._msg_store:
            self._msg_store.save_received(entry)

        if icon_b64 and self._contact_mgr and source_hex and user_sub:
            self._contact_mgr.for_user(user_sub).set_icon(source_hex, icon_b64, icon_mime)

    def _send(
        self,
        dest_hash_hex: str,
        title: str,
        content: str,
        user_sub: str = "",
    ) -> tuple[bool, str]:
        """Queue a message for background delivery and return immediately."""
        import uuid

        user_data = self._get_user_router(user_sub)
        if user_data is None:
            return False, "No delivery identity registered for this user"

        source_dest = user_data["dest"]
        router      = user_data["router"]

        try:
            dest_hash = bytes.fromhex(dest_hash_hex)
        except ValueError:
            return False, "Invalid destination hash"

        msg_id = str(uuid.uuid4())
        entry = {
            "id":      msg_id,
            "dest":    dest_hash_hex,
            "title":   title,
            # Full content, for the sender's own chat-log bubble (mirrors
            # the "content" field _on_delivery() stores for received
            # messages). "preview" alone used to be the only thing stored
            # here — fine for the 120-char conversation-list snippet, but
            # renderChatLog() falls back to it whenever "content" is
            # missing, so every sent message rendered in the open
            # conversation was silently clipped at 120 characters even
            # though the full text was — and still is — what actually
            # went out over LXMF.
            "content": content or "",
            "preview": (content or "")[:120],
            "state":   "queued",
            "sent_at": time.time(),
            "owner":   user_sub,
        }
        if self._msg_store:
            self._msg_store.save_sent(entry)

        def _deliver() -> None:
            import RNS, LXMF
            try:
                # Wait until we can recall the recipient's identity.
                # request_path kicks off path discovery if needed.
                dest_identity = RNS.Identity.recall(dest_hash)
                if dest_identity is None:
                    RNS.Transport.request_path(dest_hash)
                    deadline = time.time() + PATH_WAIT
                    while dest_identity is None:
                        if time.time() > deadline:
                            log.warning(
                                "Identity not recalled for %s after %ss — "
                                "peer may not have announced recently",
                                dest_hash_hex[:16], PATH_WAIT,
                            )
                            if self._msg_store:
                                self._msg_store.update_sent(msg_id, "failed")
                            return
                        time.sleep(0.25)
                        dest_identity = RNS.Identity.recall(dest_hash)

                lxmf_dest = RNS.Destination(
                    dest_identity,
                    RNS.Destination.OUT,
                    RNS.Destination.SINGLE,
                    "lxmf",
                    "delivery",
                )
                # Attach the sender's icon appearance if one is set.
                fields = {}
                if self._identity_store and user_sub:
                    icon = self._identity_store.get_icon_appearance_for_user(user_sub)
                    if icon:
                        fields[0x04] = [
                            icon.get("glyph", "?"),
                            _hex_to_bytes(icon.get("fg", "#ffffff")),
                            _hex_to_bytes(icon.get("bg", "#5ba3c9")),
                        ]

                # Prefer OPPORTUNISTIC (single encrypted packet, no link needed).
                # LXMessage automatically falls back to DIRECT if the content
                # is too large for a single packet.
                lxmf_msg = LXMF.LXMessage(
                    lxmf_dest,
                    source_dest,
                    content,
                    title=title or "",
                    fields=fields or None,
                    desired_method=LXMF.LXMessage.OPPORTUNISTIC,
                )

                # Callbacks update the store whenever delivery completes —
                # no fixed wait so long messages don't time out prematurely.
                def _delivered(_m):
                    real_id = _m.hash.hex() if _m.hash else msg_id
                    if self._msg_store:
                        self._msg_store.update_sent(msg_id, "delivered", real_id=real_id)
                    log.info("Delivered %s → %s", msg_id[:8], dest_hash_hex[:16])

                def _failed(_m):
                    if self._msg_store:
                        self._msg_store.update_sent(msg_id, "failed")
                    log.warning("Delivery failed %s → %s", msg_id[:8], dest_hash_hex[:16])

                lxmf_msg.register_delivery_callback(_delivered)
                lxmf_msg.register_failed_callback(_failed)
                router.handle_outbound(lxmf_msg)
                log.info("Queued LXMF message %s → %s", msg_id[:8], dest_hash_hex[:16])

            except Exception:
                log.exception("Async LXMF delivery error for %s", msg_id[:8])
                if self._msg_store:
                    self._msg_store.update_sent(msg_id, "failed")

        threading.Thread(target=_deliver, daemon=True).start()
        return True, msg_id

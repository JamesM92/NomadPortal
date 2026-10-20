"""
LXMF messaging service.

Each user gets their own LXMRouter instance (with its own storage path) so
that send and receive both use that user's LXMF delivery address.  Messages
are always accepted for any registered delivery identity — there is no
reception window; the server stores messages on behalf of users regardless
of whether they are currently logged in.
"""

import logging
import mimetypes
import os
import threading
import time
from typing import Optional

from . import mdi_icons
from .stamps import parse_stamp_cost

log = logging.getLogger(__name__)

PATH_WAIT = 10  # seconds to wait for identity recall after path request

# Re-sending messages that failed while the recipient was away (as MeshChat
# does): when the recipient announces again, recent failures are tried once
# more. Bounded so a flaky or abusive peer cannot cause endless traffic.
AUTO_RESEND_MAX_AGE_S = 24 * 3600
AUTO_RESEND_MAX_ATTEMPTS = 3
AUTO_RESEND_COOLDOWN_S = 120


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


# Map the ``image_type_str`` component of ``FIELD_IMAGE (0x06)`` to a
# canonical MIME type. MeshChat sends the file's extension (``"jpg"``,
# ``"png"``, ``"webp"``, ...) — not always the MIME. Falls back to
# byte-sniffing via ``_detect_image_mime`` when the extension is
# unrecognized or the sender didn't include one.
_IMAGE_EXT_TO_MIME = {
    "jpg":  "image/jpeg", "jpeg": "image/jpeg",
    "png":  "image/png",  "gif":  "image/gif",
    "webp": "image/webp", "svg":  "image/svg+xml",
}


# ``FIELD_AUDIO (0x07)`` is ``[audio_mode, audio_bytes]`` and ``audio_mode``
# is an INTEGER from LXMF's own ``AM_*`` constants. Checked against LXMF and
# against MeshChat's source (its sender passes the int straight through; its
# player handles only ``AM_OPUS_OGG`` and the Codec2 modes). Sideband uses the
# same constants.
#
# - ``AM_OPUS_OGG`` (0x10): the bytes are an Ogg-wrapped Opus stream, which a
#   browser's <audio> element can play.
# - ``AM_CODEC2_*`` (0x01-0x09): Codec2 frames. A browser cannot play these
#   without a decoder, so they are kept as a downloadable file.
# - The other Opus modes (0x11-0x19) are raw Opus presets with no container;
#   also kept as a file.
#
# Until v1.3.x the portal read and wrote short strings here ("opus", "webm",
# "mp3"), so audio never interoperated with a real client. The string table
# below is kept ONLY so messages from those older portal versions still play.
AM_OPUS_OGG = 0x10
_CODEC2_MODES = {
    0x01: "450pwb", 0x02: "450", 0x03: "700c", 0x04: "1200", 0x05: "1300",
    0x06: "1400", 0x07: "1600", 0x08: "2400", 0x09: "3200",
}
_OPUS_PRESETS = {
    0x11: "lbw", 0x12: "mbw", 0x13: "ptt", 0x14: "rt-hdx", 0x15: "rt-fdx",
    0x16: "standard", 0x17: "hq", 0x18: "broadcast", 0x19: "lossless",
}
_LEGACY_AUDIO_STRING_TO_MIME = {
    "opus":  "audio/opus",  "ogg":  "audio/ogg",  "oga": "audio/ogg",
    "mp3":   "audio/mpeg",  "mpeg": "audio/mpeg",
    "wav":   "audio/wav",   "wave": "audio/wav",
    "webm":  "audio/webm",  "m4a":  "audio/mp4",  "mp4": "audio/mp4",
    "aac":   "audio/aac",   "flac": "audio/flac",
}
_LEGACY_AUDIO_STRING_TO_EXT = {
    "opus":  "opus", "ogg":  "ogg",  "oga":  "oga",
    "mp3":   "mp3",  "mpeg": "mp3",
    "wav":   "wav",  "wave": "wav",
    "webm":  "webm", "m4a":  "m4a",  "mp4":  "mp4",
    "aac":   "aac",  "flac": "flac",
}


def _is_ogg(data) -> bool:
    return bytes(data[:4]) == b"OggS"


def classify_audio_field(mode, data) -> tuple:
    """What a received ``FIELD_AUDIO`` is: ``(kind, mime, filename)``.

    ``kind`` is ``"audio"`` only when a browser can play it (Ogg Opus, or a
    legacy string mode from an older portal); everything else is a ``"file"``
    so the recipient can still download the bytes instead of getting a dead
    player. Never raises.
    """
    if isinstance(mode, int) and not isinstance(mode, bool):
        if mode == AM_OPUS_OGG and _is_ogg(data):
            return "audio", "audio/ogg", "voice.ogg"
        if mode in _CODEC2_MODES:
            return "file", "application/octet-stream", f"voice-codec2-{_CODEC2_MODES[mode]}.c2"
        if mode in _OPUS_PRESETS:
            return "file", "application/octet-stream", f"voice-opus-{_OPUS_PRESETS[mode]}.opus"
        return "file", "application/octet-stream", f"voice-mode-{mode}.bin"
    if isinstance(mode, str):
        key = mode.lower()
        mime = _LEGACY_AUDIO_STRING_TO_MIME.get(key)
        if mime:
            ext = _LEGACY_AUDIO_STRING_TO_EXT.get(key, "")
            return "audio", mime, f"audio.{ext}" if ext else "audio"
    return "file", "application/octet-stream", "audio"


# Reverse of ``_IMAGE_EXT_TO_MIME`` for the send side. Given a MIME
# type of an outbound image (browser-supplied via <input type="file">),
# return the short extension string MeshChat expects in the first slot
# of ``FIELD_IMAGE = [image_type_str, image_bytes]``. Unknown or non-
# image MIMEs return "" so the caller can fall back to a filename-
# derived hint.
_MIME_TO_IMAGE_EXT = {
    "image/jpeg":   "jpg",
    "image/pjpeg":  "jpg",
    "image/png":    "png",
    "image/gif":    "gif",
    "image/webp":   "webp",
    "image/svg+xml": "svg",
}

# Audio that can be sent as a real ``FIELD_AUDIO`` (mode ``AM_OPUS_OGG``):
# Ogg-wrapped Opus only, which is what MeshChat plays. Anything else the
# user attaches (mp3, wav, m4a, WebM audio, ...) goes as an ordinary file
# attachment, which every client can download, instead of as an audio field
# no client can play.
_OGG_AUDIO_MIMES = ("audio/ogg", "audio/opus", "application/ogg")


def audio_field_for_send(mime: str, blob) -> Optional[list]:
    """``[mode, bytes]`` for ``FIELD_AUDIO``, or None when this audio cannot
    be sent as one (the caller then sends it as a file). The bytes must
    really be an Ogg stream: a mislabelled file is not sent as audio."""
    if (mime or "").lower() in _OGG_AUDIO_MIMES and _is_ogg(blob):
        return [AM_OPUS_OGG, bytes(blob)]
    return None


def _classify_attachment_kind(mime: str) -> str:
    """MIME → outbound attachment kind. ``"image"`` if the browser
    handed us an image the recipient can render inline, ``"audio"``
    if likewise for audio, ``"file"`` otherwise. Falls through to
    ``"file"`` for image/audio MIMEs the wire format can't carry
    natively (e.g. ``image/heic`` — no MeshChat parser) so those
    still transfer as a generic download rather than being dropped.
    """
    m = (mime or "").lower()
    if m in _MIME_TO_IMAGE_EXT:
        return "image"
    if m in _OGG_AUDIO_MIMES:
        return "audio"
    return "file"


def _channel_to_255(v):
    """One color channel — accepts either an int already in 0-255
    or a 0-1 float (Sideband's shape). Clamped to bounds either way.
    """
    if isinstance(v, bool):
        return 255 if v else 0
    if isinstance(v, int):
        return max(0, min(255, v))
    if isinstance(v, float):
        return max(0, min(255, round(v * 255)))
    return 128


def _appearance_color_to_hex(value) -> str:
    """LXMF FIELD_ICON_APPEARANCE color → '#rrggbb'. Two shapes exist
    in the wild:

    - MeshChat / this app: raw 3-byte ``bytes`` object, no alpha —
      sent via ``bytes.fromhex(rrggbb)``.
    - Sideband (LXMF library's reference client): a ``[r, g, b]`` or
      ``[r, g, b, a]`` sequence of 0-1 floats. Its
      DEFAULT_APPEARANCE is ``["account", [0,0,0,1], [1,1,1,1]]``.

    Historically our converter only accepted the bytes shape, so
    Sideband-users' colors rendered as a flat grey ``#888888``
    circle. Ported from the ``python-core`` of the
    NomadPortal-Android sister project, which hit this interop
    failure explicitly. Unknown shapes still fall back to grey.
    """
    if isinstance(value, (bytes, bytearray)) and len(value) >= 3:
        return "#%02x%02x%02x" % (value[0], value[1], value[2])
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        r, g, b = (_channel_to_255(value[i]) for i in range(3))
        return "#%02x%02x%02x" % (r, g, b)
    return "#888888"


def _render_appearance_svg(name, fg, bg) -> tuple:
    """Render LXMF FIELD_ICON_APPEARANCE to (base64_svg, mime).

    ``fg`` / ``bg`` are colors in either MeshChat's ``bytes(3)`` or
    Sideband's ``[r,g,b]`` float shape (see
    ``_appearance_color_to_hex``). Produces a 32×32 colored circle.

    When ``name`` resolves against the real Material Design Icons
    catalog (``mdi_icons.py`` — the same namespace Sideband/MeshChat
    actually pick icon names from), draws the real icon shape inset
    within the circle. Real MeshChat/Sideband contacts routinely send
    genuine MDI names (``"hiking"``, ``"account-supervisor"``, ...), so
    this is the common case, not the fallback — the previous behaviour
    (always drawing just the icon name's first letter as text; a real
    webfont would've been needed for anything richer) is now only the
    degraded path for a name the catalog doesn't recognize, matching how
    the NomadPortal-Android sister project resolved the same gap.
    """
    import base64
    fg_hex = _appearance_color_to_hex(fg)
    bg_hex = _appearance_color_to_hex(bg)

    icon_path = mdi_icons.get_path(name) if isinstance(name, str) else None
    if icon_path:
        # MDI's own viewBox is 24x24; inset it to 20x20 centered in the
        # 32x32 circle (6px margin each side) rather than edge-to-edge —
        # matches the visual weight of a typical avatar-with-icon look.
        glyph_svg = (
            f'<g transform="translate(6,6) scale({20/24:.4f})">'
            f'<path d="{icon_path}" fill="{fg_hex}"/></g>'
        )
    else:
        initial = ((name[:1] if isinstance(name, str) else "") or "?").upper()
        glyph_svg = (
            f'<text x="16" y="22" text-anchor="middle" font-size="18" '
            f'font-family="sans-serif" font-weight="bold" fill="{fg_hex}">{initial}</text>'
        )

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<circle cx="16" cy="16" r="16" fill="{bg_hex}"/>'
        f'{glyph_svg}'
        '</svg>'
    )
    return base64.b64encode(svg.encode("utf-8")).decode("ascii"), "image/svg+xml"


class MessagingService:
    def __init__(self, storage_path: str, message_store=None,
                 contact_store=None, attachment_store=None):
        self._storage        = storage_path
        self._msg_store      = message_store
        self._contact_mgr    = contact_store  # ContactStoreManager (param kept for compat)
        # Blob store for inbound message attachments (images, audio,
        # files). Optional so tests and older callers can construct
        # without one; when None, inbound attachments are dropped
        # (their metadata isn't recorded in the message entry).
        # See ``docs/design/chat-uploads.md``.
        self._attachments    = attachment_store
        self._lock           = threading.Lock()
        self._identity_store = None
        # identity_id -> {"router": LXMRouter, "dest": Destination,
        # "identity": RNS.Identity, "user_sub": <owning account>}. Keyed
        # by identity, not by account — see module-level design note
        # above _init_identity_router() for why an account can now have
        # more than one live entry here at once.
        self._identity_routers: dict = {}
        # Incoming-message proof of work (see stamps.py). Off until an
        # operator turns it on; applied to every delivery identity.
        self._stamp_cost: int = 0
        self._enforce_stamps: bool = False
        # Messages handed to a router and not yet delivered or failed:
        # msg_id -> {"router", "lxm"}, so one can be cancelled. A message
        # id in _cancelled must never be marked failed or queued again.
        self._outbound: dict = {}
        self._cancelled: set = set()
        self._auto_resend: bool = True
        self._resend_last: dict = {}        # dest hash -> time of the last auto-resend
        os.makedirs(storage_path, exist_ok=True)

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def stamp_policy(self) -> tuple:
        """``(cost, enforce)`` currently applied to incoming messages."""
        with self._lock:
            return self._stamp_cost, self._enforce_stamps

    def set_stamp_policy(self, cost, enforce: bool = False) -> None:
        """Require senders to attach a proof-of-work stamp of this cost
        (0 turns it off), and optionally refuse messages whose stamp is
        missing or invalid. Without enforcement the cost is only
        advertised in announces and a message with a bad stamp is still
        delivered. Applied to every live delivery identity at once and to
        any created later. Senders learn a changed cost from the
        identity's next announce."""
        n = parse_stamp_cost(cost) or 0
        with self._lock:
            self._stamp_cost = n
            self._enforce_stamps = bool(enforce)
            live = list(self._identity_routers.values())
        for data in live:
            try:
                setter = getattr(data["router"], "set_inbound_stamp_cost", None)
                if setter is not None:
                    setter(data["dest"].hash, n or None)
                self._apply_stamp_enforcement(data["router"])
            except Exception:
                log.exception("Could not apply the stamp policy to a delivery identity")

    def _apply_stamp_enforcement(self, router) -> None:
        """Make the router's enforcement match the policy. Enforcement
        only means something while a cost is required. Tolerates an LXMF
        version without these switches."""
        want = bool(self._enforce_stamps and self._stamp_cost)
        fn = getattr(router, "enforce_stamps" if want else "ignore_stamps", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                log.exception("Could not set stamp enforcement")

    def setup_user(self, user_sub: str) -> None:
        """Ensure every one of this account's identities has a live
        delivery router — so messages can be received on ANY identity
        the account owns, not just its currently active one — with only
        the active identity actually announced (see
        ``_init_identity_router()``'s own doc comment for why: an
        identity nobody's actively presenting shouldn't keep inviting
        new inbound paths, even though it still happily receives
        anything already addressed to it).

        Call at login so this doesn't wait for ``setup_delivery()``'s
        own boot-time pass — covers an identity created/imported after
        boot, or an account logging in for the first time this run.
        """
        if self._identity_store is None:
            log.warning(
                "Could not set up delivery for user %s — identity store not ready",
                user_sub[:16] if user_sub else "?",
            )
            return
        entries = self._identity_store.list_for_user(user_sub)
        if not entries:
            log.warning(
                "Could not set up delivery for user %s — "
                "identity may not exist yet",
                user_sub[:16] if user_sub else "?",
            )
            return
        active_entry = self._identity_store.get_active_for_user(user_sub)
        active_id    = active_entry["id"] if active_entry else None
        for entry in entries:
            self._init_identity_router(entry, announce=(entry["id"] == active_id))

    # ------------------------------------------------------------------
    # Delivery setup
    # ------------------------------------------------------------------

    def setup_delivery(self, identity_store) -> None:
        """Register a delivery identity + LXMRouter for EVERY identity
        of every account — so an account can receive on any identity it
        owns — but only announce each account's currently *active*
        identity (see ``_init_identity_router()``'s own doc comment)."""
        self._identity_store = identity_store
        for entry in identity_store.list_identities():
            user_sub = entry.get("user_sub", "")
            active_entry = identity_store.get_active_for_user(user_sub) if user_sub else None
            is_active = bool(active_entry) and active_entry["id"] == entry["id"]
            self._init_identity_router(entry, announce=is_active)

    def _init_identity_router(self, entry: dict, announce: bool = True) -> Optional[dict]:
        """Create (or reuse) an LXMRouter for the given identity entry,
        keyed by the identity's own id — NOT by the owning account —
        since multi-identity means an account can have more than one of
        these live at once (one per identity it owns), all delivering
        into that account's one shared inbox (the delivery callback
        below closes over the entry's own ``user_sub``, not the
        identity id).

        [announce] controls whether the real "make this identity
        reachable" bootstrap announce fires (see the block below for
        why that's needed at all) — the caller decides based on whether
        this is the account's currently *active* identity. Only the
        active identity should be inviting new inbound paths; the rest
        stay quietly receptive to anything already addressed to them
        (a contact who already has their address can still reach them)
        without advertising themselves further. There's no periodic
        re-announce loop elsewhere in this codebase for LXMF identities
        (unlike voice calls' own 3-hourly loop) — the bootstrap announce
        here is the *only* place an LXMF identity ever announces short
        of the user's own manual Announce button (which itself always
        targets the active identity, via ``do_announce()`` →
        ``_get_user_router()``) — so gating it here is sufficient to
        keep an inactive identity from announcing at all.
        """
        import LXMF

        identity_id = entry["id"]
        user_sub    = entry.get("user_sub", "")

        with self._lock:
            if identity_id in self._identity_routers:
                return self._identity_routers[identity_id]

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
            # The cost is passed only when set, so the default call is
            # exactly what it has always been.
            reg_kwargs = {"stamp_cost": self._stamp_cost} if self._stamp_cost else {}
            registered = router.register_delivery_identity(
                identity, display_name=entry.get("name", ""), **reg_kwargs
            )
            self._apply_stamp_enforcement(router)
            if registered is None:
                log.warning(
                    "Could not register delivery for %s "
                    "(LXMRouter already has a delivery identity)", identity_id[:16],
                )
                return None

            router.register_delivery_callback(
                lambda msg, sub=user_sub: self._on_delivery(msg, sub)
            )

            data = {"router": router, "dest": registered, "identity": identity, "user_sub": user_sub}
            with self._lock:
                self._identity_routers[identity_id] = data
            log.info(
                "Registered delivery identity %s → LXMF addr %s (user %s)%s",
                identity_id[:16], registered.hexhash[:16],
                user_sub[:16] if user_sub else "anon",
                "" if announce else " [not announcing — inactive identity]",
            )

            if not announce:
                return data

            # RNS path discovery is announce-based with no other mechanism —
            # a destination that has never announced is unreachable by
            # anyone, full stop. Without this, a brand-new identity (or one
            # whose last announce has aged out of peers' routing tables)
            # sat unreachable until the user discovered and clicked the
            # manual Announce button themselves; a message sent to them in
            # the meantime just failed after PATH_WAIT with no obvious
            # reason why. This fires on every process restart too (routers
            # are rebuilt in-memory on each boot via setup_delivery()/
            # setup_user(), not just on first creation) — deliberately, not
            # just for new identities: it doubles as the periodic
            # re-announce that keeps an existing user's path from going
            # stale on their peers' end. Best-effort; a failure here
            # shouldn't take down router registration.
            try:
                router.announce(registered.hash)
            except Exception as exc:
                log.warning(
                    "Bootstrap announce failed for %s: %s", identity_id[:16], exc,
                )

            return data

        except Exception as exc:
            log.warning("Failed to init router for %s: %s", identity_id[:16], exc)
            return None

    def _get_user_router(self, user_sub: str) -> Optional[dict]:
        """Return the router for this account's currently *active*
        identity, lazily initialising (and announcing) it if needed."""
        if not user_sub or self._identity_store is None:
            return None

        entry = self._identity_store.get_for_user(user_sub)
        if entry is None:
            return None
        return self._init_identity_router(entry)

    def reset_identity_router(self, identity_id: str) -> None:
        """Drop the cached router for one specific identity (keyed by
        its own id, not the owning account) so it's rebuilt on next
        use. Call after that identity's RNS keypair is regenerated
        (admin reset) so the new keypair's LXMF address takes effect
        immediately — the replacement identity gets a brand-new
        ``identity_id`` (a different hexhash), so the old entry here is
        simply orphaned, not reused.

        Does NOT call the popped router's own ``exit_handler()`` — the
        identity behind it no longer exists (a fresh one, under a
        different id, replaces it), so there's nothing meaningful left
        to cleanly hand off to. Compare ``deactivate_identity()``,
        which does the real teardown for an identity that's being
        deleted outright (not replaced).
        """
        with self._lock:
            self._identity_routers.pop(identity_id, None)

    def deactivate_identity(self, identity_id: str) -> None:
        """Cleanly stops one specific identity's own LXMRouter —
        ``LXMRouter.exit_handler()`` (verified directly against the
        installed LXMF/LXMRouter.py source) tears down that router's
        own delivery destination/links and flips
        ``exit_handler_running``, which its own background job-loop
        threads check to stop themselves — it only touches that
        router's own state, so this is safe to call while
        ``RNS.Reticulum()`` and every other identity's router (this
        account's other identities included) keep running.

        Used when that identity is being deleted outright (it can no
        longer receive for anything once its keypair is gone) — NOT
        when merely switching which identity is active. Every identity
        an account owns keeps its own live router so messages can be
        received on any of them; only the currently-active one gets
        announced (see ``_init_identity_router()``'s own doc comment).
        A no-op if this identity has no live router.
        """
        with self._lock:
            data = self._identity_routers.pop(identity_id, None)
        if data is None:
            return
        try:
            data["router"].exit_handler()
        except Exception:
            log.exception(
                "deactivate_identity: exit_handler() raised for %s — "
                "router is still removed from the live set either way",
                identity_id[:16],
            )

    def activate_identity(self, entry: dict) -> Optional[dict]:
        """Ensures [entry]'s identity has a live, *announced* router —
        the real mechanism behind marking an identity active (routes.py's
        switch-identity flow), or bringing a freshly created/imported
        identity's router up immediately rather than waiting for a lazy
        fetch. [entry] is an ``IdentityStore`` entry dict (has
        "id"/"user_sub"/"name"). Safe to call for an identity that's
        already live — ``_init_identity_router`` itself already
        tolerates that (returns the existing router, unannounced again)."""
        return self._init_identity_router(entry, announce=True)

    def active_routers(self) -> list:
        """Return a snapshot list of every currently-live identity
        router as ``[(user_sub, {"router":, "dest":, "identity":,
        "user_sub":}), ...]`` — one entry per identity, which can now be
        more than one per account (every identity an account owns keeps
        a live router for receiving, not just the active one).

        Consumed by ``PropagationSyncService`` — each tick it iterates
        this list and fires an outbound sync per router. Snapshot
        semantics: safe to iterate outside the lock, but a router
        removed after this call may still get one more sync tick.
        Harmless — the sync operation itself is idempotent and
        LXMRouter handles stale references gracefully.
        """
        with self._lock:
            return [(data["user_sub"], data) for data in self._identity_routers.values()]

    def lxmf_address(self, user_sub: str = "") -> Optional[str]:
        """Return the hexhash of the account's *active* identity's LXMF
        delivery destination, or None."""
        data = self._get_user_router(user_sub)
        return data["dest"].hexhash if data else None

    def get_identity(self, user_sub: str = ""):
        """Return the ``RNS.Identity`` behind this account's currently
        active LXMF router, or None. The real caller is the rnsh
        terminal feature, which authenticates to a remote listener with
        the same identity LXMF messaging already uses for this account
        — reused, not a separate rnsh-specific identity (see
        ``rnsh_client.RnshSession``'s own doc comment)."""
        data = self._get_user_router(user_sub)
        return data["identity"] if data else None

    def refresh_router_display_name(self, user_sub: str, name: str) -> None:
        """Applies a rename to the *live* router's destination immediately
        (if one is running), so an announce made right after a rename
        doesn't still carry the old name. Only ever meaningful for the
        account's *active* identity — routes.py's own rename handler
        already only calls this when the identity just renamed is the
        active one (renaming an inactive identity has no live announce
        to refresh in the first place).

        Real bug, found on the NomadPortal-Android sister project via a
        live on-device report ("the announce is sending out with the
        hash and not the assigned name"), root-caused by reading
        LXMRouter.py directly: ``LXMRouter.announce()`` does *not*
        consult ``Destination.default_app_data`` — it always calls
        ``delivery_destination.announce(app_data=
        self.get_announce_app_data(destination_hash), ...)`` with an
        explicit ``app_data`` argument that unconditionally wins.
        ``get_announce_app_data()`` in turn reads the plain
        ``delivery_destination.display_name`` attribute, set once at
        ``register_delivery_identity(identity, display_name=...)`` time
        (see ``_init_identity_router()`` above) and never touched again
        by anything short of this method. Without it, a rename persists
        to disk correctly (identity_store.rename()) but never changes
        what any *live* announce actually carries — mesh peers keep
        seeing the identity's original name until the next full process
        restart re-registers the delivery identity with the new
        persisted name. ``display_name`` has no dedicated setter; this
        is a plain attribute assignment, confirmed directly against
        ``register_delivery_identity()``'s own body.

        Best-effort and silent about "nothing to refresh": a no-op if
        this account has no active identity, or that identity has no
        live router yet — the next ``_init_identity_router()`` call
        picks up the new name from disk regardless.
        """
        if self._identity_store is None:
            return
        entry = self._identity_store.get_for_user(user_sub)
        if entry is None:
            return
        with self._lock:
            data = self._identity_routers.get(entry["id"])
        if data is None:
            return
        try:
            data["dest"].display_name = name
        except Exception as exc:
            log.warning("Renamed identity but couldn't update live display_name: %s", exc)

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
        attachments: Optional[list] = None,
    ) -> tuple[bool, str]:
        """Queue an outbound LXMF message.

        ``attachments`` is an optional list of dicts of the form
        ``{"data": bytes, "filename": str, "mime": str}``. Callers
        (currently the ``POST /api/messages`` multipart branch in
        ``routes.py``) are responsible for enforcing per-attachment
        and total-message size caps upstream; this layer trusts what
        it's given and just persists / wire-formats it.
        """
        return self._send(
            dest_hash_hex=dest_hash_hex,
            title=title,
            content=content,
            user_sub=user_sub,
            attachments=attachments or None,
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
        """Called by a user's LXMRouter when an inbound message arrives.

        Extracts contact-icon updates and message attachments from the
        LXMF ``fields`` dict. Icon-vs-attachment split — see
        ``docs/design/chat-uploads.md``:

        - ``FIELD_ICON_APPEARANCE`` (0x04) is a vector descriptor and
          is ALWAYS a contact-icon update (never an attachment).
        - ``FIELD_IMAGE`` (0x06) is treated as a contact icon when the
          message has no text content (announce-shaped delivery) and
          as an inline image attachment when the message DOES have
          text (chat-shaped delivery). Falsifies to receive-side
          testing if this heuristic doesn't match MeshChat's actual
          behaviour in the wild.
        """
        import base64

        source_hex = message.source_hash.hex() if message.source_hash else ""
        msg_id     = message.hash.hex()         if message.hash         else ""

        # Per-sender blocking — checked before anything else runs, so a
        # blocked sender's message never gets stored, never triggers an
        # icon update, never surfaces anywhere. Deliberately silent
        # (not a distinct error/rejection the sender could observe) —
        # indistinguishable from any other delivery outcome on their
        # end, same privacy reasoning this app already applies
        # elsewhere for contacts-only enforcement (a filtered sender
        # sees no different result than a genuine delivery failure).
        if self._contact_mgr and source_hex and user_sub:
            if self._contact_mgr.for_user(user_sub).is_blocked(source_hex):
                log.info(
                    "Dropping message from blocked contact %s",
                    source_hex[:16],
                )
                return

        def _decode(val) -> str:
            if val is None:
                return ""
            return val.decode("utf-8", errors="replace") if isinstance(val, bytes) else str(val)

        title       = _decode(message.title)
        content     = _decode(message.content)
        has_content = bool(title.strip() or content.strip())

        fields     = getattr(message, "fields", None) or {}
        appearance = fields.get(0x04)  # FIELD_ICON_APPEARANCE
        files      = fields.get(0x05)  # FIELD_FILE_ATTACHMENTS
        image      = fields.get(0x06)  # FIELD_IMAGE
        audio      = fields.get(0x07)  # FIELD_AUDIO

        # --------------------------------------------------------------
        # Contact-icon extraction (0x04 always icon; 0x06 icon-only when
        # the message carries no text — announce-shaped delivery)
        # --------------------------------------------------------------
        icon_b64  = None
        icon_mime = "image/png"
        try:
            if isinstance(appearance, list) and len(appearance) >= 3:
                icon_b64, icon_mime = _render_appearance_svg(
                    appearance[0], appearance[1], appearance[2]
                )
            elif isinstance(appearance, (bytes, bytearray)) and appearance:
                icon_b64  = base64.b64encode(appearance).decode("ascii")
                icon_mime = _detect_image_mime(appearance)
            elif (not has_content and isinstance(image, list)
                    and len(image) >= 2
                    and isinstance(image[1], (bytes, bytearray))):
                # No text content → this FIELD_IMAGE is a contact icon.
                icon_b64  = base64.b64encode(image[1]).decode("ascii")
                ext = (image[0] or "").lower() if isinstance(image[0], str) else ""
                icon_mime = _IMAGE_EXT_TO_MIME.get(ext, _detect_image_mime(image[1]))
        except Exception as exc:
            log.debug("Icon extraction skipped: %s", exc)

        # --------------------------------------------------------------
        # Attachment extraction (v1.3.0 steps 2 + 3). Each accepted
        # field appends one or more entries to ``attachments`` and
        # writes the blob under a per-message ``idx``. Ordering:
        # image first (0x06), then files (0x05 — array of files), then
        # audio (0x07). Matches MeshChat's own send-side ordering,
        # which — while not a wire-protocol requirement — is what
        # existing peers produce, so a chat-log rendered the same way
        # in both apps looks the same.
        # --------------------------------------------------------------
        attachments = []
        if has_content and self._attachments:
            # FIELD_IMAGE (0x06) — single inline image
            try:
                if (isinstance(image, list) and len(image) >= 2
                        and isinstance(image[1], (bytes, bytearray))):
                    ext = (image[0] or "").lower() if isinstance(image[0], str) else ""
                    mime = _IMAGE_EXT_TO_MIME.get(ext, _detect_image_mime(image[1]))
                    filename = f"image.{ext}" if ext else "image"
                    idx = len(attachments)
                    self._attachments.write(msg_id, idx, filename, bytes(image[1]))
                    attachments.append({
                        "kind":     "image",
                        "idx":      idx,
                        "filename": filename,
                        "mime":     mime,
                        "size":     len(image[1]),
                    })
            except Exception as exc:
                log.warning("Image attachment persist failed for %s: %s",
                            msg_id[:16] if msg_id else "?", exc)

            # FIELD_FILE_ATTACHMENTS (0x05) — array of [name, bytes]
            # tuples. Each entry becomes its own attachment slot.
            # Extension is derived from the sender's filename; mime is
            # guessed by extension or falls back to
            # ``application/octet-stream``.
            if isinstance(files, list):
                for f in files:
                    try:
                        if (not isinstance(f, (list, tuple))
                                or len(f) < 2
                                or not isinstance(f[1], (bytes, bytearray))):
                            continue
                        raw_name = f[0]
                        filename = raw_name if isinstance(raw_name, str) else "file"
                        # Extension → MIME. mimetypes.guess_type gives a
                        # good default for most non-image, non-audio
                        # types (pdf, txt, zip, docx, ...). Explicit
                        # fallback so we always send a Content-Type.
                        mime, _ = mimetypes.guess_type(filename)
                        if not mime:
                            mime = "application/octet-stream"
                        idx = len(attachments)
                        self._attachments.write(msg_id, idx, filename, bytes(f[1]))
                        attachments.append({
                            "kind":     "file",
                            "idx":      idx,
                            "filename": filename,
                            "mime":     mime,
                            "size":     len(f[1]),
                        })
                    except Exception as exc:
                        log.warning("File attachment persist failed for %s: %s",
                                    msg_id[:16] if msg_id else "?", exc)

            # FIELD_AUDIO (0x07) — [audio_mode (int), audio_bytes]. See
            # classify_audio_field(): Ogg Opus plays inline, anything else
            # (Codec2, raw Opus presets, unknown modes) is kept as a file.
            try:
                if (isinstance(audio, list) and len(audio) >= 2
                        and isinstance(audio[1], (bytes, bytearray))):
                    kind, mime, filename = classify_audio_field(audio[0], audio[1])
                    idx = len(attachments)
                    self._attachments.write(msg_id, idx, filename, bytes(audio[1]))
                    attachments.append({
                        "kind":     kind,
                        "idx":      idx,
                        "filename": filename,
                        "mime":     mime,
                        "size":     len(audio[1]),
                    })
            except Exception as exc:
                log.warning("Audio attachment persist failed for %s: %s",
                            msg_id[:16] if msg_id else "?", exc)

        entry = {
            "id":          msg_id,
            "source":      source_hex,
            "title":       title,
            "content":     content,
            "received_at": time.time(),
            "read":        False,
            "owner":       user_sub,
        }
        if attachments:
            entry["attachments"] = attachments

        log.info(
            "Received LXMF message from %s: %s (%d attachment%s)",
            source_hex[:16] if source_hex else "?",
            entry["title"] or "(no subject)",
            len(attachments),
            "" if len(attachments) == 1 else "s",
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
        attachments: Optional[list] = None,
    ) -> tuple[bool, str]:
        """Queue a message for background delivery and return immediately.

        ``attachments`` — see ``send_message`` docstring. Each item
        gets persisted to the ``AttachmentStore`` under a slot index,
        classified into ``FIELD_IMAGE`` / ``FIELD_AUDIO`` /
        ``FIELD_FILE_ATTACHMENTS`` at wire-format time (see
        ``_deliver`` below), and recorded in the sent-message
        metadata so the sender's own chat log can render the same
        bubbles the recipient sees.
        """
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

        # ------------------------------------------------------------------
        # Persist attachment blobs before we start the delivery thread,
        # while we still hold the raw bytes. On disk goes the payload;
        # the ``attachments`` metadata list goes into ``messages.json``
        # so the sent-tab chat-log renders each bubble with the same
        # attachment chips the recipient will see.
        # ------------------------------------------------------------------
        att_meta: list[dict] = []
        if attachments and self._attachments:
            for a in attachments:
                data = a.get("data") or b""
                if not isinstance(data, (bytes, bytearray)):
                    continue
                filename = a.get("filename") or "file"
                mime     = (a.get("mime") or "").lower() \
                           or "application/octet-stream"
                kind     = _classify_attachment_kind(mime)
                idx      = len(att_meta)
                try:
                    self._attachments.write(msg_id, idx, filename, bytes(data))
                except Exception as exc:
                    log.warning("Outbound attachment persist failed for %s: %s",
                                msg_id[:16], exc)
                    continue
                att_meta.append({
                    "kind":     kind,
                    "idx":      idx,
                    "filename": filename,
                    "mime":     mime,
                    "size":     len(data),
                })

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
        if att_meta:
            entry["attachments"] = att_meta
        if self._msg_store:
            self._msg_store.save_sent(entry)

        self._launch_delivery(
            msg_id, dest_hash_hex, dest_hash, title, content, user_sub,
            att_meta, router, source_dest,
        )
        return True, msg_id

    def _run_delivery(
        self,
        msg_id: str,
        dest_hash_hex: str,
        dest_hash: bytes,
        title: str,
        content: str,
        user_sub: str,
        att_meta: list,
        router,
        source_dest,
    ) -> None:
        """Build the LXMF message for a stored sent entry and hand it
        to the router. Runs on its own thread (see _launch_delivery).
        Used for the first send and for any later retry.
        """
        import RNS, LXMF
        try:
            # Wait until we can recall the recipient's identity.
            # request_path kicks off path discovery if needed.
            dest_identity = RNS.Identity.recall(dest_hash)
            if dest_identity is None:
                RNS.Transport.request_path(dest_hash)
                deadline = time.time() + PATH_WAIT
                while dest_identity is None:
                    if self._is_cancelled(msg_id):
                        return
                    if time.time() > deadline:
                        log.warning(
                            "Identity not recalled for %s after %ss — "
                            "peer may not have announced recently",
                            dest_hash_hex[:16], PATH_WAIT,
                        )
                        self._mark_failed(msg_id)
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

            # Attachments: route each persisted blob into the LXMF
            # field the recipient expects for its kind. Ordering:
            # image → FIELD_IMAGE (singleton — first one wins;
            # additional images demoted to FIELD_FILE_ATTACHMENTS),
            # audio → FIELD_AUDIO (singleton — same rule), rest →
            # FIELD_FILE_ATTACHMENTS array. Matches MeshChat's own
            # send-side structure so a two-image message shows
            # image-one inline and image-two in the file chip list
            # on both ends.
            if att_meta and self._attachments:
                image_slot = None
                audio_slot = None
                file_slots = []
                for meta in att_meta:
                    blob = self._attachments.read(msg_id, meta["idx"])
                    if blob is None:
                        log.warning(
                            "Missing blob for outbound msg %s idx %d — "
                            "skipping this attachment",
                            msg_id[:8], meta["idx"],
                        )
                        continue
                    kind = meta.get("kind")
                    mime = meta.get("mime", "")
                    name = meta.get("filename") or "file"
                    if kind == "image" and image_slot is None:
                        ext = _MIME_TO_IMAGE_EXT.get(mime.lower(), "")
                        image_slot = [ext, blob]
                    elif kind == "audio" and audio_slot is None:
                        slot = audio_field_for_send(mime, blob)
                        if slot is not None:
                            audio_slot = slot
                        else:
                            # Not real Ogg Opus (a mislabelled file) —
                            # send it as a file so the recipient can
                            # still download the bytes.
                            file_slots.append([name, blob])
                    else:
                        file_slots.append([name, blob])
                if image_slot is not None:
                    fields[0x06] = image_slot
                if audio_slot is not None:
                    fields[0x07] = audio_slot
                if file_slots:
                    fields[0x05] = file_slots

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
                self._forget(msg_id)
                real_id = _m.hash.hex() if _m.hash else msg_id
                if self._msg_store:
                    self._msg_store.update_sent(msg_id, "delivered", real_id=real_id)
                log.info("Delivered %s → %s", msg_id[:8], dest_hash_hex[:16])

            def _failed(_m):
                self._mark_failed(msg_id)
                log.warning("Delivery failed %s → %s", msg_id[:8], dest_hash_hex[:16])

            lxmf_msg.register_delivery_callback(_delivered)
            lxmf_msg.register_failed_callback(_failed)
            with self._lock:
                self._outbound[msg_id] = {"router": router, "lxm": lxmf_msg}
            if self._is_cancelled(msg_id):
                self._forget(msg_id)
                return
            router.handle_outbound(lxmf_msg)
            # A cancel that arrived while the message was being handed over
            # could not reach it (LXMF does not know it yet), so repeat it.
            if self._is_cancelled(msg_id):
                router.cancel_outbound(lxmf_msg.message_id)
                self._forget(msg_id)
            log.info("Queued LXMF message %s → %s", msg_id[:8], dest_hash_hex[:16])

        except Exception:
            log.exception("Async LXMF delivery error for %s", msg_id[:8])
            self._mark_failed(msg_id)

    # -- cancel, retry and automatic re-send ------------------------------

    def _is_cancelled(self, msg_id: str) -> bool:
        with self._lock:
            return msg_id in self._cancelled

    def _forget(self, msg_id: str) -> None:
        with self._lock:
            self._outbound.pop(msg_id, None)

    def _mark_failed(self, msg_id: str) -> None:
        """Record a failure, unless the user cancelled it first (a cancel
        is the user's decision and a late failure must not undo it)."""
        self._forget(msg_id)
        if self._is_cancelled(msg_id):
            return
        if self._msg_store:
            self._msg_store.update_sent(msg_id, "failed")

    def cancel_send(self, msg_id: str, user_sub: str) -> tuple:
        """Stop a message that has not yet been delivered. Returns
        ``(ok, reason)``. Only the sender's own queued messages can be
        cancelled; one that already went through says so."""
        entry = self._msg_store.get_sent(msg_id) if self._msg_store else None
        if entry is None or entry.get("owner", "") != user_sub:
            return False, "Message not found"
        state = entry.get("state")
        if state != "queued":
            return False, f"This message is already {state}"
        # Flag first so the delivery thread stops at its next check, then
        # tell the router in case it already has the message.
        with self._lock:
            self._cancelled.add(msg_id)
            rec = self._outbound.get(msg_id)
        if rec is not None:
            try:
                rec["router"].cancel_outbound(rec["lxm"].message_id)
            except Exception:
                log.exception("Router could not cancel %s", msg_id[:8])
        self._msg_store.update_sent(msg_id, "cancelled")
        return True, ""

    def _requeue(self, entry: dict, user_sub: str) -> bool:
        """Start delivery of a stored sent message again, under the same
        id, using the sender's current delivery identity."""
        user_data = self._get_user_router(user_sub)
        if user_data is None:
            return False
        try:
            dest_hash = bytes.fromhex(entry["dest"])
        except (KeyError, ValueError):
            return False
        msg_id = entry["id"]
        with self._lock:
            self._cancelled.discard(msg_id)
        self._msg_store.update_sent(msg_id, "queued")
        self._launch_delivery(
            msg_id, entry["dest"], dest_hash, entry.get("title", ""),
            entry.get("content", ""), user_sub, list(entry.get("attachments") or []),
            user_data["router"], user_data["dest"],
        )
        return True

    def retry_send(self, msg_id: str, user_sub: str) -> tuple:
        """Send a failed or cancelled message again. Returns ``(ok, reason)``."""
        entry = self._msg_store.get_sent(msg_id) if self._msg_store else None
        if entry is None or entry.get("owner", "") != user_sub:
            return False, "Message not found"
        if entry.get("state") not in ("failed", "cancelled"):
            return False, f"This message is {entry.get('state')}; only failed or cancelled messages can be retried"
        if not self._requeue(entry, user_sub):
            return False, "No delivery identity is available to send it"
        return True, ""

    def auto_resend_enabled(self) -> bool:
        with self._lock:
            return self._auto_resend

    def set_auto_resend(self, enabled: bool) -> None:
        with self._lock:
            self._auto_resend = bool(enabled)

    def resend_failed_for(self, dest_hash_hex: str) -> int:
        """The recipient just announced: try recent failed messages to them
        once more. Skips cancelled messages, anything older than a day, a
        message already retried too often, and a peer that was retried in
        the last couple of minutes. Returns how many were started."""
        if not dest_hash_hex or not self._msg_store or not self.auto_resend_enabled():
            return 0
        now = time.time()
        with self._lock:
            last = self._resend_last.get(dest_hash_hex, 0.0)
            if now - last < AUTO_RESEND_COOLDOWN_S:
                return 0
        started = 0
        for entry in self._msg_store.sent_messages():
            if (entry.get("dest") != dest_hash_hex or entry.get("state") != "failed"
                    or now - entry.get("sent_at", 0) > AUTO_RESEND_MAX_AGE_S
                    or (entry.get("resends") or 0) >= AUTO_RESEND_MAX_ATTEMPTS):
                continue
            if self._requeue(entry, entry.get("owner", "")):
                self._msg_store.patch_sent(entry["id"], resends=(entry.get("resends") or 0) + 1)
                started += 1
        if started:
            with self._lock:
                self._resend_last[dest_hash_hex] = now
            log.info("Re-sending %d failed message(s) to %s", started, dest_hash_hex[:16])
        return started

    def _launch_delivery(self, *args) -> None:
        threading.Thread(target=self._run_delivery, args=args, daemon=True).start()

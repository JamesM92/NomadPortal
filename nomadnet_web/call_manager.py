"""
LXST-compatible voice-call signalling engine — Phase 1a.

Ported from the NomadPortal-Android sister project's own call_manager.py
(its own Phase 1a), which reimplements just the *call state machine*
(dial/ring/answer/hangup/busy/reject) directly against RNS/LXMF, matching
LXST's real wire protocol exactly — verified there against a real local
clone of markqvist/LXST's actual source, not guessed. This file still has
zero codec knowledge: encode/decode/mic/speaker access is a browser-side
concern (WebCodecs Opus, Phase 1b — not built yet), same "opaque bytes
only" split Android's own Kotlin CallAudioEngine keeps on its side; this
class just moves opaque already-encoded bytes across the Link (see
send_audio_frame/pop_audio_frame below).

Wire protocol (verified directly against LXST/Primitives/Telephony.py +
LXST/Network.py source, on the Android side):

- Destination: ``RNS.Destination(identity, IN/OUT, SINGLE, "lxst",
  "telephony")`` — same aspect call_tracker.py's Phase 0 CallPeerTracker
  already listens for announces on.
- A call is a plain ``RNS.Link`` to the remote's telephony destination —
  the same primitive this app already uses elsewhere, nothing exotic.
- Every message over an active call Link is a plain ``RNS.Packet`` (NOT
  LXMF, NOT a Resource) with a msgpack-encoded dict payload:
  ``{0x00: [signal_int, ...]}`` for signalling,
  ``{0x01: bytes}`` for one audio frame (Phase 1b) —
  ``bytes[0]`` is a codec-type header byte: 0x00 Raw / 0x01 Opus /
  0x02 Codec2 / 0xFF Null. This class treats an audio frame as fully
  opaque bytes on both send and receive.
- Signal codes (LXST's real ``Signalling`` class): BUSY=0x00,
  REJECTED=0x01, CALLING=0x02, AVAILABLE=0x03, RINGING=0x04,
  CONNECTING=0x05, ESTABLISHED=0x06.
- Real call flow, confirmed from source:
  1. Callee's Destination sits IN, proof strategy PROVE_NONE,
     link-established callback registered.
  2. Caller resolves the callee's ``RNS.Identity`` (see ``place_call``'s
     own doc comment for how), builds an OUT destination from it, ensures
     a path is known, opens ``RNS.Link``.
  3. Callee's incoming-link callback fires: if not already busy, sends
     ``{0x00:[0x03]}`` (AVAILABLE) over the link.
  4. Caller sees AVAILABLE, calls the link's own ``identify(identity)``
     — RNS.Link's standard identify handshake, not anything custom.
  5. Callee's remote-identified callback fires once that completes: if
     the caller is allowed, sends ``{0x00:[0x04]}`` (RINGING) — the
     "phone is now audibly ringing" moment on both real ends.
  6. Callee answering sends CONNECTING then ESTABLISHED once ready;
     caller mirrors its own local state forward on each.
  7. Either side hanging up before ESTABLISHED sends REJECTED; a busy
     line sends BUSY instead of ever reaching RINGING.

Web adaptation: unlike Android (one device, one implicit user, one
global ``CallManager``), NomadPortal-web has real concurrent logged-in
accounts. ``CallManagerRegistry`` at the bottom of this file holds one
``CallManager`` per logged-in account (``user_sub``) instead — the same
"per-account instead of one global instance" pattern already used for
``RnshManager``/``MessagingService``'s own per-user routers.
"""

import logging
import queue
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

APP_NAME = "lxst"
PRIMITIVE_NAME = "telephony"

# Matches LXST's own real Telephone.ANNOUNCE_INTERVAL default (verified
# against source) — not an arbitrary choice. Same value identity_store.py's
# own ANNOUNCE_COOLDOWN already uses for LXMF identity announces.
CALL_ANNOUNCE_INTERVAL_S = 3 * 3600


class Signalling:
    """Verbatim from LXST/Primitives/Telephony.py's real Signalling
    class — these exact integer values are what's exchanged over the
    wire, not an internal choice of ours."""
    STATUS_BUSY        = 0x00
    STATUS_REJECTED    = 0x01
    STATUS_CALLING     = 0x02
    STATUS_AVAILABLE   = 0x03
    STATUS_RINGING     = 0x04
    STATUS_CONNECTING  = 0x05
    STATUS_ESTABLISHED = 0x06


class CallStatus:
    """This app's own local state names (not wire values) — deliberately
    distinct from Signalling's wire codes so a future audio-bearing phase
    can add states (e.g. "reconnecting") without colliding with wire
    semantics."""
    IDLE               = "idle"
    CALLING            = "calling"            # outgoing, path/link establishing
    RINGING_OUTGOING   = "ringing_outgoing"    # outgoing, their end is ringing
    RINGING_INCOMING   = "ringing_incoming"    # incoming, our end is ringing
    CONNECTING         = "connecting"
    ESTABLISHED        = "established"
    ENDED              = "ended"               # normal hangup, either side
    BUSY               = "busy"
    REJECTED           = "rejected"
    FAILED             = "failed"              # no path / identity unresolvable / timeout


# Real ring/connect timeouts — chosen to be generous (mesh RTT can be
# high) rather than tight, since a false timeout looks identical to a
# genuinely unreachable peer from the UI's point of view.
RING_TIMEOUT_S = 60
CONNECT_TIMEOUT_S = 20
PATH_WAIT_TIMEOUT_S = 15

# History is in-memory only — a fixed cap keeps a long-running process
# from accumulating this forever.
HISTORY_MAX = 50

# Phase 1b: a small jitter buffer for received-but-not-yet-played audio
# frames. 10 frames at ~20ms-per-frame (this app's own future codec
# target) is ~200ms of buffered audio — enough to absorb real mesh
# jitter without adding noticeable latency. Bounded so a playback side
# that's fallen behind doesn't grow this into a stale, ever-larger
# audio delay — drop-oldest is the right failure mode for live audio: a
# late frame is worse than a missing one.
AUDIO_JITTER_MAX = 10


class CallManager:
    """One call at a time (matches LXST's own single-line-busy model).
    All RNS calls are made through the ``rns`` module object passed to
    ``start()`` — injected rather than imported at module scope, so
    this class's own state-machine logic is unit-testable against a
    lightweight fake without needing real RNS/networking (see
    test_call_manager.py).
    """

    def __init__(self, on_state_change: Optional[Callable[[], None]] = None):
        self._rns = None
        self._msgpack = None
        self._identity = None
        self._destination = None
        # RLock, not Lock: hang_up()/_incoming_link_established() etc.
        # call link.teardown() while holding this lock, and RNS's own
        # Link.teardown() may invoke the closed-callback (_link_closed,
        # which itself acquires this same lock) synchronously on the
        # calling thread rather than deferring it. RLock makes the code
        # correct either way regardless of which real RNS actually does.
        self._lock = threading.RLock()
        self._on_state_change = on_state_change
        self.last_announce_at: Optional[float] = None

        # "Calls from contacts only" — same shape as MessagingService's
        # own contacts-only-messages toggle. Defaults to **True** — a
        # deliberate departure from this app's usual "nothing gets more
        # restrictive without opting in" convention: since
        # ``_calls_enabled`` below itself defaults to False, the very
        # first time an account turns calls on at all, this being
        # already-on means that decision doesn't *also* silently open
        # the door to every stranger.
        self._contacts_only = True
        self._is_known_contact: Optional[Callable[[str], bool]] = None

        # Master "allow incoming voice calls at all" toggle. Independent
        # of, and enforced *before*, ``_contacts_only`` above — this is
        # "no calls at all", not "no calls from strangers". Defaults to
        # **False** — voice calls are opt-in, not already on the first
        # time an account finds the toggle.
        self._calls_enabled = False

        # Instance attributes (not bare module constants) specifically
        # so tests can shrink them — a real 15s path-wait would make the
        # "no path found" test suite take 15 real seconds otherwise.
        self.path_wait_timeout_s = PATH_WAIT_TIMEOUT_S
        self.ring_timeout_s = RING_TIMEOUT_S

        self.status = CallStatus.IDLE
        self.link = None
        self.is_incoming = False
        self.remote_identity_hash: Optional[str] = None
        self.started_at: Optional[float] = None
        self.established_at: Optional[float] = None
        self.ended_reason: Optional[str] = None

        # Set True once the callee has actually tapped Answer — used to
        # tell a stray late signalling packet from the real answer path.
        self._answered = False

        # Real call history — capped at HISTORY_MAX, in-memory only for
        # now (not yet persisted across a process restart).
        self.history: list = []

        # Phase 1b: received-audio-frame jitter buffer. A plain
        # queue.Queue rather than anything lock-protected by self._lock
        # — Queue is already internally thread-safe, and the playback
        # side's pop_audio_frame() needs to block (with a timeout)
        # without holding self._lock, since holding it across a
        # timeout-length block would stall every other call operation
        # (answer/hangup/status polling) for that long. Drained on every
        # call end so a new call never plays back stale audio left over
        # from the previous one.
        self._audio_rx_queue: "queue.Queue" = queue.Queue(maxsize=AUDIO_JITTER_MAX)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def start(self, rns_module, identity, msgpack_module=None) -> None:
        """Brings up this account's own telephony Destination so it can
        receive calls. rns_module is the real ``RNS`` module (passed in,
        not imported here, for testability); identity is the account's
        currently-active RNS.Identity."""
        if msgpack_module is None:
            import RNS.vendor.umsgpack as msgpack_module
        self._rns = rns_module
        self._msgpack = msgpack_module
        self._identity = identity
        self._destination = rns_module.Destination(
            identity, rns_module.Destination.IN, rns_module.Destination.SINGLE,
            APP_NAME, PRIMITIVE_NAME,
        )
        self._destination.set_proof_strategy(rns_module.Destination.PROVE_NONE)
        self._destination.set_link_established_callback(self._incoming_link_established)
        log.info("Call engine listening on %s", rns_module.prettyhexrep(self._destination.hash))

    def announce(self) -> None:
        if self._destination:
            self._destination.announce()
            self.last_announce_at = time.time()

    def set_calls_enabled(self, enabled: bool) -> None:
        """Master "Allow incoming voice calls" toggle. Hangs up any call
        already in progress the instant this flips off — the
        destination stays registered and announced; instead, every
        incoming link is rejected at _incoming_link_established, before
        identification, so no call ever gets far enough to ring."""
        self._calls_enabled = bool(enabled)
        if not self._calls_enabled:
            self.hang_up()

    def get_calls_enabled(self) -> bool:
        return self._calls_enabled

    def set_contacts_only(self, enabled: bool) -> None:
        """"Calls from contacts only" — the calls-specific counterpart
        to MessagingService's own contacts-only-messages toggle."""
        self._contacts_only = bool(enabled)

    def get_contacts_only(self) -> bool:
        return self._contacts_only

    def set_contact_checker(self, is_known_contact: Optional[Callable[[str], bool]]) -> None:
        """Injects the real "is this identity hash a known contact"
        lookup — wired to ContactStoreManager once, at call-manager
        start time."""
        self._is_known_contact = is_known_contact

    def _allows_caller(self, identity_hash_hex: str) -> bool:
        """True unless contacts-only mode is on and this caller isn't a
        known contact — mirrors MessagingService's own contacts-only
        enforcement exactly (same "known contact" definition: a real
        ContactStore entry, not merely "has called before")."""
        if not self._contacts_only:
            return True
        if self._is_known_contact is None:
            return True  # Fail open — no checker wired means enforcement can't run.
        return self._is_known_contact(identity_hash_hex)

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def resolve_identity(self, address_hex: str, allow_path_request: bool = True):
        """Turns a hex address the UI hands us into a real RNS.Identity,
        or None if it genuinely can't be resolved.

        Tries both of RNS.Identity.recall()'s real modes first, since
        the caller may be handing us either shape and shouldn't have to
        know which:
        - A destination hash (from_identity_hash=False, the default) —
          what a user pastes in for manual address entry is naturally
          their already-familiar LXMF address, not a dedicated "call
          address" LXST has no convention for sharing separately. Once
          resolved to an Identity this way, a telephony Destination is
          built from that same Identity regardless of which aspect the
          hash we resolved it from actually announced under — this is
          real RNS.Identity.recall() behavior: an Identity object isn't
          aspect-specific.
        - An identity hash (from_identity_hash=True) — what
          CallPeerTracker/LXMFPeerTracker's own identity_hash field
          gives us for a contact already known to be call-capable (the
          Phase 0 phone-icon path).

        recall() is a *pure local cache lookup* — it returns None for
        any hash RNS has never seen an announce or path-response for,
        regardless of whether the address is real and reachable.
        request_path() (treating the raw address as a destination hash)
        fixes it — RNS's own path-response protocol carries the
        identity's public key even when nothing proactively announced,
        the same mechanism this app's own "message a never-seen
        address" flow already relies on (see messaging.py's PATH_WAIT
        handling). allow_path_request=False is for call sites that only
        want the cheap cache check.
        """
        try:
            address = bytes.fromhex(address_hex)
        except (ValueError, TypeError):
            return None
        identity = self._rns.Identity.recall(address, from_identity_hash=False)
        if identity is None:
            identity = self._rns.Identity.recall(address, from_identity_hash=True)
        if identity is not None or not allow_path_request:
            return identity

        if not self._rns.Transport.has_path(address):
            log.info("resolve_identity: no cached identity/path for %s, requesting path...", address_hex)
            self._rns.Transport.request_path(address)
            deadline = time.time() + self.path_wait_timeout_s
            poll_interval = min(0.2, self.path_wait_timeout_s)
            while not self._rns.Transport.has_path(address) and time.time() < deadline:
                time.sleep(poll_interval)
        return self._rns.Identity.recall(address, from_identity_hash=False)

    def place_call(self, address_hex: str) -> tuple:
        """Returns (success: bool, message: str). address_hex may be a
        destination hash or an identity hash — see resolve_identity()'s
        own doc comment. Blocks the calling thread while a path is
        looked up if one isn't already known (mirrors LXST's own call()
        exactly) — callers must not invoke this from a hot/latency-
        sensitive path (see routes.py's own /api/calls/place doc
        comment)."""
        log.info("place_call(%s)", address_hex)
        with self._lock:
            if self._rns is None:
                # start() hasn't run yet — a real reachable state, not a
                # bug. Fail cleanly rather than AttributeError-ing on
                # self._rns.Destination below.
                log.warning("place_call failed: engine not started yet")
                return False, "Call engine not ready yet"
            self._clear_terminal_state_locked()
            if self.status != CallStatus.IDLE:
                log.warning("place_call failed: already in status %s", self.status)
                return False, "Already on a call"
            identity = self.resolve_identity(address_hex)
            if identity is None:
                log.warning("place_call failed: could not resolve identity for %s", address_hex)
                return False, "Unknown address — no path/identity known for it yet"
            log.info("place_call: resolved identity %s for address %s", identity.hash.hex(), address_hex)

            call_destination = self._rns.Destination(
                identity, self._rns.Destination.OUT, self._rns.Destination.SINGLE,
                APP_NAME, PRIMITIVE_NAME,
            )
            if not self._rns.Transport.has_path(call_destination.hash):
                log.info(
                    "place_call: no path yet to telephony destination %s, requesting...",
                    call_destination.hash.hex(),
                )
                self._rns.Transport.request_path(call_destination.hash)
                deadline = time.time() + self.path_wait_timeout_s
                poll_interval = min(0.2, self.path_wait_timeout_s)
                while not self._rns.Transport.has_path(call_destination.hash) and time.time() < deadline:
                    time.sleep(poll_interval)
                if not self._rns.Transport.has_path(call_destination.hash):
                    log.warning(
                        "place_call failed: no path found to telephony destination %s within %ss",
                        call_destination.hash.hex(), self.path_wait_timeout_s,
                    )
                    return False, "No path found to that address"
                log.info("place_call: path found to %s", call_destination.hash.hex())

            self.status = CallStatus.CALLING
            self.is_incoming = False
            self.remote_identity_hash = identity.hash.hex()
            self.started_at = time.time()
            self.ended_reason = None
            self._answered = False
            self._notify()

            self.link = self._rns.Link(
                call_destination,
                established_callback=self._outgoing_link_established,
                closed_callback=self._link_closed,
            )
            self._start_timeout(self.ring_timeout_s, expected_status_below=CallStatus.ESTABLISHED)
            return True, "Calling"

    def _outgoing_link_established(self, link) -> None:
        link.set_packet_callback(self._packet_received)
        # Real signal exchange starts once the callee's own
        # link-established handler sends AVAILABLE — handled in
        # _packet_received below, matching LXST's own flow exactly.

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def _incoming_link_established(self, link) -> None:
        with self._lock:
            self._clear_terminal_state_locked()
            if self.status != CallStatus.IDLE:
                log.info("Incoming call while line busy, signalling BUSY")
                self._send_signal(link, Signalling.STATUS_BUSY)
                link.teardown()
                return
            if not self._calls_enabled:
                # Rejected before identification — the master toggle
                # blocks everyone, not just non-contacts, so there's no
                # need to wait for the caller's identity to decide.
                log.info("Incoming call link established while voice calls disabled, signalling BUSY")
                self._send_signal(link, Signalling.STATUS_BUSY)
                link.teardown()
                return
            link.set_remote_identified_callback(self._caller_identified)
            link.set_link_closed_callback(self._link_closed)
            link.set_packet_callback(self._packet_received)
            self._send_signal(link, Signalling.STATUS_AVAILABLE)

    def _caller_identified(self, link, identity) -> None:
        with self._lock:
            self._clear_terminal_state_locked()
            if self.status != CallStatus.IDLE:
                log.info("Caller identified while line busy, signalling BUSY")
                self._send_signal(link, Signalling.STATUS_BUSY)
                link.teardown()
                return
            caller_hash_hex = identity.hash.hex()
            if not self._allows_caller(caller_hash_hex):
                # Signals BUSY rather than a distinct "rejected" status —
                # deliberately indistinguishable from "the callee is
                # genuinely on another call" from the caller's own side,
                # same privacy reasoning MessagingService's own
                # contacts-only enforcement already applies. Never
                # rings, never surfaces to the UI at all.
                log.info(
                    "Incoming call from non-contact %s, contacts-only calls is on — signalling BUSY",
                    self._rns.prettyhexrep(identity.hash),
                )
                self._send_signal(link, Signalling.STATUS_BUSY)
                link.teardown()
                return
            log.info("Incoming call from %s", self._rns.prettyhexrep(identity.hash))
            self.link = link
            self.is_incoming = True
            self.remote_identity_hash = identity.hash.hex()
            self.status = CallStatus.RINGING_INCOMING
            self.started_at = time.time()
            self.ended_reason = None
            self._answered = False
            self._send_signal(link, Signalling.STATUS_RINGING)
            self._start_timeout(self.ring_timeout_s, expected_status_below=CallStatus.ESTABLISHED)
            self._notify()

    def answer_call(self) -> tuple:
        """Callee accepts a RINGING_INCOMING call. Returns (success, message)."""
        with self._lock:
            if self.status != CallStatus.RINGING_INCOMING or self.link is None:
                return False, "No incoming call to answer"
            self._answered = True
            self.status = CallStatus.CONNECTING
            self._send_signal(self.link, Signalling.STATUS_CONNECTING)
            self.status = CallStatus.ESTABLISHED
            self.established_at = time.time()
            self._send_signal(self.link, Signalling.STATUS_ESTABLISHED)
            self._notify()
            return True, "Answered"

    # ------------------------------------------------------------------
    # Signalling receive (both directions share this)
    # ------------------------------------------------------------------

    def _packet_received(self, data, packet) -> None:
        try:
            unpacked = self._msgpack.unpackb(data)
        except Exception as exc:
            log.warning("Could not decode call signalling packet: %s", exc)
            return
        if not isinstance(unpacked, dict):
            return
        source = packet.link if hasattr(packet, "link") else self.link
        if 0x01 in unpacked:
            self._handle_audio_frame(unpacked[0x01], source)
        if 0x00 in unpacked:
            signals = unpacked[0x00]
            if not isinstance(signals, list):
                signals = [signals]
            for signal in signals:
                self._handle_signal(signal, source)

    def _handle_audio_frame(self, frame, source) -> None:
        # Deliberately much cheaper than _handle_signal: only holds the
        # lock long enough to read status/link (never while touching the
        # queue) — an audio frame arrives up to ~50x/sec, so this runs
        # on the hot path.
        with self._lock:
            active = self.status == CallStatus.ESTABLISHED and source == self.link
        if not active or not isinstance(frame, (bytes, bytearray)):
            return  # stray/late frame outside an active established call
        try:
            self._audio_rx_queue.put_nowait(bytes(frame))
        except queue.Full:
            try:
                self._audio_rx_queue.get_nowait()  # drop the oldest…
            except queue.Empty:
                pass
            try:
                self._audio_rx_queue.put_nowait(bytes(frame))  # …then queue the newest
            except queue.Full:
                pass  # a concurrent pop already made room; fine either way

    def _handle_signal(self, signal: int, source) -> None:
        with self._lock:
            if source != self.link:
                log.info("Ignoring signal 0x%02x from a non-active link", signal)
                return
            if signal == Signalling.STATUS_BUSY:
                log.info("Remote signalled BUSY")
                self._end_call(CallStatus.BUSY, "Remote is busy")
            elif signal == Signalling.STATUS_REJECTED:
                log.info("Remote signalled REJECTED")
                self._end_call(CallStatus.REJECTED, "Call was rejected")
            elif signal == Signalling.STATUS_AVAILABLE:
                # Caller side only: line is free, identify ourselves —
                # RNS.Link's own standard handshake, not custom.
                if not self.is_incoming and self._identity is not None:
                    self.link.identify(self._identity)
            elif signal == Signalling.STATUS_RINGING:
                if not self.is_incoming:
                    self.status = CallStatus.RINGING_OUTGOING
                    self._notify()
            elif signal == Signalling.STATUS_CONNECTING:
                if not self.is_incoming:
                    self.status = CallStatus.CONNECTING
                    self._notify()
            elif signal == Signalling.STATUS_ESTABLISHED:
                if not self.is_incoming:
                    self.status = CallStatus.ESTABLISHED
                    self.established_at = time.time()
                    self._notify()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def hang_up(self) -> tuple:
        """User-initiated end — from either the caller or the callee, at
        any point in the call. Returns (success, message)."""
        with self._lock:
            if self.status == CallStatus.IDLE:
                return False, "No active call"
            was_ringing_incoming_unanswered = (
                self.status == CallStatus.RINGING_INCOMING and not self._answered
            )
            link = self.link
            if link is not None:
                if was_ringing_incoming_unanswered:
                    self._send_signal(link, Signalling.STATUS_REJECTED)
                try:
                    if link.status == self._rns.Link.ACTIVE:
                        link.teardown()
                except Exception:
                    pass
            # link.teardown() above can synchronously invoke
            # _link_closed on the calling thread (RLock is what makes
            # that safe at all — see __init__'s own comment) — which
            # already calls _end_call and clears self.link. Without this
            # guard, hang_up() would unconditionally call _end_call
            # again right after, double-recording one hangup as two
            # history entries. _end_call always clears self.link, so
            # its absence here means the callback already ran; only
            # call it ourselves if it didn't (covers real RNS deferring
            # the callback instead, if it does).
            if self.link is not None:
                self._end_call(CallStatus.ENDED, "Call ended")
            return True, "Hung up"

    def _link_closed(self, link) -> None:
        with self._lock:
            if link != self.link:
                return
            log.info("Link closed (was in status %s)", self.status)
            if self.status not in (CallStatus.ENDED, CallStatus.BUSY, CallStatus.REJECTED, CallStatus.FAILED):
                self._end_call(CallStatus.ENDED, "Remote hung up")

    def _end_call(self, status: str, reason: str) -> None:
        # Caller must already hold self._lock.
        log.info(
            "Call ended: status=%s reason=%s remote=%s incoming=%s",
            status, reason, self.remote_identity_hash, self.is_incoming,
        )
        self._record_history(status, reason)
        self.status = status
        self.ended_reason = reason
        self.link = None
        self._drain_audio_queue()
        self._notify()

    def _drain_audio_queue(self) -> None:
        # Caller must already hold self._lock. Runs on every call end so
        # a new call starting right after never plays back audio left
        # over from the previous one.
        while True:
            try:
                self._audio_rx_queue.get_nowait()
            except queue.Empty:
                break

    def _record_history(self, status: str, reason: str) -> None:
        # Caller must already hold self._lock.
        self.history.insert(0, {
            "is_incoming": self.is_incoming,
            "remote_identity_hash": self.remote_identity_hash,
            "started_at": self.started_at,
            "established_at": self.established_at,
            "ended_at": time.time(),
            "status": status,
            "reason": reason,
        })
        del self.history[HISTORY_MAX:]

    def get_history(self) -> list:
        with self._lock:
            return list(self.history)

    def reset_after_end(self) -> None:
        """Clears a terminal state (ENDED/BUSY/REJECTED/FAILED) back to
        IDLE so the *UI* can stop showing it — deliberately not called
        automatically from _end_call itself: the call overlay needs a
        moment to actually show "call ended" rather than the state
        instantly disappearing back to idle."""
        with self._lock:
            if self._clear_terminal_state_locked():
                self._notify()

    def _clear_terminal_state_locked(self) -> bool:
        """Caller must already hold self._lock. Returns True if a
        terminal state was actually cleared.

        A terminal status (ENDED/BUSY/REJECTED/FAILED) means the
        *previous* call is over, not that the line is still busy —
        without this, any new call arriving (or being placed) in the
        short window before the UI calls reset_after_end() would be
        incorrectly signalled BUSY / refused locally, even though
        nothing was actually happening on this end anymore. Called from
        every call-starting path directly (not just reset_after_end())
        so a new call is never stuck waiting on the UI's own dismiss
        timer to get through."""
        if self.status in (CallStatus.ENDED, CallStatus.BUSY, CallStatus.REJECTED, CallStatus.FAILED):
            self.status = CallStatus.IDLE
            self.is_incoming = False
            self.remote_identity_hash = None
            self.started_at = None
            self.established_at = None
            self._answered = False
            return True
        return False

    def _send_signal(self, link, signal: int) -> None:
        try:
            packet = self._rns.Packet(link, self._msgpack.packb({0x00: [signal]}), create_receipt=False)
            packet.send()
        except Exception as exc:
            log.warning("Could not send call signal 0x%02x: %s", signal, exc)

    # ------------------------------------------------------------------
    # Audio frame relay (Phase 1b) — the browser side is the only caller
    # of both of these once it exists. frame/return value are always
    # opaque bytes: a 1-byte codec-type header followed by an
    # already-encoded audio frame. This class never inspects or decodes
    # them.
    # ------------------------------------------------------------------

    def send_audio_frame(self, frame: bytes) -> bool:
        """Sends one already-encoded audio frame over the active call's
        Link, mirroring _send_signal's own fire-and-forget RNS.Packet
        pattern (create_receipt=False — a late/dropped audio frame
        should never be retransmitted, unlike a signal). Gated on
        ESTABLISHED as defense-in-depth."""
        with self._lock:
            link = self.link
            established = self.status == CallStatus.ESTABLISHED
        if not established or link is None:
            return False
        try:
            packet = self._rns.Packet(link, self._msgpack.packb({0x01: bytes(frame)}), create_receipt=False)
            packet.send()
            return True
        except Exception as exc:
            log.warning("Could not send call audio frame: %s", exc)
            return False

    def pop_audio_frame(self, timeout_s: float = 0.5) -> Optional[bytes]:
        """Blocks the calling thread for up to timeout_s waiting for a
        received audio frame, returning None on timeout/empty — the
        actual pull mechanism for playback, not a poll-with-sleep loop.
        Deliberately does NOT acquire self._lock: blocking on the queue
        while holding it would stall every other call operation
        (answer/hangup/status polling) for up to timeout_s on every
        single frame."""
        try:
            return self._audio_rx_queue.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def _start_timeout(self, timeout_s: float, expected_status_below: str) -> None:
        call_link = self.link

        def job():
            time.sleep(timeout_s)
            with self._lock:
                if self.link is call_link and self.status not in (
                    CallStatus.ESTABLISHED, CallStatus.ENDED, CallStatus.BUSY,
                    CallStatus.REJECTED, CallStatus.FAILED,
                ):
                    log.info("Call timed out in status %s", self.status)
                    if self.link is not None:
                        try:
                            if self.link.status == self._rns.Link.ACTIVE:
                                self.link.teardown()
                        except Exception:
                            pass
                    self._end_call(CallStatus.FAILED, "Timed out")

        threading.Thread(target=job, daemon=True).start()

    def _notify(self) -> None:
        if callable(self._on_state_change):
            try:
                self._on_state_change()
            except Exception:
                pass

    def status_dict(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "is_incoming": self.is_incoming,
                "remote_identity_hash": self.remote_identity_hash,
                "started_at": self.started_at,
                "established_at": self.established_at,
                "ended_reason": self.ended_reason,
            }


class CallManagerRegistry:
    """Holds one ``CallManager`` per logged-in account (``user_sub``) —
    the web-specific counterpart to Android's single module-level
    ``_call_manager``. See this module's own top doc comment for why.

    ``contact_store``/``call_settings`` are injected (not imported)
    dependencies, same "inject, don't import" testability convention
    ``CallManager`` itself already follows — a fake/None of either
    keeps this class constructible and testable without the real
    per-user stores.
    """

    def __init__(self, contact_store=None, call_settings=None):
        self._lock = threading.Lock()
        self._managers: dict = {}
        self._contact_store = contact_store
        self._call_settings = call_settings
        self._announce_loop_started = False

    def setup_user(self, user_sub: str, identity) -> Optional[CallManager]:
        """Brings up (or re-uses) this account's call engine, wiring in
        its persisted settings and contact-checker. Idempotent for the
        same identity — safe to call on every login. If [identity]
        genuinely changed (e.g. the account reset its keypair), rebuilds
        the receiving Destination so incoming calls route to the new
        one; matches the NomadPortal-Android sister project's own
        ``_start_call_manager()`` in *not* handling a live multi-identity
        switch specially — its own ``switch_active_identity()`` doesn't
        touch ``_call_manager`` either, so this stays in scope-parity
        with the reference implementation rather than adding handling
        upstream itself doesn't have.
        """
        if identity is None:
            return None
        with self._lock:
            mgr = self._managers.get(user_sub)
        if mgr is None:
            mgr = CallManager()
            with self._lock:
                self._managers[user_sub] = mgr
        if mgr._identity is None or getattr(mgr._identity, "hash", None) != getattr(identity, "hash", None):
            import RNS
            mgr.start(RNS, identity)
            mgr.announce()

        if self._call_settings is not None:
            settings = self._call_settings.for_user(user_sub)
            mgr.set_calls_enabled(settings.get_calls_enabled())
            mgr.set_contacts_only(settings.get_contacts_only())

        if self._contact_store is not None:
            store = self._contact_store.for_user(user_sub)
            mgr.set_contact_checker(lambda h: store.get(h) is not None)

        return mgr

    def get(self, user_sub: str) -> Optional[CallManager]:
        with self._lock:
            return self._managers.get(user_sub)

    def start_announce_loop(self) -> None:
        """Idempotent — a second call is a no-op. One shared background
        thread re-announcing every started account's telephony
        Destination every CALL_ANNOUNCE_INTERVAL_S, rather than one
        thread per account (matches this app's own general "shared
        ticking service, not N per-account threads" preference — see
        PropagationSyncService)."""
        with self._lock:
            if self._announce_loop_started:
                return
            self._announce_loop_started = True
        threading.Thread(target=self._announce_loop, daemon=True, name="call-auto-announce").start()

    def _announce_loop(self) -> None:
        while True:
            time.sleep(60)
            with self._lock:
                managers = list(self._managers.values())
            for mgr in managers:
                if mgr._destination is None:
                    continue
                last = mgr.last_announce_at
                if last is None or time.time() - last >= CALL_ANNOUNCE_INTERVAL_S:
                    mgr.announce()

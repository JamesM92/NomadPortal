"""RNS SSH-like shell client — real interop with rnsh
(github.com/acehoss/rnsh, MIT License, Copyright (c) 2023 Aaron Heise),
ported from the NomadPortal-Android sister project's own copy of this
module (confirmed against rnsh's actual `initiator.py`/`protocol.py`
source there, not guessed). This reimplements the *client* (initiator)
side of rnsh's real wire protocol using `RNS.Link`/`RNS.Channel`
directly — no `pty`/`termios`/`tty`/`fcntl`/`pwd` anywhere, unlike
rnsh's own reference CLI, which assumes a real POSIX terminal on both
ends and isn't reusable as a library as-is. The wire protocol itself —
`WindowSizeMessage`/`ExecuteCommandMessage`/`StreamDataMessage`/
`VersionInfoMessage`/`ErrorMessage`/`CommandExitedMessage` below — is
just `RNS.MessageBase`/`RNS.Channel` definitions, genuinely portable,
ported directly from rnsh's own `protocol.py` (same magic byte, message
type numbers, and msgpack field order, so a real `rnsh listener` on the
other end can't tell the difference).

**Client (initiator) only, deliberately.** This app only ever connects
OUT to a remote `rnsh listener` someone else already runs and controls;
there is no listener implementation here at all, and none should ever
be added. A listener means anyone with the right destination hash gets
a real shell *on this device* — a fundamentally different risk category
than anything else this app does. Don't "complete the pair" by adding
one later without an explicit, separate decision to do so.

**"Line mode" only, deliberately.** Real rnsh's own reference client
also supports this as a first-class, documented mode (its own `~L`
escape sequence toggles it, mid-session): the user composes a full line
locally, then it's flushed to the remote shell on Enter. This module
doesn't attempt true character-at-a-time raw terminal mode (arrow-key
shell history, live tab-completion) — a browser tab has no raw-keystroke
terminal mode to hook into cleanly either, same honestly-scoped
limitation the Android app's own version of this module already
documents.

Web adaptation: unlike the Android app (one device, one implicit user,
one global session), NomadPortal-web has real concurrent logged-in
accounts. `RnshManager` below holds at most one `RnshSession` *per
account* (`user_sub`), rather than a single module-level session —
otherwise two different logged-in accounts opening the terminal panel
would silently steal each other's shell.
"""

import logging
import threading
import time

log = logging.getLogger(__name__)

MSG_MAGIC = 0xac
PROTOCOL_VERSION = 1

# The RNS destination app_name rnsh's own listener/initiator both use
# (confirmed directly against rnsh.py's own `APP_NAME = "rnsh"` and its
# `RNS.Destination(identity, ..., APP_NAME)` call sites) — a single-
# component destination name, no aspect suffix. Must match exactly for
# a real rnsh listener to even be found via path discovery.
APP_NAME = "rnsh"

# How long to wait for path discovery / link establishment / the
# version handshake before giving up — generous, since a real mesh
# path can take a while, but still bounded so a bad destination hash
# fails visibly instead of hanging the UI forever.
CONNECT_TIMEOUT_S = 20.0

SW_VERSION = "nomadportal-web"


def _make_msgtype(val: int) -> int:
    return ((MSG_MAGIC << 8) & 0xff00) | (val & 0x00ff)


def _register_message_types():
    """Deferred import + class definitions — `RNS` (and therefore
    `RNS.MessageBase`, which these all subclass) isn't necessarily
    importable at module load time on every code path that imports
    this module (matches this codebase's own established `import RNS`
    -inside-the-function convention elsewhere, e.g. messaging.py's own
    `_deliver()`). Returns the ordered dict `RnshSession` registers on
    its channel."""
    import RNS
    from RNS.vendor import umsgpack
    from RNS.Buffer import StreamDataMessage as RNSStreamDataMessage

    class NoopMessage(RNS.MessageBase):
        MSGTYPE = _make_msgtype(0)

        def pack(self) -> bytes:
            return bytes()

        def unpack(self, raw):
            pass

    class WindowSizeMessage(RNS.MessageBase):
        MSGTYPE = _make_msgtype(2)

        def __init__(self, rows: int = None, cols: int = None, hpix: int = None, vpix: int = None):
            super().__init__()
            self.rows, self.cols, self.hpix, self.vpix = rows, cols, hpix, vpix

        def pack(self) -> bytes:
            return umsgpack.packb((self.rows, self.cols, self.hpix, self.vpix))

        def unpack(self, raw):
            self.rows, self.cols, self.hpix, self.vpix = umsgpack.unpackb(raw)

    class ExecuteCommandMessage(RNS.MessageBase):
        MSGTYPE = _make_msgtype(3)

        def __init__(self, cmdline=None, pipe_stdin: bool = False, pipe_stdout: bool = False,
                     pipe_stderr: bool = False, tcflags=None, term: str = None, rows: int = None,
                     cols: int = None, hpix: int = None, vpix: int = None):
            super().__init__()
            self.cmdline = cmdline
            self.pipe_stdin = pipe_stdin
            self.pipe_stdout = pipe_stdout
            self.pipe_stderr = pipe_stderr
            self.tcflags = tcflags
            self.term = term
            self.rows = rows
            self.cols = cols
            self.hpix = hpix
            self.vpix = vpix

        def pack(self) -> bytes:
            return umsgpack.packb((self.cmdline, self.pipe_stdin, self.pipe_stdout, self.pipe_stderr,
                                    self.tcflags, self.term, self.rows, self.cols, self.hpix, self.vpix))

        def unpack(self, raw):
            (self.cmdline, self.pipe_stdin, self.pipe_stdout, self.pipe_stderr, self.tcflags, self.term,
             self.rows, self.cols, self.hpix, self.vpix) = umsgpack.unpackb(raw)

    # A real, distinct MSGTYPE from RNS's own base StreamDataMessage —
    # matches rnsh's own protocol.py exactly ("Create a version of
    # RNS.Buffer.StreamDataMessage that we control").
    class StreamDataMessage(RNSStreamDataMessage):
        MSGTYPE = _make_msgtype(4)
        STREAM_ID_STDIN = 0
        STREAM_ID_STDOUT = 1
        STREAM_ID_STDERR = 2

    class VersionInfoMessage(RNS.MessageBase):
        MSGTYPE = _make_msgtype(5)

        def __init__(self, sw_version: str = None):
            super().__init__()
            self.sw_version = sw_version or SW_VERSION
            self.protocol_version = PROTOCOL_VERSION

        def pack(self) -> bytes:
            return umsgpack.packb((self.sw_version, self.protocol_version))

        def unpack(self, raw):
            self.sw_version, self.protocol_version = umsgpack.unpackb(raw)

    class ErrorMessage(RNS.MessageBase):
        MSGTYPE = _make_msgtype(6)

        def __init__(self, msg: str = None, fatal: bool = False, data=None):
            super().__init__()
            self.msg, self.fatal, self.data = msg, fatal, data

        def pack(self) -> bytes:
            return umsgpack.packb((self.msg, self.fatal, self.data))

        def unpack(self, raw):
            self.msg, self.fatal, self.data = umsgpack.unpackb(raw)

    class CommandExitedMessage(RNS.MessageBase):
        MSGTYPE = _make_msgtype(7)

        def __init__(self, return_code: int = None):
            super().__init__()
            self.return_code = return_code

        def pack(self) -> bytes:
            return umsgpack.packb(self.return_code)

        def unpack(self, raw):
            self.return_code = umsgpack.unpackb(raw)

    return {
        "Noop": NoopMessage,
        "WindowSize": WindowSizeMessage,
        "ExecuteCommand": ExecuteCommandMessage,
        "StreamData": StreamDataMessage,
        "VersionInfo": VersionInfoMessage,
        "Error": ErrorMessage,
        "CommandExited": CommandExitedMessage,
    }


class RnshSession:
    """One rnsh client session — connecting, connected, or ended.

    All public methods are safe to call from any thread — the Flask
    request handlers that drive this call these directly, while
    `_connect()`'s own background thread and RNS's own callback threads
    mutate the same state under `self._lock`.
    """

    STATE_CONNECTING = "connecting"
    STATE_CONNECTED = "connected"
    STATE_CLOSED = "closed"
    STATE_FAILED = "failed"

    def __init__(self, identity, destination_hash_hex: str):
        """[identity] is the caller's own RNS identity — the exact same
        one LXMF messaging already uses for this account's currently
        active identity (reused, not a separate rnsh-specific
        identity), passed to `link.identify()` as rnsh's own real auth
        mechanism (per its README: "each initiator has an identity hash
        which is used as an authentication mechanism on Reticulum" —
        i.e. the listener operator allowlists specific identity
        hashes)."""
        self._identity = identity
        self._destination_hash_hex = destination_hash_hex.lower()
        self._link = None
        self._channel = None
        self._msg = None  # populated once _register_message_types() succeeds
        self._lock = threading.Lock()
        self._state = RnshSession.STATE_CONNECTING
        self._error = None
        self._exit_code = None
        self._output_buffer = bytearray()
        self._version_event = threading.Event()
        self._version_ok = False

    def start(self) -> None:
        """Kicks off connection in the background — returns immediately.
        Real state is read via `status()`, polled from the browser (no
        server-push mechanism for this — same polling convention the
        rest of this app's near-real-time UI already uses)."""
        threading.Thread(target=self._connect, daemon=True, name="rnsh-client").start()

    def _connect(self) -> None:
        import RNS
        try:
            self._msg = _register_message_types()
        except Exception as exc:
            self._fail(f"Could not load rnsh protocol: {exc}")
            return

        try:
            dest_hash = bytes.fromhex(self._destination_hash_hex)
        except ValueError:
            self._fail("Invalid destination hash")
            return
        if len(dest_hash) != RNS.Reticulum.TRUNCATED_HASHLENGTH // 8:
            self._fail("Destination hash is the wrong length")
            return

        # Each phase (path discovery, then link establishment) gets its
        # own full CONNECT_TIMEOUT_S budget on its own clock — a single
        # shared deadline across both was a real bug found on the
        # Android sister project via an on-device test against a real
        # rnsh listener: path discovery alone can eat most of a 20s
        # budget on a real mesh, silently starving the link-establishment
        # wait that follows it and producing a misleading "could not
        # establish a link" failure for what was actually just "ran out
        # of time," not a real link problem.
        path_deadline = time.time() + CONNECT_TIMEOUT_S
        if not RNS.Transport.has_path(dest_hash):
            RNS.Transport.request_path(dest_hash)
            while not RNS.Transport.has_path(dest_hash):
                if time.time() > path_deadline:
                    self._fail("No path to that destination — it may not have announced recently")
                    return
                time.sleep(0.25)

        listener_identity = RNS.Identity.recall(dest_hash)
        if listener_identity is None:
            self._fail("Could not recall the listener's identity")
            return

        try:
            destination = RNS.Destination(
                listener_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, APP_NAME,
            )
            link = RNS.Link(destination)
        except Exception as exc:
            self._fail(f"Could not create link: {exc}")
            return
        link.set_link_closed_callback(self._on_link_closed)

        link_deadline = time.time() + CONNECT_TIMEOUT_S
        while link.status != RNS.Link.ACTIVE:
            if link.status == RNS.Link.CLOSED:
                self._fail("Link closed before it became active")
                return
            if time.time() > link_deadline:
                self._fail("Could not establish a link to that destination")
                return
            time.sleep(0.1)

        # Real auth step — see __init__'s own doc comment. A short
        # delay first so the listener has fully entered its own
        # wait-for-identify state, matching rnsh's own initiator.py
        # (`await asyncio.sleep(max(_link.rtt * 10, 0.05))`).
        time.sleep(max(link.rtt * 10, 0.05))
        link.identify(self._identity)

        channel = link.get_channel()
        for message_type in self._msg.values():
            channel.register_message_type(message_type)
        channel.add_message_handler(self._on_message)

        self._link = link
        self._channel = channel

        channel.send(self._msg["VersionInfo"](sw_version=SW_VERSION))
        if not self._version_event.wait(timeout=max(link.rtt * 20, 5)):
            self._fail("No response from listener (protocol handshake timed out)")
            return
        if not self._version_ok:
            return  # _fail() already ran from the error-message path

        # Interactive shell (cmdline=None — same as rnsh's own default
        # when no command is given), 80x24 until the UI sends a real
        # WindowSizeMessage (see resize()).
        channel.send(self._msg["ExecuteCommand"](
            cmdline=None, pipe_stdin=False, pipe_stdout=False, pipe_stderr=False,
            tcflags=None, term="xterm-256color", rows=24, cols=80,
        ))

        with self._lock:
            if self._state == RnshSession.STATE_CONNECTING:
                self._state = RnshSession.STATE_CONNECTED
        log.info("rnsh session connected to %s", self._destination_hash_hex[:16])

    def _on_link_closed(self, link) -> None:
        with self._lock:
            if self._state == RnshSession.STATE_FAILED:
                return
            if self._state in (RnshSession.STATE_CONNECTING, RnshSession.STATE_CONNECTED):
                # Neither a clean exit (CommandExited, handled in
                # _on_message, already moved us to CLOSED before this
                # callback could fire) nor a user-initiated disconnect()
                # (which also already sets CLOSED itself before tearing
                # the link down) got here first — so this is a real,
                # unsolicited close. Record an honest reason rather than
                # silently looking identical to "never connected yet" or
                # "cleanly disconnected."
                self._error = "Connection lost (link closed unexpectedly)"
            self._state = RnshSession.STATE_CLOSED

    def _fail(self, reason: str) -> None:
        log.warning("rnsh session to %s failed: %s", self._destination_hash_hex[:16], reason)
        with self._lock:
            self._state = RnshSession.STATE_FAILED
            self._error = reason
        self._version_event.set()  # unblock a waiting _connect(), if any

    def _on_message(self, message) -> bool:
        msg = self._msg
        if msg is None:
            return True
        if isinstance(message, msg["VersionInfo"]):
            self._version_ok = True
            self._version_event.set()
        elif isinstance(message, msg["Error"]):
            self._fail(message.msg or "Remote error")
            if message.fatal and self._link is not None:
                try:
                    self._link.teardown()
                except Exception:
                    pass
        elif isinstance(message, msg["StreamData"]):
            if message.data:
                with self._lock:
                    self._output_buffer.extend(message.data)
        elif isinstance(message, msg["CommandExited"]):
            with self._lock:
                self._exit_code = message.return_code if message.return_code is not None else 0
                self._state = RnshSession.STATE_CLOSED
        return True

    def status(self) -> dict:
        with self._lock:
            return {"state": self._state, "error": self._error, "exit_code": self._exit_code}

    def read_output(self) -> bytes:
        """Drains and returns any output bytes (stdout+stderr, in the
        order received — a real interactive terminal session already
        interleaves both onto one visual stream, so this doesn't try to
        keep them separate) buffered since the last call."""
        with self._lock:
            data = bytes(self._output_buffer)
            self._output_buffer.clear()
        return data

    def send_input(self, data: bytes) -> None:
        if self._channel is None or self._state != RnshSession.STATE_CONNECTED:
            return
        try:
            self._channel.send(self._msg["StreamData"](self._msg["StreamData"].STREAM_ID_STDIN, data, False, False))
        except Exception:
            log.exception("rnsh send_input failed")

    def resize(self, rows: int, cols: int) -> None:
        if self._channel is None or self._state != RnshSession.STATE_CONNECTED:
            return
        try:
            self._channel.send(self._msg["WindowSize"](rows, cols, None, None))
        except Exception:
            log.exception("rnsh resize failed")

    def disconnect(self) -> None:
        with self._lock:
            if self._state not in (RnshSession.STATE_FAILED,):
                self._state = RnshSession.STATE_CLOSED
        if self._link is not None:
            try:
                self._link.teardown()
            except Exception:
                pass


class RnshManager:
    """Holds at most one `RnshSession` per logged-in account
    (`user_sub`) — the web-specific counterpart to the Android app's
    single module-level `_rnsh_session` global. A second `connect()`
    call for the same account tears down and replaces that account's
    prior session (single-session-at-a-time *per account*, matching
    rnsh's own real client, which never juggles two shells for the same
    user); a different account's session is entirely unaffected."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict = {}

    def connect(self, user_sub: str, identity, destination_hash_hex: str) -> RnshSession:
        with self._lock:
            prior = self._sessions.get(user_sub)
        if prior is not None:
            try:
                prior.disconnect()
            except Exception:
                pass
        session = RnshSession(identity=identity, destination_hash_hex=destination_hash_hex)
        with self._lock:
            self._sessions[user_sub] = session
        session.start()
        return session

    def status(self, user_sub: str) -> dict:
        session = self._sessions.get(user_sub)
        if session is None:
            return {"state": "idle", "error": None, "exit_code": None}
        return session.status()

    def read_output(self, user_sub: str) -> bytes:
        session = self._sessions.get(user_sub)
        if session is None:
            return b""
        return session.read_output()

    def send_input(self, user_sub: str, data: bytes) -> None:
        session = self._sessions.get(user_sub)
        if session is not None:
            session.send_input(data)

    def resize(self, user_sub: str, rows: int, cols: int) -> None:
        session = self._sessions.get(user_sub)
        if session is not None:
            session.resize(rows, cols)

    def disconnect(self, user_sub: str) -> None:
        session = self._sessions.get(user_sub)
        if session is not None:
            session.disconnect()

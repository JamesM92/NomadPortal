"""Tests for rnsh_client.py: the rnsh wire-protocol message classes
(real pack/unpack round-trips — RNS.MessageBase's pack/unpack never
touch the network, so these are safe to exercise directly) plus
RnshSession's own state-machine transitions in isolation, and
RnshManager's per-account session isolation.

Never drives a real _connect() (that opens a real RNS.Link over the
network) — RnshManager's tests stub RnshSession itself, same
"stub the class, not the network" approach test_refresh_router_display_name.py
already established for LXMF.LXMRouter.
"""

import pytest

from nomadnet_web.rnsh_client import (
    RnshManager,
    RnshSession,
    _make_msgtype,
    _register_message_types,
)


# ---------------------------------------------------------------------------
# Wire protocol: real pack/unpack round-trips
# ---------------------------------------------------------------------------

def msg():
    return _register_message_types()


def test_msgtype_uses_the_shared_rnsh_magic_byte():
    # 0xac is rnsh's own real magic byte (protocol.py) -- every message
    # type here must carry it or a real rnsh listener won't recognise
    # these as its own protocol at all.
    assert _make_msgtype(4) == 0xac04


def test_window_size_message_round_trips():
    original = msg()["WindowSize"](rows=24, cols=80, hpix=None, vpix=None)
    restored = msg()["WindowSize"]()
    restored.unpack(original.pack())
    assert (restored.rows, restored.cols) == (24, 80)


def test_execute_command_message_round_trips_interactive_shell_shape():
    # cmdline=None is rnsh's own "interactive shell, no explicit
    # command" default -- the only shape this client ever sends
    # (see RnshSession._connect()).
    original = msg()["ExecuteCommand"](
        cmdline=None, pipe_stdin=False, pipe_stdout=False, pipe_stderr=False,
        tcflags=None, term="xterm-256color", rows=24, cols=80,
    )
    restored = msg()["ExecuteCommand"]()
    restored.unpack(original.pack())
    assert restored.cmdline is None
    assert restored.term == "xterm-256color"
    assert (restored.rows, restored.cols) == (24, 80)


def test_version_info_message_carries_this_apps_own_sw_version():
    original = msg()["VersionInfo"]()
    restored = msg()["VersionInfo"]()
    restored.unpack(original.pack())
    assert restored.sw_version == "nomadportal-web"
    assert restored.protocol_version == 1


def test_error_message_round_trips_fatal_flag():
    original = msg()["Error"](msg="boom", fatal=True, data=None)
    restored = msg()["Error"]()
    restored.unpack(original.pack())
    assert restored.msg == "boom"
    assert restored.fatal is True


def test_command_exited_message_round_trips_return_code():
    original = msg()["CommandExited"](return_code=0)
    restored = msg()["CommandExited"]()
    restored.unpack(original.pack())
    assert restored.return_code == 0


def test_stream_data_message_has_its_own_distinct_msgtype_from_rns_base():
    from RNS.Buffer import StreamDataMessage as RNSStreamDataMessage

    assert msg()["StreamData"].MSGTYPE != RNSStreamDataMessage.MSGTYPE
    assert msg()["StreamData"].STREAM_ID_STDIN == 0
    assert msg()["StreamData"].STREAM_ID_STDOUT == 1


# ---------------------------------------------------------------------------
# RnshSession state machine, exercised without any real network I/O
# ---------------------------------------------------------------------------

def _new_session():
    return RnshSession(identity=object(), destination_hash_hex="ab" * 16)


def test_fresh_session_starts_in_connecting_state():
    session = _new_session()
    status = session.status()
    assert status["state"] == RnshSession.STATE_CONNECTING
    assert status["error"] is None


def test_fail_moves_to_failed_with_the_given_reason():
    session = _new_session()
    session._fail("no path")
    status = session.status()
    assert status["state"] == RnshSession.STATE_FAILED
    assert status["error"] == "no path"


def test_on_message_stream_data_buffers_output_for_read_output():
    session = _new_session()
    session._msg = _register_message_types()
    stream_msg = session._msg["StreamData"](
        session._msg["StreamData"].STREAM_ID_STDOUT, b"hello\n", False, False,
    )

    session._on_message(stream_msg)

    assert session.read_output() == b"hello\n"
    # Drained -- a second read before any new data arrives is empty.
    assert session.read_output() == b""


def test_on_message_command_exited_closes_the_session_with_exit_code():
    session = _new_session()
    session._msg = _register_message_types()
    exited = session._msg["CommandExited"](return_code=1)

    session._on_message(exited)

    status = session.status()
    assert status["state"] == RnshSession.STATE_CLOSED
    assert status["exit_code"] == 1


def test_on_message_version_info_unblocks_the_waiting_connect():
    session = _new_session()
    session._msg = _register_message_types()
    assert not session._version_event.is_set()

    session._on_message(session._msg["VersionInfo"]())

    assert session._version_ok is True
    assert session._version_event.is_set()


def test_on_message_error_fails_the_session_with_the_remote_reason():
    session = _new_session()
    session._msg = _register_message_types()

    session._on_message(session._msg["Error"](msg="listener rejected us", fatal=False, data=None))

    status = session.status()
    assert status["state"] == RnshSession.STATE_FAILED
    assert status["error"] == "listener rejected us"


def test_send_input_before_connected_is_a_silent_noop():
    session = _new_session()
    session.send_input(b"ls\n")  # no channel yet -- must not raise


def test_disconnect_before_any_link_exists_is_safe():
    session = _new_session()
    session.disconnect()
    assert session.status()["state"] == RnshSession.STATE_CLOSED


# ---------------------------------------------------------------------------
# RnshManager: per-account session isolation
# ---------------------------------------------------------------------------

class _FakeSession:
    """Stands in for a real RnshSession -- records what it was asked to
    do instead of touching RNS/the network at all."""

    def __init__(self, identity, destination_hash_hex):
        self.identity = identity
        self.destination_hash_hex = destination_hash_hex
        self.started = False
        self.disconnected = False
        self.sent = []
        self._output = b""

    def start(self):
        self.started = True

    def status(self):
        return {"state": "connected", "error": None, "exit_code": None}

    def read_output(self):
        out, self._output = self._output, b""
        return out

    def send_input(self, data):
        self.sent.append(data)

    def resize(self, rows, cols):
        pass

    def disconnect(self):
        self.disconnected = True


@pytest.fixture
def _fake_session_class(monkeypatch):
    # Deliberately NOT autouse -- these tests are the only ones in this
    # module that should get a fake RnshSession; everything above
    # exercises the real class directly (safe: none of it calls
    # start()/_connect(), which is the only part that touches the
    # network).
    monkeypatch.setattr("nomadnet_web.rnsh_client.RnshSession", _FakeSession)


def test_status_for_an_account_with_no_session_is_idle():
    mgr = RnshManager()
    assert mgr.status("nobody") == {"state": "idle", "error": None, "exit_code": None}


def test_connect_creates_and_starts_a_session_for_that_account(_fake_session_class):
    mgr = RnshManager()
    session = mgr.connect("u1", identity=object(), destination_hash_hex="aa" * 16)

    assert session.started is True
    assert mgr.status("u1")["state"] == "connected"


def test_two_accounts_get_independent_sessions(_fake_session_class):
    mgr = RnshManager()
    mgr.connect("u1", identity=object(), destination_hash_hex="aa" * 16)
    mgr.connect("u2", identity=object(), destination_hash_hex="bb" * 16)

    mgr.send_input("u1", b"only for u1\n")

    assert mgr._sessions["u1"].sent == [b"only for u1\n"]
    assert mgr._sessions["u2"].sent == []


def test_a_second_connect_for_the_same_account_disconnects_the_prior_session(_fake_session_class):
    mgr = RnshManager()
    mgr.connect("u1", identity=object(), destination_hash_hex="aa" * 16)
    first = mgr._sessions["u1"]

    mgr.connect("u1", identity=object(), destination_hash_hex="bb" * 16)

    assert first.disconnected is True
    assert mgr._sessions["u1"] is not first


def test_disconnecting_one_account_does_not_touch_another(_fake_session_class):
    mgr = RnshManager()
    mgr.connect("u1", identity=object(), destination_hash_hex="aa" * 16)
    mgr.connect("u2", identity=object(), destination_hash_hex="bb" * 16)

    mgr.disconnect("u1")

    assert mgr._sessions["u1"].disconnected is True
    assert mgr._sessions["u2"].disconnected is False

"""Tests for CallManager's real-wire-protocol-matching state machine.

Ported from the NomadPortal-Android sister project's own
test_call_manager.py — CallManager's own logic here is a near-verbatim
port of that project's call_manager.py (see this codebase's own
call_manager.py doc comment), so its test suite ports the same way.

CallManager takes its RNS module (and msgpack module) as injected
dependencies rather than importing them at module scope specifically so
this suite can drive the whole signalling flow — outbound call, inbound
call, busy, reject, hangup, timeout — against a lightweight fake that
mirrors just the RNS surface CallManager actually calls, without needing
real RNS or real networking. The fake's shapes (Destination/Link/Packet
constructor signatures, ACTIVE status, identify()/set_*_callback()
methods, and the exact Signalling wire values) mirror real RNS/LXST one-
for-one — verified (on the Android side) directly against a real local
clone of markqvist/LXST's source, not guessed.

The fake msgpack below is NOT wire-compatible with real
RNS.vendor.umsgpack — it doesn't need to be, since these tests only
exercise CallManager's own pack-then-unpack round trip through one
consistent fake, never real network bytes.
"""

import pytest

from nomadnet_web.call_manager import CallManager, CallStatus, Signalling


# ---------------------------------------------------------------------
# Fake RNS surface — mirrors real RNS's own API shapes one-for-one for
# exactly the calls CallManager makes.
# ---------------------------------------------------------------------

class FakeMsgpack:
    """packb/unpackb round-trip via plain Python object identity — good
    enough since both sides of every test go through this same fake."""

    @staticmethod
    def packb(obj):
        return obj

    @staticmethod
    def unpackb(obj):
        return obj


class FakeIdentity:
    def __init__(self, hash_hex):
        self.hash = bytes.fromhex(hash_hex)


class FakeDestination:
    IN = "IN"
    OUT = "OUT"
    SINGLE = "SINGLE"
    PROVE_NONE = "PROVE_NONE"

    def __init__(self, identity, direction, dest_type, *aspects):
        self.identity = identity
        self.direction = direction
        self.dest_type = dest_type
        self.aspects = aspects
        # Deterministic fake hash: identity hash + aspects, distinct per
        # aspect combination (mirrors real RNS: destination hash depends
        # on both identity and aspect).
        self.hash = identity.hash + "/".join(aspects).encode()
        self._link_established_callback = None

    def set_proof_strategy(self, strategy):
        self.proof_strategy = strategy

    def set_link_established_callback(self, cb):
        self._link_established_callback = cb

    def announce(self):
        self.announced = True


class FakeLink:
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"

    def __init__(self, destination, established_callback=None, closed_callback=None):
        self.destination = destination
        self.status = FakeLink.ACTIVE
        self._established_cb = established_callback
        self._closed_cb = closed_callback
        self._packet_cb = None
        self._remote_identified_cb = None
        self.sent_signals = []  # list of raw signal payload dicts sent over this link
        self.sent_audio_frames = []  # list of raw frame bytes sent over this link
        self.identified_as = None
        self.link_id = id(self)

    def set_packet_callback(self, cb):
        self._packet_cb = cb

    def set_link_closed_callback(self, cb):
        self._closed_cb = cb

    def set_remote_identified_callback(self, cb):
        self._remote_identified_cb = cb

    def identify(self, identity):
        self.identified_as = identity

    def teardown(self):
        if self.status != FakeLink.CLOSED:
            self.status = FakeLink.CLOSED
            if callable(self._closed_cb):
                self._closed_cb(self)

    # Test helpers, not real RNS API -----------------------------------

    def fire_established(self):
        if callable(self._established_cb):
            self._established_cb(self)

    def fire_remote_identified(self, identity):
        if callable(self._remote_identified_cb):
            self._remote_identified_cb(self, identity)

    def receive_signal(self, signal):
        """Simulates the *other* end sending a signal to us over this
        link — delivers it through this link's own registered packet
        callback, exactly as real RNS would."""
        if callable(self._packet_cb):
            fake_packet = FakePacket(self, {0x00: [signal]})
            self._packet_cb(fake_packet.data, fake_packet)

    def receive_audio_frame(self, frame: bytes):
        """Same idea as receive_signal, but for a 0x01 audio-frame
        packet — mirrors real CallManager._packet_received's dict
        shape."""
        if callable(self._packet_cb):
            fake_packet = FakePacket(self, {0x01: frame})
            self._packet_cb(fake_packet.data, fake_packet)


class FakePacket:
    def __init__(self, target, payload_obj, create_receipt=True):
        self.target = target
        self.data = payload_obj
        self.create_receipt = create_receipt
        self.link = target if isinstance(target, FakeLink) else None
        self.sent = False

    def send(self):
        self.sent = True
        if isinstance(self.target, FakeLink):
            if isinstance(self.data, dict) and 0x00 in self.data:
                self.target.sent_signals.extend(self.data[0x00])
            if isinstance(self.data, dict) and 0x01 in self.data:
                self.target.sent_audio_frames.append(self.data[0x01])
        return True


class FakeTransport:
    ACTIVE = "ACTIVE"
    _paths: set = set()

    @classmethod
    def has_path(cls, dest_hash):
        return dest_hash in cls._paths

    @classmethod
    def request_path(cls, dest_hash):
        pass

    @classmethod
    def reset(cls):
        cls._paths = set()

    @classmethod
    def add_path(cls, dest_hash):
        cls._paths.add(dest_hash)


class FakeRNSModule:
    Destination = FakeDestination
    Link = FakeLink
    Packet = FakePacket
    Transport = FakeTransport

    class Identity:
        _by_destination_hash: dict = {}
        _by_identity_hash: dict = {}

        @classmethod
        def recall(cls, target_hash, from_identity_hash=False):
            if from_identity_hash:
                return cls._by_identity_hash.get(target_hash)
            return cls._by_destination_hash.get(target_hash)

        @classmethod
        def reset(cls):
            cls._by_destination_hash = {}
            cls._by_identity_hash = {}

        @classmethod
        def register(cls, identity, destination_hash=None):
            cls._by_identity_hash[identity.hash] = identity
            if destination_hash is not None:
                cls._by_destination_hash[destination_hash] = identity

    @staticmethod
    def prettyhexrep(h):
        return h.hex()


@pytest.fixture(autouse=True)
def reset_fake_state():
    FakeTransport.reset()
    FakeRNSModule.Identity.reset()
    yield


@pytest.fixture
def manager():
    m = CallManager()
    m._rns = FakeRNSModule
    m._msgpack = FakeMsgpack
    m._identity = FakeIdentity("11" * 16)
    # Tiny by default, not the real 15s default — resolve_identity()'s
    # own path-request fallback means even a plain "unresolvable
    # address" test now exercises a real wait loop; nothing in this
    # suite is testing the *timeout duration* itself, so there's no
    # reason for any of it to run at real-world speed.
    m.path_wait_timeout_s = 0.05
    # _calls_enabled defaults to False (see CallManager's own doc
    # comment — a deliberate opt-in departure). Almost everything in
    # this suite is exercising *other* behavior (hangup, history, audio
    # frames, state notifications) and would otherwise have every
    # incoming call rejected before it even got going — explicitly
    # enabled here so the fixture itself, not every individual test,
    # carries that assumption. The master toggle's own real default is
    # covered separately, against a fresh unmodified CallManager(), by
    # TestCallsEnabledMasterToggle.test_disabled_by_default below.
    m.set_calls_enabled(True)
    return m


REMOTE_HASH = "22" * 16


def make_remote_identity():
    return FakeIdentity(REMOTE_HASH)


class TestResolveIdentity:
    def test_resolves_via_destination_hash(self, manager):
        remote = make_remote_identity()
        dest_hash = b"some-dest-hash-1"
        FakeRNSModule.Identity.register(remote, destination_hash=dest_hash)
        found = manager.resolve_identity(dest_hash.hex())
        assert found is remote

    def test_resolves_via_identity_hash_when_destination_lookup_fails(self, manager):
        remote = make_remote_identity()
        FakeRNSModule.Identity.register(remote)  # no destination_hash registered
        found = manager.resolve_identity(REMOTE_HASH)
        assert found is remote

    def test_returns_none_for_completely_unknown_address(self, manager):
        assert manager.resolve_identity("ff" * 16) is None

    def test_returns_none_for_invalid_hex(self, manager):
        assert manager.resolve_identity("not-hex-at-all") is None


class TestNotYetStarted:
    def test_place_call_fails_cleanly_before_start(self):
        # A fresh CallManager whose start() has never run (self._rns is
        # None) — a real reachable state (RNS init can take a while),
        # must fail cleanly, not AttributeError.
        m = CallManager()
        success, message = m.place_call("aa" * 16)
        assert success is False
        assert "not ready" in message.lower()


class TestOutboundCallFlow:
    def _place_call_with_path(self, manager):
        remote = make_remote_identity()
        FakeRNSModule.Identity.register(remote)
        dest = FakeDestination(remote, FakeDestination.OUT, FakeDestination.SINGLE, "lxst", "telephony")
        FakeTransport.add_path(dest.hash)
        success, message = manager.place_call(REMOTE_HASH)
        assert success is True
        return remote

    def test_fails_for_unresolvable_address(self, manager):
        success, message = manager.place_call("ff" * 16)
        assert success is False
        assert manager.status == CallStatus.IDLE

    def test_fails_when_no_path_found(self, manager):
        remote = make_remote_identity()
        FakeRNSModule.Identity.register(remote)
        manager.path_wait_timeout_s = 0.05  # keep this test fast
        success, message = manager.place_call(REMOTE_HASH)
        assert success is False
        assert "path" in message.lower()

    def test_status_calling_immediately_after_place_call(self, manager):
        self._place_call_with_path(manager)
        assert manager.status in (CallStatus.CALLING, CallStatus.RINGING_OUTGOING)
        assert manager.is_incoming is False
        assert manager.remote_identity_hash == REMOTE_HASH

    def test_identifies_once_remote_signals_available(self, manager):
        self._place_call_with_path(manager)
        manager.link.fire_established()
        manager.link.receive_signal(Signalling.STATUS_AVAILABLE)
        assert manager.link.identified_as is manager._identity

    def test_advances_through_ringing_connecting_established(self, manager):
        self._place_call_with_path(manager)
        manager.link.fire_established()
        manager.link.receive_signal(Signalling.STATUS_AVAILABLE)
        manager.link.receive_signal(Signalling.STATUS_RINGING)
        assert manager.status == CallStatus.RINGING_OUTGOING
        manager.link.receive_signal(Signalling.STATUS_CONNECTING)
        assert manager.status == CallStatus.CONNECTING
        manager.link.receive_signal(Signalling.STATUS_ESTABLISHED)
        assert manager.status == CallStatus.ESTABLISHED
        assert manager.established_at is not None

    def test_busy_signal_ends_call_as_busy(self, manager):
        self._place_call_with_path(manager)
        manager.link.fire_established()
        manager.link.receive_signal(Signalling.STATUS_BUSY)
        assert manager.status == CallStatus.BUSY

    def test_rejected_signal_ends_call_as_rejected(self, manager):
        self._place_call_with_path(manager)
        manager.link.fire_established()
        manager.link.receive_signal(Signalling.STATUS_AVAILABLE)
        manager.link.receive_signal(Signalling.STATUS_RINGING)
        manager.link.receive_signal(Signalling.STATUS_REJECTED)
        assert manager.status == CallStatus.REJECTED

    def test_second_call_while_one_active_is_refused_locally(self, manager):
        self._place_call_with_path(manager)
        success, message = manager.place_call("33" * 16)
        assert success is False
        assert "already" in message.lower()


class TestInboundCallFlow:
    def _incoming_link(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        return link

    def test_sends_available_on_incoming_link(self, manager):
        link = self._incoming_link(manager)
        assert Signalling.STATUS_AVAILABLE in link.sent_signals

    def test_busy_when_already_on_a_call(self, manager):
        first_link = self._incoming_link(manager)
        remote = make_remote_identity()
        first_link.fire_remote_identified(remote)
        assert manager.status == CallStatus.RINGING_INCOMING

        second_link = FakeLink(FakeDestination(FakeIdentity("77" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony"))
        manager._incoming_link_established(second_link)
        assert Signalling.STATUS_BUSY in second_link.sent_signals
        assert second_link.status == FakeLink.CLOSED

    def test_ringing_after_caller_identified(self, manager):
        link = self._incoming_link(manager)
        remote = make_remote_identity()
        link.fire_remote_identified(remote)
        assert manager.status == CallStatus.RINGING_INCOMING
        assert manager.is_incoming is True
        assert manager.remote_identity_hash == REMOTE_HASH
        assert Signalling.STATUS_RINGING in link.sent_signals

    def test_answer_sends_connecting_then_established(self, manager):
        link = self._incoming_link(manager)
        link.fire_remote_identified(make_remote_identity())
        success, message = manager.answer_call()
        assert success is True
        assert manager.status == CallStatus.ESTABLISHED
        assert Signalling.STATUS_CONNECTING in link.sent_signals
        assert Signalling.STATUS_ESTABLISHED in link.sent_signals

    def test_answer_fails_when_nothing_ringing(self, manager):
        success, message = manager.answer_call()
        assert success is False


class TestCallsEnabledMasterToggle:
    """Real allowVoiceCalls-equivalent master toggle. Independent of,
    and enforced ahead of, contacts-only."""

    def _incoming_link(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        return link

    def test_disabled_by_default(self):
        # Deliberately not the shared `manager` fixture — that fixture
        # force-enables calls for every *other* test's convenience,
        # which would make this assertion meaningless. A fresh,
        # unmodified CallManager() is the only way to actually observe
        # the class's own real default.
        assert CallManager().get_calls_enabled() is False

    def test_disabled_call_rejected_before_identification(self, manager):
        manager.set_calls_enabled(False)
        link = self._incoming_link(manager)
        assert Signalling.STATUS_BUSY in link.sent_signals
        assert link.status == FakeLink.CLOSED
        # Never even reached AVAILABLE/identification.
        assert Signalling.STATUS_AVAILABLE not in link.sent_signals
        assert manager.status == CallStatus.IDLE

    def test_re_enabling_allows_calls_again(self, manager):
        manager.set_calls_enabled(False)
        manager.set_calls_enabled(True)
        link = self._incoming_link(manager)
        assert Signalling.STATUS_AVAILABLE in link.sent_signals

    def test_disabling_hangs_up_a_call_already_in_progress(self, manager):
        link = self._incoming_link(manager)
        link.fire_remote_identified(make_remote_identity())
        assert manager.status == CallStatus.RINGING_INCOMING

        manager.set_calls_enabled(False)
        # Same real hang_up() path/outcome as TestHangUp's own
        # test_hangup_incoming_ringing_unanswered_sends_rejected — a
        # ringing-unanswered call ends as REJECTED, not a bare IDLE.
        assert manager.status == CallStatus.ENDED
        assert Signalling.STATUS_REJECTED in link.sent_signals
        assert link.status == FakeLink.CLOSED

    def test_disabling_with_no_active_call_is_a_safe_no_op(self, manager):
        manager.set_calls_enabled(False)  # must not raise
        assert manager.status == CallStatus.IDLE


class TestHangUp:
    def test_hangup_incoming_ringing_unanswered_sends_rejected(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())

        success, message = manager.hang_up()
        assert success is True
        assert Signalling.STATUS_REJECTED in link.sent_signals
        assert manager.status == CallStatus.ENDED
        assert link.status == FakeLink.CLOSED

    def test_hangup_established_call_tears_down_link(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())
        manager.answer_call()

        success, message = manager.hang_up()
        assert success is True
        assert manager.status == CallStatus.ENDED
        assert link.status == FakeLink.CLOSED

    def test_hangup_with_no_active_call_fails(self, manager):
        success, message = manager.hang_up()
        assert success is False

    def test_remote_hangup_ends_call_locally(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())
        manager.answer_call()
        assert manager.status == CallStatus.ESTABLISHED

        link.teardown()  # simulates the remote end closing the link
        assert manager.status == CallStatus.ENDED


class TestCallHistory:
    """_end_call() is the one choke point every terminal transition
    funnels through (hangup, remote hangup, busy, rejected, timeout) —
    these tests cover that every one of those paths actually gets
    recorded."""

    def test_starts_empty(self, manager):
        assert manager.get_history() == []

    def test_hangup_records_an_entry(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())
        manager.hang_up()

        history = manager.get_history()
        assert len(history) == 1
        entry = history[0]
        assert entry["is_incoming"] is True
        assert entry["remote_identity_hash"] == REMOTE_HASH
        assert entry["status"] == CallStatus.ENDED
        assert entry["started_at"] is not None
        assert entry["ended_at"] is not None

    def test_remote_hangup_records_an_entry(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())
        manager.answer_call()

        link.teardown()  # simulates the remote end closing the link
        history = manager.get_history()
        assert len(history) == 1
        assert history[0]["status"] == CallStatus.ENDED
        assert history[0]["reason"] == "Remote hung up"
        assert history[0]["established_at"] is not None

    def test_busy_records_an_entry(self, manager):
        self_call = _place_outbound(manager)
        self_call.fire_established()
        self_call.receive_signal(Signalling.STATUS_BUSY)

        history = manager.get_history()
        assert len(history) == 1
        assert history[0]["status"] == CallStatus.BUSY
        assert history[0]["is_incoming"] is False

    def test_rejected_records_an_entry(self, manager):
        self_call = _place_outbound(manager)
        self_call.fire_established()
        self_call.receive_signal(Signalling.STATUS_AVAILABLE)
        self_call.receive_signal(Signalling.STATUS_RINGING)
        self_call.receive_signal(Signalling.STATUS_REJECTED)

        history = manager.get_history()
        assert len(history) == 1
        assert history[0]["status"] == CallStatus.REJECTED

    def test_most_recent_call_is_first(self, manager):
        for i in range(3):
            remote_dest = FakeDestination(FakeIdentity(f"{i:02x}" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
            link = FakeLink(remote_dest)
            manager._incoming_link_established(link)
            link.fire_remote_identified(FakeIdentity(f"{i:02x}" * 16))
            manager.hang_up()
            # Real behavior, not test housekeeping: status stays ENDED
            # (not IDLE) until reset_after_end() runs.
            manager.reset_after_end()

        history = manager.get_history()
        assert len(history) == 3
        assert history[0]["remote_identity_hash"] == "02" * 16
        assert history[2]["remote_identity_hash"] == "00" * 16

    def test_capped_at_history_max(self, manager):
        from nomadnet_web.call_manager import HISTORY_MAX
        for i in range(HISTORY_MAX + 10):
            remote_dest = FakeDestination(FakeIdentity("aa" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
            link = FakeLink(remote_dest)
            manager._incoming_link_established(link)
            link.fire_remote_identified(FakeIdentity("aa" * 16))
            manager.hang_up()
            manager.reset_after_end()

        assert len(manager.get_history()) == HISTORY_MAX


def _place_outbound(manager):
    remote = make_remote_identity()
    FakeRNSModule.Identity.register(remote)
    dest = FakeDestination(remote, FakeDestination.OUT, FakeDestination.SINGLE, "lxst", "telephony")
    FakeTransport.add_path(dest.hash)
    manager.place_call(REMOTE_HASH)
    return manager.link


class TestResetAfterEnd:
    def test_reset_clears_terminal_state_to_idle(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())
        manager.hang_up()
        assert manager.status == CallStatus.ENDED

        manager.reset_after_end()
        assert manager.status == CallStatus.IDLE
        assert manager.remote_identity_hash is None

    def test_reset_is_a_no_op_when_not_in_a_terminal_state(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())
        assert manager.status == CallStatus.RINGING_INCOMING

        manager.reset_after_end()
        assert manager.status == CallStatus.RINGING_INCOMING


class TestNotActuallyBusyAfterATerminalState:
    """Terminal status (ENDED/BUSY/REJECTED/FAILED) means the previous
    call is over, not that the line is still busy. None of these tests
    call reset_after_end() themselves — the whole point is that a new
    call must get through without it."""

    def test_new_incoming_call_succeeds_right_after_a_hangup(self, manager):
        first_link = FakeLink(FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony"))
        manager._incoming_link_established(first_link)
        first_link.fire_remote_identified(make_remote_identity())
        manager.hang_up()
        assert manager.status == CallStatus.ENDED  # terminal, not yet reset

        second_link = FakeLink(FakeDestination(FakeIdentity("77" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony"))
        manager._incoming_link_established(second_link)
        assert Signalling.STATUS_BUSY not in second_link.sent_signals
        assert Signalling.STATUS_AVAILABLE in second_link.sent_signals

        second_link.fire_remote_identified(FakeIdentity("77" * 16))
        assert manager.status == CallStatus.RINGING_INCOMING
        assert manager.remote_identity_hash == "77" * 16

    def test_new_outbound_call_succeeds_right_after_a_hangup(self, manager):
        first_link = FakeLink(FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony"))
        manager._incoming_link_established(first_link)
        first_link.fire_remote_identified(make_remote_identity())
        manager.hang_up()
        assert manager.status == CallStatus.ENDED  # terminal, not yet reset

        remote = make_remote_identity()
        FakeRNSModule.Identity.register(remote)
        dest = FakeDestination(remote, FakeDestination.OUT, FakeDestination.SINGLE, "lxst", "telephony")
        FakeTransport.add_path(dest.hash)
        success, message = manager.place_call(REMOTE_HASH)
        assert success is True

    def test_still_correctly_busy_during_a_genuinely_active_call(self, manager):
        # Sanity check the fix didn't over-correct: a real non-terminal
        # active call must still refuse a second one.
        first_link = FakeLink(FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony"))
        manager._incoming_link_established(first_link)
        first_link.fire_remote_identified(make_remote_identity())
        assert manager.status == CallStatus.RINGING_INCOMING

        second_link = FakeLink(FakeDestination(FakeIdentity("77" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony"))
        manager._incoming_link_established(second_link)
        assert Signalling.STATUS_BUSY in second_link.sent_signals


class TestAudioFrames:
    """Phase 1b: CallManager relays already-encoded audio frames as
    opaque bytes over the active call's Link. These tests never touch a
    real codec — only that send/pop/receive/drop/drain behave correctly
    around them."""

    def _established_incoming_call(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())
        manager.answer_call()
        assert manager.status == CallStatus.ESTABLISHED
        return link

    def test_send_requires_an_established_call(self, manager):
        assert manager.send_audio_frame(b"\x01abc") is False

    def test_send_fails_while_only_ringing(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())
        assert manager.status == CallStatus.RINGING_INCOMING
        assert manager.send_audio_frame(b"\x01abc") is False
        assert link.sent_audio_frames == []

    def test_send_succeeds_once_established(self, manager):
        link = self._established_incoming_call(manager)
        assert manager.send_audio_frame(b"\x01abc") is True
        assert link.sent_audio_frames == [b"\x01abc"]

    def test_send_converts_a_non_bytes_frame_argument(self, manager):
        # Same defensive bytes(frame) conversion the audio-frame relay
        # needs on Android (a Kotlin ByteArray proxy isn't native
        # Python bytes there) — kept here for parity even though a pure
        # Python caller normally already hands over real bytes; a plain
        # list of ints exercises the same conversion path.
        link = self._established_incoming_call(manager)
        assert manager.send_audio_frame([0x01, 0x02, 0x03]) is True
        assert link.sent_audio_frames == [b"\x01\x02\x03"]

    def test_pop_returns_none_when_empty(self, manager):
        assert manager.pop_audio_frame(timeout_s=0.05) is None

    def test_received_frame_round_trips_through_pop(self, manager):
        link = self._established_incoming_call(manager)
        link.receive_audio_frame(b"\x01xyz")
        assert manager.pop_audio_frame(timeout_s=0.05) == b"\x01xyz"

    def test_frame_received_outside_established_is_dropped(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())
        assert manager.status == CallStatus.RINGING_INCOMING  # not yet answered
        link.receive_audio_frame(b"\x01too-early")
        assert manager.pop_audio_frame(timeout_s=0.05) is None

    def test_frame_from_a_non_active_link_is_dropped(self, manager):
        # Same "packet.link != self.link" guard _handle_signal already
        # relies on (source resolves to the packet's own .link, per
        # _packet_received) -- wire the manager's own callback onto an
        # unrelated link to simulate a stray/late packet.
        self._established_incoming_call(manager)  # sets manager.link to the real, active one
        stray = FakeLink(FakeDestination(FakeIdentity("55" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony"))
        stray.set_packet_callback(manager._packet_received)
        stray.receive_audio_frame(b"\x01stray")
        assert manager.pop_audio_frame(timeout_s=0.05) is None

    def test_queue_drops_oldest_when_full(self, manager):
        from nomadnet_web.call_manager import AUDIO_JITTER_MAX
        link = self._established_incoming_call(manager)
        for i in range(AUDIO_JITTER_MAX + 3):
            link.receive_audio_frame(bytes([0x01, i]))

        popped = []
        while True:
            frame = manager.pop_audio_frame(timeout_s=0.05)
            if frame is None:
                break
            popped.append(frame)

        assert len(popped) == AUDIO_JITTER_MAX
        # The oldest 3 (i=0,1,2) were dropped -- the newest AUDIO_JITTER_MAX survive.
        assert popped[0] == bytes([0x01, 3])
        assert popped[-1] == bytes([0x01, AUDIO_JITTER_MAX + 2])

    def test_queue_is_drained_on_call_end(self, manager):
        link = self._established_incoming_call(manager)
        link.receive_audio_frame(b"\x01leftover")
        manager.hang_up()

        assert manager.pop_audio_frame(timeout_s=0.05) is None


class TestStatusDict:
    def test_reflects_current_state(self, manager):
        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        manager._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())

        d = manager.status_dict()
        assert d["status"] == CallStatus.RINGING_INCOMING
        assert d["is_incoming"] is True
        assert d["remote_identity_hash"] == REMOTE_HASH


class TestStateChangeNotification:
    def test_on_state_change_called_on_transitions(self):
        calls = []
        m = CallManager(on_state_change=lambda: calls.append(1))
        m._rns = FakeRNSModule
        m._msgpack = FakeMsgpack
        m._identity = FakeIdentity("11" * 16)
        # Constructed directly rather than via the `manager` fixture
        # (this test wants its own dedicated on_state_change callback),
        # so it needs the same explicit calls-enabled opt-in the
        # fixture itself would otherwise provide.
        m.set_calls_enabled(True)

        remote_dest = FakeDestination(FakeIdentity("99" * 16), FakeDestination.IN, FakeDestination.SINGLE, "lxst", "telephony")
        link = FakeLink(remote_dest)
        m._incoming_link_established(link)
        link.fire_remote_identified(make_remote_identity())
        assert len(calls) >= 1

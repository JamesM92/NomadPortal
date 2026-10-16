"""Cancelling a message that has not gone out, retrying one that failed or
was cancelled, and re-sending failed messages when the recipient announces.

The real delivery method runs here against small fakes of RNS and LXMF, so
ordering and races (cancelling while a message is being queued) are checked
for real, not just the state that ends up in the store.
"""

import os
import threading
import time
import types

import pytest

import nomadnet_web.messaging as messaging
from nomadnet_web.attachment_store import AttachmentStore
from nomadnet_web.message_store import MessageStore
from nomadnet_web.messaging import (
    AUTO_RESEND_COOLDOWN_S, AUTO_RESEND_MAX_AGE_S, AUTO_RESEND_MAX_ATTEMPTS,
    MessagingService,
)

ME = "user-1"
OTHER_USER = "user-2"
PEER = "bb" * 16
PEER2 = "cc" * 16


class FakeLXMessage:
    OPPORTUNISTIC = 1
    made = []

    def __init__(self, dest, source, content, title="", fields=None, desired_method=None):
        self.content, self.title, self.fields = content, title, fields
        self.message_id = os.urandom(32)
        self.hash = os.urandom(32)
        self.delivered_cb = self.failed_cb = None
        FakeLXMessage.made.append(self)

    def register_delivery_callback(self, cb):
        self.delivered_cb = cb

    def register_failed_callback(self, cb):
        self.failed_cb = cb


class FakeRouter:
    def __init__(self):
        self.events = []
        self.on_handle = None

    def handle_outbound(self, lxm):
        self.events.append(("handle", lxm.message_id))
        if self.on_handle:
            self.on_handle(lxm)

    def cancel_outbound(self, message_id):
        self.events.append(("cancel", message_id))


class FakeDest:
    OUT, SINGLE = 1, 2

    def __init__(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def fake_rns(monkeypatch):
    FakeLXMessage.made = []
    monkeypatch.setattr("RNS.Identity.recall", staticmethod(lambda h: object()))
    monkeypatch.setattr("RNS.Transport.request_path", staticmethod(lambda h: None))
    monkeypatch.setattr("RNS.Destination", FakeDest)
    monkeypatch.setattr("LXMF.LXMessage", FakeLXMessage)


@pytest.fixture
def env(tmp_path):
    att = AttachmentStore(str(tmp_path))
    store = MessageStore(str(tmp_path), attachment_store=att)
    svc = MessagingService(storage_path=str(tmp_path / "lxmf"), message_store=store,
                           attachment_store=att)
    router = FakeRouter()
    dest = types.SimpleNamespace(hexhash="aa" * 16)
    svc._get_user_router = lambda sub: ({"router": router, "dest": dest, "identity": None}
                                        if sub in (ME,) else None)
    launched = []
    svc._launch_delivery = lambda *a: launched.append(a)       # no threads unless a test wants one
    return svc, store, router, dest, launched, att


def sent_entry(store, msg_id="m1", state="queued", dest=PEER, owner=ME, sent_at=None, **extra):
    entry = {"id": msg_id, "dest": dest, "title": "", "content": "hello", "preview": "hello",
             "state": state, "sent_at": sent_at if sent_at is not None else time.time(), "owner": owner}
    entry.update(extra)
    store.save_sent(entry)
    return entry


def deliver(svc, router, dest, msg_id="m1", **kw):
    """Run the real delivery body synchronously."""
    args = dict(msg_id=msg_id, dest_hash_hex=PEER, dest_hash=bytes.fromhex(PEER), title="",
                content="hello", user_sub=ME, att_meta=[], router=router, source_dest=dest)
    args.update(kw)
    svc._run_delivery(**args)
    return FakeLXMessage.made[-1] if FakeLXMessage.made else None


def state(store, msg_id="m1"):
    return store.get_sent(msg_id)["state"]


class TestCancel:
    def test_cancels_a_queued_message_in_the_router(self, env):
        svc, store, router, dest, *_ = env
        sent_entry(store)
        lxm = deliver(svc, router, dest)
        ok, _ = svc.cancel_send("m1", ME)
        assert ok and ("cancel", lxm.message_id) in router.events
        assert state(store) == "cancelled"

    def test_a_late_failure_does_not_overwrite_cancelled(self, env):
        svc, store, router, dest, *_ = env
        sent_entry(store)
        lxm = deliver(svc, router, dest)
        svc.cancel_send("m1", ME)
        lxm.failed_cb(lxm)
        assert state(store) == "cancelled"

    def test_cancelling_during_queueing_still_stops_the_message(self, env):
        svc, store, router, dest, *_ = env
        sent_entry(store)
        # LXMF cannot see a message that is still being handed to handle_outbound,
        # so the cancel must be repeated once handle_outbound returns.
        router.on_handle = lambda lxm: svc.cancel_send("m1", ME)
        lxm = deliver(svc, router, dest)
        kinds = [e[0] for e in router.events]
        assert kinds[0] == "handle" and kinds[-1] == "cancel"
        assert router.events[-1][1] == lxm.message_id

    def test_cancelling_before_the_message_is_built_means_it_is_never_queued(self, env):
        svc, store, router, dest, *_ = env
        sent_entry(store)
        svc._cancelled.add("m1")
        deliver(svc, router, dest)
        assert [e for e in router.events if e[0] == "handle"] == []
        assert state(store) == "queued"           # cancel_send sets the final state itself

    def test_cancelling_while_waiting_for_the_peers_path(self, env, monkeypatch):
        svc, store, router, dest, *_ = env
        sent_entry(store)
        monkeypatch.setattr(messaging, "PATH_WAIT", 5)
        monkeypatch.setattr("RNS.Identity.recall", staticmethod(lambda h: None))
        t = threading.Thread(target=svc._run_delivery, kwargs=dict(
            msg_id="m1", dest_hash_hex=PEER, dest_hash=bytes.fromhex(PEER), title="",
            content="hello", user_sub=ME, att_meta=[], router=router, source_dest=dest))
        t.start()
        time.sleep(0.4)
        started = time.time()
        svc.cancel_send("m1", ME)
        t.join(timeout=3)
        assert not t.is_alive() and time.time() - started < 2
        assert [e for e in router.events if e[0] == "handle"] == []
        assert state(store) == "cancelled"

    def test_a_delivered_message_cannot_be_cancelled(self, env):
        svc, store, *_ = env
        sent_entry(store, state="delivered")
        ok, why = svc.cancel_send("m1", ME)
        assert not ok and "delivered" in why and state(store) == "delivered"

    def test_a_failed_message_cannot_be_cancelled(self, env):
        svc, store, *_ = env
        sent_entry(store, state="failed")
        assert svc.cancel_send("m1", ME)[0] is False

    def test_someone_elses_message_cannot_be_cancelled(self, env):
        svc, store, *_ = env
        sent_entry(store, owner=OTHER_USER)
        ok, _ = svc.cancel_send("m1", ME)
        assert not ok and state(store) == "queued"

    def test_unknown_message(self, env):
        assert env[0].cancel_send("nope", ME)[0] is False

    def test_a_message_delivered_despite_the_cancel_ends_up_delivered(self, env):
        svc, store, router, dest, *_ = env
        sent_entry(store)
        lxm = deliver(svc, router, dest)
        svc.cancel_send("m1", ME)
        lxm.delivered_cb(lxm)                     # it got through anyway: that is the truth
        assert store.get_sent(lxm.hash.hex())["state"] == "delivered"


class TestRetry:
    def test_a_failed_message_is_sent_again_with_the_same_id_and_content(self, env):
        svc, store, router, dest, launched, _ = env
        sent_entry(store, state="failed", content="the text", title="t")
        ok, _ = svc.retry_send("m1", ME)
        assert ok and state(store) == "queued"
        (msg_id, dest_hex, dest_hash, title, content, sub, att, r, d), = launched
        assert (msg_id, dest_hex, title, content, sub) == ("m1", PEER, "t", "the text", ME)
        assert dest_hash == bytes.fromhex(PEER) and r is router and d is dest

    def test_a_cancelled_message_can_be_retried(self, env):
        svc, store, *_ = env
        sent_entry(store, state="cancelled")
        assert svc.retry_send("m1", ME)[0] is True

    def test_retry_clears_the_cancelled_flag(self, env):
        svc, store, router, dest, launched, _ = env
        sent_entry(store, state="queued")
        svc.cancel_send("m1", ME)
        svc.retry_send("m1", ME)
        deliver(svc, router, dest)
        assert [e for e in router.events if e[0] == "handle"]       # not blocked by the old cancel

    def test_attachments_go_with_the_retry(self, env):
        svc, store, router, dest, launched, att = env
        att.write("m1", 0, "pic.png", b"PNGDATA")
        meta = [{"kind": "image", "idx": 0, "filename": "pic.png", "mime": "image/png", "size": 7}]
        sent_entry(store, state="failed", attachments=meta)
        svc.retry_send("m1", ME)
        assert launched[0][6] == meta

    @pytest.mark.parametrize("st", ["delivered", "queued"])
    def test_only_failed_or_cancelled_messages_can_be_retried(self, env, st):
        svc, store, *_ = env
        sent_entry(store, state=st)
        ok, why = svc.retry_send("m1", ME)
        assert not ok and st in why

    def test_someone_elses_message_cannot_be_retried(self, env):
        svc, store, *_ = env
        sent_entry(store, state="failed", owner=OTHER_USER)
        assert svc.retry_send("m1", ME)[0] is False

    def test_no_delivery_identity_means_no_retry(self, env):
        svc, store, *_ = env
        svc._get_user_router = lambda sub: None
        sent_entry(store, state="failed")
        ok, why = svc.retry_send("m1", ME)
        assert not ok and state(store) == "failed"

    def test_a_manual_retry_does_not_use_up_the_automatic_ones(self, env):
        svc, store, *_ = env
        sent_entry(store, state="failed")
        svc.retry_send("m1", ME)
        assert (store.get_sent("m1").get("resends") or 0) == 0


class TestAutoResend:
    def test_resends_failed_messages_to_the_peer_that_announced(self, env):
        svc, store, router, dest, launched, _ = env
        sent_entry(store, "a", "failed")
        sent_entry(store, "b", "failed")
        sent_entry(store, "other-peer", "failed", dest=PEER2)
        assert svc.resend_failed_for(PEER) == 2
        assert sorted(a[0] for a in launched) == ["a", "b"]
        assert all(state(store, i) == "queued" for i in ("a", "b"))
        assert state(store, "other-peer") == "failed"

    def test_only_failed_messages_are_resent(self, env):
        svc, store, *_ = env
        sent_entry(store, "q", "queued")
        sent_entry(store, "d", "delivered")
        sent_entry(store, "c", "cancelled")
        assert svc.resend_failed_for(PEER) == 0

    def test_old_messages_are_left_alone(self, env):
        svc, store, *_ = env
        sent_entry(store, "old", "failed", sent_at=time.time() - AUTO_RESEND_MAX_AGE_S - 60)
        assert svc.resend_failed_for(PEER) == 0

    def test_each_message_is_resent_at_most_a_few_times(self, env):
        svc, store, router, dest, launched, _ = env
        sent_entry(store, "a", "failed")
        for i in range(AUTO_RESEND_MAX_ATTEMPTS):
            svc._resend_last.clear()
            assert svc.resend_failed_for(PEER) == 1
            store.update_sent("a", "failed")
        svc._resend_last.clear()
        assert svc.resend_failed_for(PEER) == 0
        assert store.get_sent("a")["resends"] == AUTO_RESEND_MAX_ATTEMPTS

    def test_a_peer_that_announces_often_does_not_cause_a_resend_storm(self, env, monkeypatch):
        svc, store, *_ = env
        sent_entry(store, "a", "failed")
        now = [1000.0]
        monkeypatch.setattr(messaging.time, "time", lambda: now[0])
        assert svc.resend_failed_for(PEER) == 1
        store.update_sent("a", "failed")
        now[0] += AUTO_RESEND_COOLDOWN_S - 1
        assert svc.resend_failed_for(PEER) == 0
        now[0] += 2
        assert svc.resend_failed_for(PEER) == 1

    def test_can_be_switched_off(self, env):
        svc, store, *_ = env
        sent_entry(store, "a", "failed")
        svc.set_auto_resend(False)
        assert svc.resend_failed_for(PEER) == 0 and state(store, "a") == "failed"
        assert svc.auto_resend_enabled() is False

    def test_a_message_whose_owner_has_no_identity_stays_failed(self, env):
        svc, store, *_ = env
        sent_entry(store, "a", "failed", owner=OTHER_USER)
        assert svc.resend_failed_for(PEER) == 0 and state(store, "a") == "failed"

    def test_a_bad_hash_is_harmless(self, env):
        assert env[0].resend_failed_for("") == 0

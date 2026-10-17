"""Falling back to a propagation node when direct delivery fails.

A message is first tried directly. If that fails (the recipient is offline)
and the sender has a propagation node, it is handed to the node, which holds
it until the recipient collects it. That is not delivery: the message shows
as "held" (stored on a relay), never "delivered", and nothing is claimed
about whether the recipient has it.

It must never happen for a message the sender cancelled, never loop, and
never when there is no node to use or the admin has turned it off.
"""

import os
import types

import pytest

import LXMF
from nomadnet_web.attachment_store import AttachmentStore
from nomadnet_web.message_store import MessageStore
from nomadnet_web.messaging import MessagingService

PEER = "bb" * 16
NODE = bytes.fromhex("cc" * 16)
ME = "user-1"


class FakeLXMessage:
    OPPORTUNISTIC = LXMF.LXMessage.OPPORTUNISTIC
    PROPAGATED = LXMF.LXMessage.PROPAGATED
    made = []

    def __init__(self, dest, source, content, title="", fields=None, desired_method=None):
        self.method = self.desired_method = desired_method
        self.message_id = os.urandom(32)
        self.hash = os.urandom(32)
        self.delivered_cb = self.failed_cb = None
        FakeLXMessage.made.append(self)

    def register_delivery_callback(self, cb):
        self.delivered_cb = cb

    def register_failed_callback(self, cb):
        self.failed_cb = cb


class FakeRouter:
    def __init__(self, node=NODE):
        self.node = node
        self.handled = []

    def get_outbound_propagation_node(self):
        return self.node

    def handle_outbound(self, lxm):
        self.handled.append(lxm)

    def cancel_outbound(self, message_id):
        pass


class FakeDest:
    OUT, SINGLE = 1, 2

    def __init__(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def fake_rns(monkeypatch):
    FakeLXMessage.made = []
    monkeypatch.setattr("RNS.Identity.recall", staticmethod(lambda h: object()))
    monkeypatch.setattr("RNS.Destination", FakeDest)
    monkeypatch.setattr("LXMF.LXMessage", FakeLXMessage)


@pytest.fixture
def env(tmp_path):
    att = AttachmentStore(str(tmp_path))
    store = MessageStore(str(tmp_path), attachment_store=att)
    svc = MessagingService(storage_path=str(tmp_path / "lxmf"), message_store=store, attachment_store=att)
    router = FakeRouter()
    dest = types.SimpleNamespace(hexhash="aa" * 16)
    launched = []
    svc._launch_delivery = lambda *a: launched.append(a)
    store.save_sent({"id": "m1", "dest": PEER, "title": "", "content": "hi", "preview": "hi",
                     "state": "queued", "sent_at": 1.0, "owner": ME})
    return svc, store, router, dest, launched


def run(svc, router, dest, propagate=False):
    svc._run_delivery("m1", PEER, bytes.fromhex(PEER), "", "hi", ME, [], router, dest, propagate)
    return FakeLXMessage.made[-1]


def state(store):
    return store.get_sent("m1")["state"]


class TestFallback:
    def test_a_first_attempt_is_direct(self, env):
        svc, store, router, dest, launched = env
        lxm = run(svc, router, dest)
        assert lxm.desired_method == LXMF.LXMessage.OPPORTUNISTIC

    def test_a_direct_failure_hands_the_message_to_the_node(self, env):
        svc, store, router, dest, launched = env
        lxm = run(svc, router, dest)
        lxm.failed_cb(lxm)
        assert len(launched) == 1 and launched[0][0] == "m1" and launched[0][9] is True
        assert state(store) == "queued"                    # still going: not failed, not delivered

    def test_the_second_attempt_asks_for_propagated_delivery(self, env):
        svc, store, router, dest, launched = env
        lxm = run(svc, router, dest, propagate=True)
        assert lxm.desired_method == LXMF.LXMessage.PROPAGATED

    def test_a_message_held_by_the_node_is_held_not_delivered(self, env):
        svc, store, router, dest, launched = env
        lxm = run(svc, router, dest, propagate=True)
        lxm.delivered_cb(lxm)
        assert store.get_sent(lxm.hash.hex())["state"] == "held"

    def test_a_directly_delivered_message_is_delivered(self, env):
        svc, store, router, dest, launched = env
        lxm = run(svc, router, dest)
        lxm.delivered_cb(lxm)
        assert store.get_sent(lxm.hash.hex())["state"] == "delivered"

    def test_a_failed_propagated_attempt_is_failed_with_no_further_fallback(self, env):
        svc, store, router, dest, launched = env
        lxm = run(svc, router, dest, propagate=True)
        lxm.failed_cb(lxm)
        assert state(store) == "failed" and launched == []

    def test_no_propagation_node_means_failed(self, env):
        svc, store, router, dest, launched = env
        router.node = None
        lxm = run(svc, router, dest)
        lxm.failed_cb(lxm)
        assert state(store) == "failed" and launched == []

    def test_switched_off_means_failed(self, env):
        svc, store, router, dest, launched = env
        svc.set_propagate_on_fail(False)
        assert svc.propagate_on_fail_enabled() is False
        lxm = run(svc, router, dest)
        lxm.failed_cb(lxm)
        assert state(store) == "failed" and launched == []

    def test_a_cancelled_message_never_falls_back(self, env):
        svc, store, router, dest, launched = env
        lxm = run(svc, router, dest)
        svc.cancel_send("m1", ME)
        lxm.failed_cb(lxm)
        assert state(store) == "cancelled" and launched == []

    def test_a_router_without_the_accessor_does_not_break_a_failure(self, env):
        svc, store, router, dest, launched = env
        del FakeRouter.get_outbound_propagation_node
        try:
            lxm = run(svc, router, dest)
            lxm.failed_cb(lxm)
        finally:
            FakeRouter.get_outbound_propagation_node = lambda self: self.node
        assert state(store) == "failed"

    def test_on_by_default(self, env):
        assert env[0].propagate_on_fail_enabled() is True


class TestHeldMessages:
    def test_a_held_message_is_not_retried_or_resent(self, env):
        svc, store, router, dest, launched = env
        store.update_sent("m1", "held")
        svc._get_user_router = lambda sub: {"router": router, "dest": dest}
        assert svc.retry_send("m1", ME)[0] is False
        assert svc.resend_failed_for(PEER) == 0

    def test_a_held_message_cannot_be_cancelled(self, env):
        svc, store, *_ = env
        store.update_sent("m1", "held")
        ok, why = svc.cancel_send("m1", ME)
        assert not ok and "held" in why


class TestSetting:
    def test_on_by_default_and_persisted(self, tmp_path):
        from nomadnet_web.ui_settings import UISettings
        ui = UISettings(str(tmp_path))
        assert ui.get_all()["lxmf_propagate_on_fail"] is True
        ui.update({"lxmf_propagate_on_fail": False})
        assert UISettings(str(tmp_path)).get_all()["lxmf_propagate_on_fail"] is False

"""Tests for ``MessagingService._send()``'s sent-message storage.

Motivation: sent-message entries only ever stored a 120-char ``preview``,
never the full ``content``. The frontend's ``renderChatLog()`` falls back
to ``preview`` whenever ``content`` is missing, so every sent message
shown in the sender's own open conversation was silently clipped at 120
characters — even though the full text was, and still is, what actually
went out over LXMF. This guards the fix: the full content must always be
stored alongside the preview.

``_send()`` spawns a background delivery thread that does real RNS/LXMF
work (path discovery, link establishment) once started — irrelevant to
what changed here, and not something a unit test should exercise. These
tests stub ``threading.Thread`` so that thread is never actually started;
we only assert on the synchronous prefix of ``_send()`` — building the
entry dict and calling ``save_sent`` — which is deterministic and runs
before the (stubbed-out) thread would begin.
"""

import types

import pytest

from nomadnet_web.messaging import MessagingService

LONG_MESSAGE = "x" * 500  # comfortably past the old 120-char preview cutoff
DEST_HASH = "aa" * 16


class _StubMessageStore:
    """Records what's passed to save_sent; nothing else is exercised."""

    def __init__(self):
        self.saved = []

    def save_sent(self, entry):
        self.saved.append(entry)


class _NoOpThread:
    """Stand-in for ``threading.Thread``.

    Captures the target but never runs it — ``_deliver()``'s RNS/LXMF
    network calls have nothing to do with what these tests check, and
    running them for real would need an actual RNS instance.
    """

    def __init__(self, target=None, daemon=None, **kwargs):
        self.target = target

    def start(self):
        pass


@pytest.fixture(autouse=True)
def _no_background_delivery(monkeypatch):
    monkeypatch.setattr("nomadnet_web.messaging.threading.Thread", _NoOpThread)


@pytest.fixture
def service(tmp_path):
    svc = MessagingService(
        storage_path=str(tmp_path), message_store=_StubMessageStore()
    )
    # Bypass real identity/router setup — _send() only needs a dict with
    # "dest" (read via .hexhash inside the stubbed-out delivery thread,
    # never touched by the synchronous path under test) and "router" to
    # get past its early-return guard.
    svc._get_user_router = lambda user_sub: {
        "dest":   types.SimpleNamespace(hexhash=DEST_HASH),
        "router": types.SimpleNamespace(),
    }
    return svc


def test_send_stores_full_content_not_just_preview(service):
    ok, _ = service.send_message(DEST_HASH, LONG_MESSAGE, user_sub="u1")
    assert ok

    saved = service._msg_store.saved
    assert len(saved) == 1
    assert saved[0]["content"] == LONG_MESSAGE
    assert len(saved[0]["content"]) == 500


def test_preview_stays_truncated_for_the_conversation_list(service):
    ok, _ = service.send_message(DEST_HASH, LONG_MESSAGE, user_sub="u1")
    assert ok

    entry = service._msg_store.saved[0]
    assert entry["preview"] == LONG_MESSAGE[:120]
    assert len(entry["preview"]) == 120


def test_short_message_content_and_preview_match(service):
    ok, _ = service.send_message(DEST_HASH, "hello", user_sub="u1")
    assert ok

    entry = service._msg_store.saved[0]
    assert entry["content"] == "hello"
    assert entry["preview"] == "hello"


def test_empty_content_stores_empty_not_none(service):
    # _send() guards content with `content or ""` — verify that survives
    # for both fields rather than storing None (which would break the
    # frontend's `m.content || m.preview || ''` fallback chain).
    ok, _ = service.send_message(DEST_HASH, "", user_sub="u1")
    assert ok

    entry = service._msg_store.saved[0]
    assert entry["content"] == ""
    assert entry["preview"] == ""

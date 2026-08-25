"""Tests for outbound-attachment persistence + LXMF field assembly
in ``MessagingService._send`` / ``send_message`` (v1.3.0 step 4).

These tests bypass the actual RNS delivery thread — that requires a
live Transport, a delivery destination, and a working identity — by
monkeypatching ``_get_user_router`` to return a stub and asserting
on the outcome we CAN see synchronously:

- The message entry is saved to the store with the right attachment
  metadata (``kind`` / ``filename`` / ``mime`` / ``size`` / ``idx``).
- Blobs land on disk under the AttachmentStore with the right bytes.
- The MIME → LXMF field classifier picks the right slot
  (``FIELD_IMAGE`` / ``FIELD_AUDIO`` / ``FIELD_FILE_ATTACHMENTS``).

The delivery-side field assembly runs inside a background thread;
we validate the classifier separately via the exported helper
``_classify_attachment_kind`` rather than trying to intercept
LXMF.LXMessage construction.
"""

import os

import pytest

from nomadnet_web.attachment_store import AttachmentStore
from nomadnet_web.message_store import MessageStore
from nomadnet_web.messaging import (
    MessagingService,
    _MIME_TO_AUDIO_MODE,
    _MIME_TO_IMAGE_EXT,
    _classify_attachment_kind,
)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """MessagingService with a stub router so ``send_message`` runs
    synchronously up to the point of queueing the delivery thread.

    Monkeypatch ``_get_user_router`` to return a truthy dict so the
    "no delivery identity" early-return doesn't trip. The delivery
    thread will fail (no real router), but that's OK — the
    attachment persistence and message-store write happen BEFORE
    the thread starts, so the outcomes we care about are already
    observable.
    """
    att = AttachmentStore(str(tmp_path))
    msg = MessageStore(str(tmp_path), attachment_store=att)
    lxmf_path = os.path.join(str(tmp_path), "lxmf")
    service = MessagingService(
        storage_path=lxmf_path,
        message_store=msg,
        attachment_store=att,
    )

    class _StubDest:
        hexhash = "aa" * 16

    def _fake_get_user_router(_user_sub):
        return {
            "router":   None,       # thread will crash trying to use it
            "dest":     _StubDest(),
            "identity": None,
        }
    monkeypatch.setattr(
        service, "_get_user_router", _fake_get_user_router,
    )
    # ``_send`` spawns a daemon thread that tries to touch RNS.Transport.
    # Without a live Reticulum instance that raises, which is caught by
    # the thread's except-and-log block — noisy in test output but not
    # a functional failure. Neuter the thread here so the test log stays
    # readable: the persistence + entry-write happen BEFORE the thread
    # would have started, so nothing under test needs the delivery half.
    import threading
    class _NoopThread:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        daemon = True
    monkeypatch.setattr(threading, "Thread", _NoopThread)
    return service, msg, att


class TestClassifier:
    """MIME → attachment kind. Drives the FIELD_* routing choice
    in ``_send`` / ``_deliver``."""

    def test_image_mimes_classify_as_image(self):
        assert _classify_attachment_kind("image/jpeg") == "image"
        assert _classify_attachment_kind("image/png")  == "image"
        assert _classify_attachment_kind("image/webp") == "image"

    def test_audio_mimes_classify_as_audio(self):
        assert _classify_attachment_kind("audio/opus") == "audio"
        assert _classify_attachment_kind("audio/mpeg") == "audio"
        assert _classify_attachment_kind("audio/webm") == "audio"
        assert _classify_attachment_kind("audio/mp4")  == "audio"

    def test_generic_mimes_classify_as_file(self):
        assert _classify_attachment_kind("application/pdf")  == "file"
        assert _classify_attachment_kind("text/plain")        == "file"
        assert _classify_attachment_kind("application/zip")   == "file"

    def test_image_variant_without_native_slot_falls_back_to_file(self):
        # image/heic is a real image MIME but MeshChat's FIELD_IMAGE
        # parser doesn't know it — safer to send as a generic file
        # than to attach bytes the recipient can't decode.
        assert _classify_attachment_kind("image/heic") == "file"

    def test_audio_variant_without_native_slot_falls_back_to_file(self):
        # Similarly for audio codecs we don't have in the map.
        assert _classify_attachment_kind("audio/vnd.rn-realaudio") == "file"

    def test_case_insensitive(self):
        assert _classify_attachment_kind("IMAGE/JPEG") == "image"
        assert _classify_attachment_kind("Audio/Opus") == "audio"

    def test_empty_or_missing_classifies_as_file(self):
        assert _classify_attachment_kind("")   == "file"
        assert _classify_attachment_kind(None) == "file"


class TestOutboundAttachmentPersistence:
    """Attachments handed to ``send_message`` land on disk + in the
    sent-message entry BEFORE the delivery thread starts."""

    def test_single_image_persists_to_store_and_entry(self, svc):
        service, msg, att = svc
        ok, msg_id = service.send_message(
            dest_hash_hex="bb" * 16,
            content="here's a pic",
            title="",
            user_sub="user1",
            attachments=[{
                "data":     b"\xff\xd8\xff\xe0fake-jpeg-body",
                "filename": "photo.jpg",
                "mime":     "image/jpeg",
            }],
        )
        assert ok
        assert msg_id  # uuid string
        # Fetch sent entry from the store
        sent = msg.sent_messages()
        assert len(sent) == 1
        entry = sent[0]
        assert entry["id"] == msg_id
        assert entry["dest"] == "bb" * 16
        assert entry["content"] == "here's a pic"
        assert "attachments" in entry
        assert len(entry["attachments"]) == 1
        a0 = entry["attachments"][0]
        assert a0["kind"]     == "image"
        assert a0["idx"]      == 0
        assert a0["filename"] == "photo.jpg"
        assert a0["mime"]     == "image/jpeg"
        assert a0["size"]     == len(b"\xff\xd8\xff\xe0fake-jpeg-body")
        # Blob on disk
        assert att.read(msg_id, 0) == b"\xff\xd8\xff\xe0fake-jpeg-body"

    def test_multiple_kinds_persist_with_correct_classification(self, svc):
        service, msg, att = svc
        ok, msg_id = service.send_message(
            dest_hash_hex="cc" * 16,
            content="kitchen sink",
            user_sub="user1",
            attachments=[
                {"data": b"IMG",  "filename": "a.png",  "mime": "image/png"},
                {"data": b"AUDIO","filename": "b.opus", "mime": "audio/opus"},
                {"data": b"PDF",  "filename": "c.pdf",  "mime": "application/pdf"},
            ],
        )
        assert ok
        entry = msg.sent_messages()[0]
        kinds = [a["kind"] for a in entry["attachments"]]
        assert kinds == ["image", "audio", "file"]
        # Indexes are sequential — same as the receive-side convention
        assert [a["idx"] for a in entry["attachments"]] == [0, 1, 2]
        # Each blob readable at its slot
        assert att.read(msg_id, 0) == b"IMG"
        assert att.read(msg_id, 1) == b"AUDIO"
        assert att.read(msg_id, 2) == b"PDF"

    def test_missing_mime_defaults_to_octet_stream_and_file_kind(self, svc):
        service, msg, att = svc
        ok, msg_id = service.send_message(
            dest_hash_hex="dd" * 16,
            content="mystery bytes",
            user_sub="user1",
            attachments=[{
                "data":     b"???",
                "filename": "mystery.bin",
                "mime":     "",   # empty → octet-stream fallback
            }],
        )
        assert ok
        a0 = msg.sent_messages()[0]["attachments"][0]
        assert a0["mime"] == "application/octet-stream"
        assert a0["kind"] == "file"

    def test_text_only_send_has_no_attachments_key(self, svc):
        service, msg, _ = svc
        ok, _msg_id = service.send_message(
            dest_hash_hex="ee" * 16,
            content="just text",
            user_sub="user1",
        )
        assert ok
        entry = msg.sent_messages()[0]
        assert "attachments" not in entry

    def test_empty_attachments_list_treated_as_none(self, svc):
        service, msg, _ = svc
        ok, _msg_id = service.send_message(
            dest_hash_hex="ff" * 16,
            content="just text again",
            user_sub="user1",
            attachments=[],
        )
        assert ok
        assert "attachments" not in msg.sent_messages()[0]

    def test_non_bytes_data_is_skipped(self, svc):
        # A caller mistake (str where bytes expected) shouldn't crash
        # the send — the bad entry is skipped, good ones still land.
        service, msg, att = svc
        ok, msg_id = service.send_message(
            dest_hash_hex="ab" * 16,
            content="mixed",
            user_sub="user1",
            attachments=[
                {"data": "not-bytes", "filename": "a.txt", "mime": "text/plain"},
                {"data": b"ok",       "filename": "b.txt", "mime": "text/plain"},
            ],
        )
        assert ok
        # Only the second entry landed — and it took idx 0 (the bad one
        # was skipped before it consumed an idx).
        atts = msg.sent_messages()[0]["attachments"]
        assert len(atts) == 1
        assert atts[0]["filename"] == "b.txt"
        assert atts[0]["idx"] == 0
        assert att.read(msg_id, 0) == b"ok"


class TestReverseMimeMaps:
    """The MIME → wire-format-string maps that ``_deliver`` uses to
    populate ``FIELD_IMAGE[0]`` / ``FIELD_AUDIO[0]``.

    These are the interop-critical bits: MeshChat's parser keys off
    the extension string, not the raw MIME.
    """

    def test_image_mime_to_ext_covers_common_formats(self):
        assert _MIME_TO_IMAGE_EXT["image/jpeg"] == "jpg"
        assert _MIME_TO_IMAGE_EXT["image/png"]  == "png"
        assert _MIME_TO_IMAGE_EXT["image/gif"]  == "gif"
        assert _MIME_TO_IMAGE_EXT["image/webp"] == "webp"

    def test_audio_mime_to_mode_covers_common_codecs(self):
        assert _MIME_TO_AUDIO_MODE["audio/opus"] == "opus"
        assert _MIME_TO_AUDIO_MODE["audio/mpeg"] == "mp3"
        assert _MIME_TO_AUDIO_MODE["audio/wav"]  == "wav"
        assert _MIME_TO_AUDIO_MODE["audio/webm"] == "webm"

    def test_maps_agree_with_receive_side_extensions(self):
        # For every image ext we send, the receive side has a MIME
        # mapping — otherwise sender and recipient would disagree
        # about how to render the same bytes.
        from nomadnet_web.messaging import (
            _AUDIO_MODE_TO_EXT,
            _AUDIO_MODE_TO_MIME,
            _IMAGE_EXT_TO_MIME,
        )
        for mime, ext in _MIME_TO_IMAGE_EXT.items():
            assert ext in _IMAGE_EXT_TO_MIME, \
                f"send ext '{ext}' has no receive mapping"
        for mime, mode in _MIME_TO_AUDIO_MODE.items():
            assert mode in _AUDIO_MODE_TO_MIME, \
                f"send audio mode '{mode}' has no receive mapping"
            assert mode in _AUDIO_MODE_TO_EXT, \
                f"send audio mode '{mode}' has no receive ext"

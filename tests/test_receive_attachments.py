"""Tests for inbound-image attachment extraction in
``MessagingService._on_delivery`` (v1.3.0 step 2).

The icon-vs-attachment heuristic (see ``docs/design/chat-uploads.md``):
- ``FIELD_ICON_APPEARANCE`` (0x04) is always a contact icon.
- ``FIELD_IMAGE`` (0x06) is a contact icon when the message has no
  text content, and a message attachment when it has text.

The tests below drive ``_on_delivery`` directly with mock LXMF-style
messages and assert on the persisted message-store entry + the
attachment-store blob.
"""

import os

import pytest

from nomadnet_web.attachment_store import AttachmentStore
from nomadnet_web.message_store import MessageStore
from nomadnet_web.messaging import MessagingService


class _MockMessage:
    """Minimal shape of an LXMF ``LXMessage`` sufficient for
    ``_on_delivery``. Field values match the wire format (bytes for
    title/content, dict of int-keyed field values).
    """
    def __init__(self, *, msg_id, source, title=b"", content=b"", fields=None):
        self.hash        = bytes.fromhex(msg_id)
        self.source_hash = bytes.fromhex(source) if source else None
        self.title       = title
        self.content     = content
        self.fields      = fields or {}


@pytest.fixture
def svc(tmp_path):
    """Fresh MessagingService wired to a temp AttachmentStore +
    MessageStore. No RNS involvement — we drive ``_on_delivery``
    directly so the whole thing runs offline.
    """
    att = AttachmentStore(str(tmp_path))
    msg = MessageStore(str(tmp_path), attachment_store=att)
    # Use a subdir for lxmf storage — MessagingService creates it
    lxmf_path = os.path.join(str(tmp_path), "lxmf")
    service = MessagingService(
        storage_path=lxmf_path,
        message_store=msg,
        attachment_store=att,
    )
    return service, msg, att


# Two fixture blobs — small but recognisable to _detect_image_mime.
_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"payload_bytes" * 4
_FAKE_PNG  = b"\x89PNG\r\n\x1a\n" + b"payload_bytes" * 4


class TestImageAttachment:
    """FIELD_IMAGE + text content → treated as inline attachment."""

    def test_image_with_content_becomes_attachment(self, svc):
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="aa" * 16, source="bb" * 16,
            title=b"", content=b"here's a photo",
            fields={0x06: ["jpg", _FAKE_JPEG]},
        ), user_sub="user1")

        entries = msg.received_messages()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["attachments"], "expected an image attachment"
        att0 = entry["attachments"][0]
        assert att0["kind"] == "image"
        assert att0["mime"] == "image/jpeg"
        assert att0["size"] == len(_FAKE_JPEG)
        assert att0["idx"]  == 0
        # And the blob is on disk with the correct bytes
        assert att.read(entry["id"], 0) == _FAKE_JPEG

    def test_image_with_title_only_still_becomes_attachment(self, svc):
        # ``title`` counts as text content — a subject-line-only
        # message with an image is still a chat delivery, not an
        # announce-style icon update.
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="cc" * 16, source="dd" * 16,
            title=b"look at this", content=b"",
            fields={0x06: ["png", _FAKE_PNG]},
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        assert entry["attachments"]
        assert entry["attachments"][0]["mime"] == "image/png"

    def test_image_mime_fallback_to_sniffing(self, svc):
        # Sender doesn't set the image_type_str field — MIME resolves
        # via ``_detect_image_mime`` on the byte header.
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="11" * 16, source="22" * 16,
            title=b"", content=b"no type on this one",
            fields={0x06: ["", _FAKE_JPEG]},  # empty type
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        assert entry["attachments"][0]["mime"] == "image/jpeg"

    def test_unknown_extension_uses_sniffed_mime(self, svc):
        # An extension not in the whitelist (``bmp``) still resolves
        # via byte-sniffing so we don't silently produce
        # ``application/octet-stream`` for a legit image.
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="33" * 16, source="44" * 16,
            title=b"", content=b"unknown ext",
            fields={0x06: ["bmp", _FAKE_PNG]},   # bmp not in the map
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        # PNG magic wins the sniff
        assert entry["attachments"][0]["mime"] == "image/png"


class TestIconVsAttachmentHeuristic:
    """Same FIELD_IMAGE input, different meaning depending on whether
    the message carries text."""

    def test_no_text_content_treats_image_as_icon(self, svc):
        # Announce-shaped delivery: no title, no content, just an
        # image. Historical behaviour was to update the contact's
        # icon. Keep that intact — no attachment persisted.
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="55" * 16, source="66" * 16,
            title=b"", content=b"",
            fields={0x06: ["png", _FAKE_PNG]},
        ), user_sub="user1")

        entry = msg.received_messages()[0]
        assert "attachments" not in entry
        assert att.read(entry["id"], 0) is None

    def test_field_04_always_icon_regardless_of_content(self, svc):
        # FIELD_ICON_APPEARANCE (0x04) is a vector descriptor, not a
        # file — should never be treated as an attachment, even if
        # the message also has text content.
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="77" * 16, source="88" * 16,
            title=b"heads up", content=b"still just an icon-descriptor field",
            fields={0x04: ["star", b"\xff\x00\x00", b"\x00\x00\xff"]},
        ), user_sub="user1")

        entry = msg.received_messages()[0]
        assert "attachments" not in entry


class TestFileAttachments:
    """FIELD_FILE_ATTACHMENTS (0x05) — array of [name, bytes] tuples.
    Each entry becomes its own attachment slot in the message entry.
    (v1.3.0 step 3.)
    """

    def test_single_file_attachment(self, svc):
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="ba" * 16, source="ca" * 16,
            title=b"", content=b"here's the spec",
            fields={0x05: [["spec.pdf", b"%PDF-1.4 payload"]]},
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        assert len(entry["attachments"]) == 1
        f0 = entry["attachments"][0]
        assert f0["kind"] == "file"
        assert f0["filename"] == "spec.pdf"
        assert f0["mime"] == "application/pdf"
        assert att.read(entry["id"], 0) == b"%PDF-1.4 payload"

    def test_multiple_file_attachments_get_sequential_indexes(self, svc):
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="bb" * 16, source="cb" * 16,
            title=b"", content=b"three docs",
            fields={0x05: [
                ["a.pdf", b"PDF"],
                ["b.txt", b"plain"],
                ["c.zip", b"PK\x03\x04"],
            ]},
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        assert [a["idx"] for a in entry["attachments"]] == [0, 1, 2]
        assert [a["filename"] for a in entry["attachments"]] == [
            "a.pdf", "b.txt", "c.zip",
        ]
        # All blobs distinct + readable
        assert att.read(entry["id"], 0) == b"PDF"
        assert att.read(entry["id"], 1) == b"plain"
        assert att.read(entry["id"], 2) == b"PK\x03\x04"

    def test_unknown_extension_falls_back_to_octet_stream(self, svc):
        # Extension mimetypes.guess_type doesn't know → sane default
        # so the browser can still download the blob.
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="bc" * 16, source="cc" * 16,
            title=b"", content=b"weird ext",
            fields={0x05: [["mystery.blorp", b"data"]]},
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        assert entry["attachments"][0]["mime"] == "application/octet-stream"

    def test_malformed_file_entry_is_skipped(self, svc):
        # A garbage entry in the array (not a [name, bytes] pair)
        # doesn't crash the receive path — just gets skipped, other
        # entries in the same message still land.
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="bd" * 16, source="cd" * 16,
            title=b"", content=b"mixed",
            fields={0x05: [
                "not-a-tuple",         # garbage
                ["ok.txt", b"good"],   # real
                ["only-name"],         # too short
                [None, b"no-name"],    # non-str name — accepted, falls back to "file"
            ]},
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        # Two attachments landed (the good.txt and the None-name one)
        assert len(entry["attachments"]) == 2

    def test_image_and_files_both_land(self, svc):
        # Both fields present — image lands as idx 0, files after.
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="be" * 16, source="ce" * 16,
            title=b"", content=b"mixed bag",
            fields={
                0x06: ["jpg", _FAKE_JPEG],
                0x05: [["note.txt", b"content"]],
            },
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        assert len(entry["attachments"]) == 2
        assert entry["attachments"][0]["kind"] == "image"
        assert entry["attachments"][1]["kind"] == "file"


class TestAudioAttachment:
    """FIELD_AUDIO (0x07) — [audio_mode_str, audio_bytes]. audio_mode
    is a codec/container identifier (e.g. ``"opus"``, ``"webm"``).
    (v1.3.0 step 3.)
    """

    def test_opus_audio_attachment(self, svc):
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="e0" * 16, source="d0" * 16,
            title=b"", content=b"voice note",
            fields={0x07: ["opus", b"OggS\x00opus_frames_here"]},
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        assert len(entry["attachments"]) == 1
        a0 = entry["attachments"][0]
        assert a0["kind"]     == "audio"
        assert a0["mime"]     == "audio/opus"
        assert a0["filename"] == "audio.opus"

    def test_webm_audio_attachment(self, svc):
        # Browsers' MediaRecorder often emits webm audio — check we
        # accept and label it correctly.
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="e1" * 16, source="d1" * 16,
            title=b"", content=b"voice note",
            fields={0x07: ["webm", b"webm audio bytes"]},
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        assert entry["attachments"][0]["mime"] == "audio/webm"

    def test_unknown_audio_mode_still_persists(self, svc):
        # An unknown codec still saves the bytes (with an
        # application/octet-stream MIME so the browser can at least
        # download the blob).
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="e2" * 16, source="d2" * 16,
            title=b"", content=b"weird codec",
            fields={0x07: ["weirdcodec", b"raw bytes"]},
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        assert entry["attachments"][0]["mime"] == "application/octet-stream"
        assert entry["attachments"][0]["filename"] == "audio"

    def test_all_three_kinds_together(self, svc):
        # Image + files + audio all in one message — order in the
        # attachments array is image, files, audio (matches MeshChat's
        # own send-side ordering).
        service, msg, att = svc
        service._on_delivery(_MockMessage(
            msg_id="e3" * 16, source="d3" * 16,
            title=b"", content=b"kitchen sink",
            fields={
                0x06: ["png", _FAKE_PNG],
                0x05: [["note.txt", b"n"], ["doc.pdf", b"p"]],
                0x07: ["opus", b"o"],
            },
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        kinds = [a["kind"] for a in entry["attachments"]]
        assert kinds == ["image", "file", "file", "audio"]


class TestNoAttachmentStore:
    """MessagingService without an attachment_store still processes
    inbound messages — just doesn't persist blobs."""

    def test_no_attachment_store_skips_blob_persist(self, tmp_path):
        # Old-style construction: no attachment_store passed.
        # Message goes through fine; the attachments-array field
        # simply isn't populated.
        msg = MessageStore(str(tmp_path))
        service = MessagingService(
            storage_path=os.path.join(str(tmp_path), "lxmf"),
            message_store=msg,
            attachment_store=None,   # explicit
        )
        service._on_delivery(_MockMessage(
            msg_id="99" * 16, source="aa" * 16,
            title=b"", content=b"has content",
            fields={0x06: ["jpg", _FAKE_JPEG]},
        ), user_sub="user1")
        entry = msg.received_messages()[0]
        assert "attachments" not in entry

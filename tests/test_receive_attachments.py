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

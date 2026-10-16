"""What goes on the wire for attached audio: runs the real delivery code
and inspects the fields of the LXMF message it builds.

``FIELD_AUDIO`` (0x07) must be ``[0x10, ogg_bytes]`` (LXMF.AM_OPUS_OGG, an
integer) for Ogg Opus, which is the only audio MeshChat plays inline.
Everything else is an ordinary file attachment (0x05).
"""

import os
import types

import pytest

import LXMF
from nomadnet_web.attachment_store import AttachmentStore
from nomadnet_web.message_store import MessageStore
from nomadnet_web.messaging import MessagingService

PEER = "bb" * 16
OGG = b"OggS\x00\x02" + b"\x00" * 24


class FakeLXMessage:
    OPPORTUNISTIC = 1
    made = []

    def __init__(self, dest, source, content, title="", fields=None, desired_method=None):
        self.fields = fields
        self.message_id = os.urandom(32)
        self.hash = os.urandom(32)
        FakeLXMessage.made.append(self)

    def register_delivery_callback(self, cb):
        pass

    def register_failed_callback(self, cb):
        pass


class FakeRouter:
    def handle_outbound(self, lxm):
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
def svc(tmp_path):
    att = AttachmentStore(str(tmp_path))
    store = MessageStore(str(tmp_path), attachment_store=att)
    s = MessagingService(storage_path=str(tmp_path / "lxmf"), message_store=store,
                         attachment_store=att)
    return s, att


def send(svc_att, attachments):
    """Store the blobs the way _send does, then run the delivery body."""
    svc, att = svc_att
    # Same kind decision production makes when a message is queued.
    from nomadnet_web.messaging import _classify_attachment_kind
    meta = []
    for i, (name, mime, data) in enumerate(attachments):
        att.write("m1", i, name, data)
        meta.append({"kind": _classify_attachment_kind(mime), "idx": i,
                     "filename": name, "mime": mime, "size": len(data)})
    svc._run_delivery(
        msg_id="m1", dest_hash_hex=PEER, dest_hash=bytes.fromhex(PEER), title="",
        content="hi", user_sub="u", att_meta=meta, router=FakeRouter(),
        source_dest=types.SimpleNamespace(hexhash="aa" * 16),
    )
    return FakeLXMessage.made[-1].fields or {}


class TestOnTheWire:
    def test_ogg_opus_is_an_integer_mode_audio_field(self, svc):
        fields = send(svc, [("voice.ogg", "audio/ogg", OGG)])
        assert fields[LXMF.FIELD_AUDIO] == [LXMF.AM_OPUS_OGG, OGG]
        assert type(fields[LXMF.FIELD_AUDIO][0]) is int
        assert LXMF.FIELD_FILE_ATTACHMENTS not in fields

    def test_the_opus_mime_is_also_sent_as_audio(self, svc):
        fields = send(svc, [("clip.opus", "audio/opus", OGG)])
        assert fields[LXMF.FIELD_AUDIO] == [LXMF.AM_OPUS_OGG, OGG]

    @pytest.mark.parametrize("name,mime", [
        ("song.mp3", "audio/mpeg"), ("voice.webm", "audio/webm"), ("a.m4a", "audio/mp4"),
        ("a.wav", "audio/wav"),
    ])
    def test_other_audio_is_a_file_attachment(self, svc, name, mime):
        fields = send(svc, [(name, mime, b"\x00audio bytes")])
        assert LXMF.FIELD_AUDIO not in fields
        assert fields[LXMF.FIELD_FILE_ATTACHMENTS] == [[name, b"\x00audio bytes"]]

    def test_a_mislabelled_file_is_not_sent_as_audio(self, svc):
        fields = send(svc, [("evil.ogg", "audio/ogg", b"<html>not an ogg stream</html>")])
        assert LXMF.FIELD_AUDIO not in fields
        assert fields[LXMF.FIELD_FILE_ATTACHMENTS][0][0] == "evil.ogg"

    def test_only_one_audio_field_the_second_clip_becomes_a_file(self, svc):
        fields = send(svc, [("a.ogg", "audio/ogg", OGG), ("b.ogg", "audio/ogg", OGG + b"2")])
        assert fields[LXMF.FIELD_AUDIO] == [LXMF.AM_OPUS_OGG, OGG]
        assert fields[LXMF.FIELD_FILE_ATTACHMENTS] == [["b.ogg", OGG + b"2"]]

    def test_audio_alongside_an_image(self, svc):
        fields = send(svc, [("p.png", "image/png", b"\x89PNG\r\n\x1a\n"), ("v.ogg", "audio/ogg", OGG)])
        assert fields[LXMF.FIELD_IMAGE][0] == "png"             # image types stay short strings
        assert fields[LXMF.FIELD_AUDIO][0] == LXMF.AM_OPUS_OGG

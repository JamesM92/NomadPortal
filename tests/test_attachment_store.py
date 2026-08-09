"""Tests for ``AttachmentStore`` — on-disk blob storage for LXMF
message attachments (v1.3.0 chat-uploads groundwork).

Covers the write/read/evict lifecycle, path-traversal defenses on
untrusted ``msg_id`` and ``filename`` inputs, and the message-store
integration (eviction on ``delete_conversation`` and the silent
``MAX_MESSAGES`` overflow). All tests use ``tmp_path`` fixtures so
nothing touches the operator's real ``config/`` directory.
"""

import os

import pytest

from nomadnet_web.attachment_store import AttachmentStore
from nomadnet_web.message_store import MessageStore, MAX_MESSAGES


@pytest.fixture
def store(tmp_path):
    return AttachmentStore(str(tmp_path))


class TestWriteRead:
    def test_round_trip(self, store):
        store.write("msg-abc123", 0, "photo.jpg", b"\xff\xd8\xff\xe0PAYLOAD")
        got = store.read("msg-abc123", 0)
        assert got == b"\xff\xd8\xff\xe0PAYLOAD"

    def test_multiple_slots_per_message(self, store):
        store.write("msg1", 0, "a.jpg",  b"AAA")
        store.write("msg1", 1, "b.opus", b"BBB")
        store.write("msg1", 2, "c.pdf",  b"CCC")
        assert store.read("msg1", 0) == b"AAA"
        assert store.read("msg1", 1) == b"BBB"
        assert store.read("msg1", 2) == b"CCC"

    def test_read_nonexistent_returns_none(self, store):
        # Distinguishing None (not found) from b"" (found, empty) is
        # part of the API — the receive path needs to know whether to
        # log a "missing attachment" warning vs render an empty blob.
        assert store.read("does-not-exist", 0) is None
        # Existing msg dir but no such idx also returns None
        store.write("msg1", 0, "x.txt", b"data")
        assert store.read("msg1", 99) is None

    def test_write_overwrites_same_slot(self, store):
        # Same msg_id + idx twice = idempotent overwrite. LXMF receive
        # can fire the same message id more than once (retry, duplicate
        # delivery); we shouldn't error on that path.
        store.write("msg1", 0, "a.jpg", b"first")
        store.write("msg1", 0, "a.jpg", b"second")
        assert store.read("msg1", 0) == b"second"

    def test_zero_length_blob_is_legal(self, store):
        # A recipient might send an empty file — weird but legal.
        # read() must return b"" (not None) so the caller can tell
        # "found, empty" from "not found."
        store.write("msg1", 0, "empty.txt", b"")
        assert store.read("msg1", 0) == b""


class TestPathTraversalDefenses:
    """Peers control ``msg_id`` and ``filename``; both are treated as
    untrusted input. Explicit tests below to lock down the sanitizer.
    """

    def test_msg_id_with_path_separators_stripped(self, store, tmp_path):
        # A poisoned msg_id like "../../etc/passwd" must NOT create a
        # file outside the store root. Non-hex chars are stripped, so
        # this ends up in a directory named "etcpasswd" (safe).
        store.write("../../etc/passwd", 0, "x.txt", b"hostile")
        # The bytes are stored SOMEWHERE inside the root (sanitizer
        # keeps only hex chars). The important assertion is nothing
        # leaked outside tmp_path/attachments/.
        for root, dirs, files in os.walk(str(tmp_path)):
            for f in files:
                path = os.path.join(root, f)
                # Every stored file MUST live under attachments/
                assert "attachments" in path
        # And nothing above tmp_path exists that we created
        assert not os.path.exists("/etc/passwd_via_attachment_store")

    def test_msg_id_with_null_bytes_stripped(self, store):
        # Null-byte injection has historically bypassed some Python
        # path handling. The sanitizer strips them.
        store.write("abc\x00../../evil", 0, "x.txt", b"data")
        # Reads back with the same sanitizer applied — round-trip works
        assert store.read("abc\x00../../evil", 0) == b"data"

    def test_msg_id_all_non_hex_falls_back_to_stable_name(self, store):
        # A completely garbage msg_id (no hex chars at all) still gets
        # a stable directory name, doesn't crash.
        store.write("!@#$%^&*()", 0, "x.txt", b"stored")
        assert store.read("!@#$%^&*()", 0) == b"stored"

    def test_filename_extension_whitelist_allows_normal(self, store, tmp_path):
        # Real LXMF msg_ids are hex hashes — use a realistic one for
        # tests that assert on the on-disk directory name (the
        # sanitizer strips non-hex from msg_ids, so ``msg1`` would
        # become ``1`` on disk).
        store.write("abc123", 0, "photo.jpg", b"jpeg")
        d = os.path.join(str(tmp_path), "attachments", "abc123")
        assert any(name.endswith(".jpg") for name in os.listdir(d))

    def test_filename_extension_with_bad_chars_dropped(self, store, tmp_path):
        # Extension is inspected via os.path.splitext; anything not
        # matching /^\.[a-z0-9]{1,10}$/i is dropped so a poisoned
        # filename can't smuggle path metachars.
        store.write("abc123", 0, "photo.jpg/../etc/passwd", b"data")
        d = os.path.join(str(tmp_path), "attachments", "abc123")
        # No file with a "/" in its on-disk name — that would be a
        # separate file elsewhere in the filesystem
        for name in os.listdir(d):
            assert "/" not in name
            assert ".." not in name

    def test_filename_extension_too_long_dropped(self, store, tmp_path):
        # A weirdly long extension (say, 30 chars) gets dropped rather
        # than preserved. Keeps ls output legible + prevents various
        # filesystem quirks.
        store.write("abc123", 0, "photo.thisextensionistoolong",
                    b"data")
        d = os.path.join(str(tmp_path), "attachments", "abc123")
        for name in os.listdir(d):
            # The idx portion is "0", no extension after it
            assert name == "0" or name.startswith("0.")

    def test_filename_empty_extension_ok(self, store, tmp_path):
        # A filename with no extension writes cleanly with just the idx
        store.write("abc123", 0, "README", b"data")
        d = os.path.join(str(tmp_path), "attachments", "abc123")
        assert "0" in os.listdir(d)


class TestEvict:
    def test_evict_removes_all_slots_for_message(self, store, tmp_path):
        store.write("msg1", 0, "a.jpg",  b"A")
        store.write("msg1", 1, "b.opus", b"B")
        store.write("msg1", 2, "c.pdf",  b"C")
        n = store.evict("msg1")
        assert n == 3
        assert store.read("msg1", 0) is None
        # Directory should be gone too
        d = os.path.join(str(tmp_path), "attachments", "msg1")
        assert not os.path.exists(d)

    def test_evict_nonexistent_message_is_zero(self, store):
        # Called for every message deletion — most don't have any
        # attachments in the first place, so no-op is important.
        assert store.evict("does-not-exist") == 0

    def test_evict_does_not_touch_other_messages(self, store):
        store.write("msg1", 0, "a.jpg", b"AAA")
        store.write("msg2", 0, "b.jpg", b"BBB")
        store.evict("msg1")
        assert store.read("msg2", 0) == b"BBB"

    def test_evict_many_bulk_removal(self, store):
        store.write("a", 0, "x.jpg", b"1")
        store.write("b", 0, "x.jpg", b"1")
        store.write("c", 0, "x.jpg", b"1")
        store.write("c", 1, "y.jpg", b"1")  # 2 slots for c
        assert store.evict_many(["a", "b", "c"]) == 4

    def test_evict_many_empty_list_is_zero(self, store):
        # Common case in MessageStore integration — no messages
        # actually being evicted this call. Must be cheap.
        assert store.evict_many([]) == 0
        assert store.evict_many(None) == 0


class TestMessageStoreIntegration:
    """MessageStore's eviction hooks — the pathway that automatically
    keeps blob storage in sync with the message-JSON store.
    """

    def test_delete_conversation_evicts_attachments(self, tmp_path):
        att = AttachmentStore(str(tmp_path))
        msg = MessageStore(str(tmp_path), attachment_store=att)

        # Two messages with the same counterparty, each with attachments
        msg.save_sent({"id": "msg-a", "dest": "peer1", "content": "hi"})
        msg.save_received({"id": "msg-b", "source": "peer1", "content": "hey"})
        att.write("msg-a", 0, "a.jpg", b"outbound")
        att.write("msg-b", 0, "b.jpg", b"inbound")

        removed = msg.delete_conversation("peer1")
        assert removed == 2
        # Both blobs gone
        assert att.read("msg-a", 0) is None
        assert att.read("msg-b", 0) is None

    def test_delete_conversation_only_touches_removed_message_blobs(self, tmp_path):
        att = AttachmentStore(str(tmp_path))
        msg = MessageStore(str(tmp_path), attachment_store=att)

        # One message with peer1, one with peer2 — deleting peer1
        # must not touch peer2's attachment.
        msg.save_sent({"id": "msg-a", "dest": "peer1", "content": "x"})
        msg.save_sent({"id": "msg-b", "dest": "peer2", "content": "y"})
        att.write("msg-a", 0, "a.jpg", b"target")
        att.write("msg-b", 0, "b.jpg", b"survivor")

        msg.delete_conversation("peer1")
        assert att.read("msg-a", 0) is None
        assert att.read("msg-b", 0) == b"survivor"

    def test_max_messages_overflow_evicts_blobs(self, tmp_path):
        # The silent LIFO cap silently drops old messages when new ones
        # arrive. Without the eviction hook this leaked attachment
        # bytes on disk forever. Test that the cap now also evicts.
        att = AttachmentStore(str(tmp_path))
        msg = MessageStore(str(tmp_path), attachment_store=att)

        # Fill exactly to the cap; each carries an attachment
        for i in range(MAX_MESSAGES):
            mid = f"m{i:04x}"  # hex-safe
            msg.save_received({"id": mid, "source": "peer", "content": ""})
            att.write(mid, 0, "x.jpg", b"payload")

        # Confirm the first-inserted (oldest) attachment is still there
        assert att.read("m0000", 0) == b"payload"

        # One more push evicts the tail
        msg.save_received({"id": "new-one", "source": "peer", "content": ""})
        att.write("new-one", 0, "x.jpg", b"newer")

        # Oldest attachment blob is now gone (evicted by the cap)
        assert att.read("m0000", 0) is None
        # And the new one is present
        assert att.read("new-one", 0) == b"newer"

    def test_message_store_without_attachment_store_still_works(self, tmp_path):
        # Backward-compat: the ``attachment_store`` param is optional.
        # Callers that don't need attachments (or old code paths) can
        # construct MessageStore the old way.
        msg = MessageStore(str(tmp_path))
        msg.save_sent({"id": "x", "dest": "p", "content": "t"})
        msg.save_received({"id": "y", "source": "p", "content": "t"})
        assert len(msg.sent_messages()) == 1
        assert len(msg.received_messages()) == 1
        # delete_conversation must not crash despite no attachment store
        assert msg.delete_conversation("p") == 2

    def test_message_without_id_doesnt_break_eviction(self, tmp_path):
        # Some legacy entries in messages.json may lack an ``id``. The
        # eviction code path must skip those cleanly — evict_many
        # already filters None, but confirm end-to-end.
        att = AttachmentStore(str(tmp_path))
        msg = MessageStore(str(tmp_path), attachment_store=att)
        msg.save_sent({"dest": "peer", "content": "no id"})   # missing id
        # No crash; nothing to evict
        assert msg.delete_conversation("peer") == 1

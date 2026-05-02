"""Persistent sent/received message storage."""

import json
import logging
import os
import threading

log = logging.getLogger(__name__)
MAX_MESSAGES = 500


class MessageStore:
    def __init__(self, storage_dir: str):
        self._path = os.path.join(storage_dir, "messages.json")
        self._lock = threading.Lock()
        self._sent:     list = []
        self._received: list = []
        self._load()

    def save_sent(self, entry: dict) -> None:
        with self._lock:
            self._sent.insert(0, entry)
            self._sent = self._sent[:MAX_MESSAGES]
            snapshot = self._snapshot()
        self._persist(snapshot)

    def save_received(self, entry: dict) -> None:
        with self._lock:
            self._received.insert(0, entry)
            self._received = self._received[:MAX_MESSAGES]
            snapshot = self._snapshot()
        self._persist(snapshot)

    def sent_messages(self) -> list:
        with self._lock:
            return list(self._sent)

    def received_messages(self) -> list:
        with self._lock:
            return list(self._received)

    def update_sent(self, msg_id: str, state: str, real_id: str = None) -> None:
        """Update state (and optionally final ID) of a queued sent message."""
        with self._lock:
            for m in self._sent:
                if m.get("id") == msg_id:
                    m["state"] = state
                    if real_id and real_id != msg_id:
                        m["id"] = real_id
                    snapshot = self._snapshot()
                    break
            else:
                return
        self._persist(snapshot)

    def delete_conversation(self, hash_hex: str, owner: str = "") -> int:
        """Remove all sent+received messages for a given counterparty hash.

        When owner is provided, only messages belonging to that user are removed.
        """
        with self._lock:
            before = len(self._sent) + len(self._received)
            def _keep_sent(m):
                if m.get("dest") != hash_hex:
                    return True
                return bool(owner) and m.get("owner") != owner
            def _keep_recv(m):
                if m.get("source") != hash_hex:
                    return True
                return bool(owner) and m.get("owner") != owner
            self._sent     = [m for m in self._sent     if _keep_sent(m)]
            self._received = [m for m in self._received if _keep_recv(m)]
            removed = before - len(self._sent) - len(self._received)
            snapshot = self._snapshot()
        self._persist(snapshot)
        return removed

    def mark_read(self, msg_id: str, owner: str = "") -> None:
        with self._lock:
            for m in self._received:
                if m.get("id") == msg_id:
                    if owner and m.get("owner") != owner:
                        break
                    m["read"] = True
            snapshot = self._snapshot()
        self._persist(snapshot)

    def _snapshot(self) -> dict:
        return {"sent": list(self._sent), "received": list(self._received)}

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._sent     = data.get("sent", [])
            self._received = data.get("received", [])
            log.info("Loaded %d sent, %d received messages",
                     len(self._sent), len(self._received))
        except Exception as exc:
            log.warning("Could not load messages: %s", exc)

    def _persist(self, snapshot: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            log.warning("Could not save messages: %s", exc)

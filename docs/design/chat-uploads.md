# Chat file / image / audio upload — design

Draft for review before implementation. Covers scope, wire formats, storage strategy, size limits, UI, and rollout order.

Author: 2026-08-09 session. Sits under `docs/design/` alongside other pre-implementation specs.

---

## Goal

Let a NomadPortal user attach a file, image, or audio clip to an outbound LXMF message and see any of those attachments render appropriately in inbound messages. Match MeshChat's on-the-wire format so we interop with the existing ecosystem.

**Non-goals for the first cut:**
- Group / multi-recipient upload — LXMF has `FIELD_GROUP (0x0b)` but this app has no group primitive yet
- Editing / redacting sent attachments — LXMF is send-only, once it's out it's out
- Server-side re-encoding / thumbnail generation — clients render the bytes as-received
- Video attachments — LXMF has no `FIELD_VIDEO`, and multi-MB video over Reticulum links isn't reasonable anyway

---

## Wire format — matches MeshChat exactly

Confirmed against MeshChat's `meshchat.py` and the LXMF library's `FIELD_*` constants (2026-08-09 read):

```python
# Outbound send path — assign fields to lxmf_message.fields dict
lxmf_message.fields[LXMF.FIELD_IMAGE]            = [image_type_str, image_bytes]
lxmf_message.fields[LXMF.FIELD_AUDIO]            = [audio_mode_str, audio_bytes]
lxmf_message.fields[LXMF.FIELD_FILE_ATTACHMENTS] = [[file_name, file_bytes], [file_name, file_bytes], ...]
```

Field IDs (from `LXMF.FIELD_*`):
- `FIELD_IMAGE            = 0x06`  — single image
- `FIELD_AUDIO            = 0x07`  — single audio clip
- `FIELD_FILE_ATTACHMENTS = 0x05`  — array of arbitrary files

We already parse `FIELD_IMAGE` on receive (contact-icon path in `messaging.py`); the send-side is new, and `FIELD_AUDIO` + `FIELD_FILE_ATTACHMENTS` are new both directions.

`audio_mode_str` is a codec/container identifier that MeshChat uses to route the bytes to the right decoder (e.g. `"opus"`, `"mp3"`). Reasonable default for our first cut: whatever MIME/codec the browser hands us from `<input type="file">`, mapped to a short string. For a general-purpose file that happens to be audio, we can send it as `FIELD_FILE_ATTACHMENTS` and let the recipient's UI decide whether to render as `<audio>` or a download link based on MIME sniffing.

---

## Size limits

**Hard limit: 500 KB per attachment.** LXMF messages are transferred as RNS Resources under the hood; there's no hard protocol cap on message size, but practical mesh transfer times for multi-MB payloads over multi-hop links are painful — a 5 MB file over 3 hops at ~2 kB/s is 40+ minutes of wait, during which the link can drop.

Rationale for 500 KB specifically:
- Comfortably above what a well-compressed JPEG at message-attachment quality needs (~50-200 KB)
- Fits an ~30 s Opus voice clip at 16 kbps (~60 KB)
- Below the tipping point where transfer failure becomes likely
- Round number that's easy to communicate to users

**Configurable:** `LXMF_ATTACHMENT_MAX_BYTES` env var, defaults to `524288`. Admin UI toggle can expose it later.

**UI enforcement:** browser-side check on the `<input>` File's `.size` before starting the send, plus server-side re-check for defense in depth. Reject with a clear "file too large — cap is 500 KB" toast; don't silently truncate.

**Total-message cap:** `FIELD_FILE_ATTACHMENTS` is an array — we should also cap the array's total combined bytes at 500 KB (not per-file, since 20 × 100 KB is worse than 1 × 500 KB for transfer time). Configurable via the same env var.

---

## Storage strategy

**Approach: JSON store keeps a reference; blob store keeps the bytes.**

Rationale: our existing `messages.json` is loaded whole into memory on startup. A single 500 KB attachment inflates the JSON by ~700 KB (base64 overhead) — 100 messages with attachments = 70 MB of JSON to parse every startup. That's the same NAS/GIL pathology we spent v0.9.x fixing on the peer tracker and node registry.

**Concrete plan:**

- **Blob store:** `config/attachments/<msg_id>/<attachment_idx>.<ext>` — files-on-disk, one file per attachment, keyed by message ID. Written on receive; read lazily when the UI requests them.
- **Message entry:** stays in `messages.json`, gains an `attachments` array with metadata only:
  ```json
  {
    "id": "abc123...",
    "source": "...",
    "content": "...",
    "attachments": [
      {"kind": "image", "filename": "photo.jpg", "mime": "image/jpeg", "size": 148231, "path": "config/attachments/abc123/0.jpg"},
      {"kind": "audio", "filename": "voice.opus", "mime": "audio/opus", "size": 61240, "path": "config/attachments/abc123/1.opus"}
    ]
  }
  ```
- **Serve on demand:** new endpoint `GET /api/messages/<msg_id>/attachments/<idx>` streams the blob with appropriate Content-Type. Auth-gated to the message's owner.
- **Eviction:** delete `config/attachments/<msg_id>/` when the message itself is deleted (message-store deletion is the existing lifecycle).

**Alternative considered:** stream directly from LXMF's own storage, no separate copy. Rejected — LXMF library's storage layout isn't a stable API and could break on upstream refactor. Owning our own blob directory is cheap insurance.

---

## Receive path

1. `_on_delivery` in `messaging.py` already extracts `FIELD_IMAGE` for contact icons. Extend to extract `FIELD_IMAGE` (as regular content, not just as an icon), `FIELD_AUDIO`, and `FIELD_FILE_ATTACHMENTS`.
2. For each attachment, write bytes to `config/attachments/<msg_id>/<idx>.<ext>` and record metadata in the message entry.
3. Existing contact-icon extraction stays as-is — that's a separate use of `FIELD_IMAGE` where the icon is a per-contact avatar, not a message attachment. The icon path checks whether the message is otherwise empty; if there's text content, we treat `FIELD_IMAGE` as an attachment, not an avatar update. **Confirm with a real MeshChat send** — this is my read of MeshChat's convention, but worth double-checking their send-side logic.

---

## Send path

1. New endpoint `POST /api/messages` accepts `multipart/form-data` when attachments are present. Existing JSON body path still works when no attachments.
2. Backend validates size caps, writes blobs to a temporary staging area (not the final `config/attachments/` — that's for received messages), then assembles the LXMF message with the appropriate `FIELD_*` fields set.
3. On successful `lxmf_router.handle_outbound(msg)`, copy the staging files into `config/attachments/<outbound_msg_id>/` so the sent message's attachments are viewable in the "sent" tab.
4. Clean up staging on error.

**MIME → LXMF field routing:**

| Client-side MIME       | LXMF field                | Notes                                    |
|------------------------|---------------------------|------------------------------------------|
| `image/*`              | `FIELD_IMAGE`             | Type token = extension (`jpg`/`png`/…)   |
| `audio/*`              | `FIELD_AUDIO`             | `audio_mode` = codec token (`opus`/`mp3`)|
| anything else          | `FIELD_FILE_ATTACHMENTS`  | Preserves original filename              |

If the user attaches multiple items: single image → `FIELD_IMAGE`, single audio → `FIELD_AUDIO`, everything else + any "extras" beyond one image/audio → `FIELD_FILE_ATTACHMENTS`. If they attach two images, one goes to `FIELD_IMAGE` and the second goes into `FIELD_FILE_ATTACHMENTS` as a file. This matches MeshChat's structure — image/audio are singletons, files is an array.

---

## UI

**Compose bar:**
- Add a `📎` (paperclip) icon-button next to the compose input.
- Click opens `<input type="file" multiple accept="image/*,audio/*,*/*">` — the browser's native file picker.
- Selected files show as chips above the compose input: `[📷 photo.jpg — 148 KB × ]`. Clicking × removes.
- Total size counter next to the chips: `2 files, 210 KB / 500 KB` — turns red when the cap is exceeded.
- Send button disabled while any chip is over-cap; enabled when total is under.

**Received messages:**
- **Image attachment:** rendered inline in the bubble at ≤300 px wide (CSS `max-width`), tap to view full-size.
- **Audio attachment:** rendered as a native `<audio controls>` element — the browser handles play/pause/seek/volume for us for free. Add a small filename label above.
- **File attachment:** rendered as a download link with filename + size + MIME icon. Click downloads via the existing `/api/messages/<id>/attachments/<idx>` endpoint.

**Sent messages:** same rendering as received, plus a "Delivered / Failed" state indicator (existing behaviour).

---

## Security notes

- **No virus scanning** on chat attachments in the first cut. Rationale: chat is user↔user with an existing consent model (the user chose to receive from this sender). Files fetched from external NomadNet nodes still go through the scanner because that's an unknown-source content pull.
- **Content-Disposition** on the download endpoint uses `attachment; filename="<sanitised>"` — same RFC 5987 encoding the existing `/api/file/download` endpoint uses, so non-ASCII filenames survive.
- **Path traversal:** the `<idx>` in the URL is a number, not a filename. Server maps `idx` to the on-disk file via the message's stored metadata; nothing user-controlled reaches the filesystem path.
- **Blob-store permissions:** created with `0700` owner-only mode (matches `config/reticulum/` conventions).

---

## Rollout order

Small enough steps that each stands alone and can be tested:

1. **Blob-store scaffolding** — the `config/attachments/` directory, the write/read helpers, the eviction hook wired into the existing message-delete flow. No UI changes. Verify via unit tests. **✅ shipped 2026-08-08 (commit `76fafa5`)** — 22 pytest cases in `tests/test_attachment_store.py`.
2. **Receive path — images inline** — extend `_on_delivery` to persist `FIELD_IMAGE` attachments and render in the chat bubble. Test by having MeshChat send us an image. Contact-icon path stays intact. **✅ shipped 2026-08-08 (commit `0b97bf0`)** — 7 pytest cases in `tests/test_receive_attachments.py`; new endpoint `GET /api/messages/<msg_id>/attachments/<idx>`.
3. **Receive path — files + audio** — same as (2) but for `FIELD_FILE_ATTACHMENTS` and `FIELD_AUDIO`. Test by MeshChat sending each type. **✅ shipped 2026-08-08 (commit `63bd254`)** — 9 more pytest cases (16 total in the file); audio-mode → MIME lookup with fallback.
4. **Send path** — the `📎` UI, size caps, `multipart/form-data` endpoint, LXMF field assembly. Test by sending to MeshChat and confirming their side renders correctly. **✅ shipped 2026-08-09 (commit `7e30ba3`)** — 16 pytest cases in `tests/test_send_attachments.py`; `POST /api/messages` dual-pathed on Content-Type; MIME → field routing (image/audio singleton, extras + generics → FIELD_FILE_ATTACHMENTS); env var `LXMF_ATTACHMENT_MAX_BYTES` override.
5. **Docs + release notes** — README feature bullet, CHANGELOG. Site examples-page demo skipped (page is for Micron markup features, not chat UI). **✅ shipped 2026-08-09** — README bullet + CHANGELOG entries for steps 1-4 under `[Unreleased]`; awaiting `[Unreleased] → [1.3.0]` flip at release time.

Each step is a separate commit; the full batch ships as `v1.3.0` (minor bump — new user-facing feature). Total scope: 4 focused sessions in the end (steps 1+2 in one session, then one session per remaining step).

**MeshChat interop verification is still TODO** — every step above landed under the assumption that our field extraction / assembly matches MeshChat's wire format, but end-to-end round-trip testing with a real MeshChat instance hasn't happened yet. If mismatch surfaces, the fix is localized to `_MIME_TO_IMAGE_EXT` / `_MIME_TO_AUDIO_MODE` in `messaging.py`.

---

## Operator decisions — locked 2026-08-09

All four open questions from the initial draft resolved:

1. **Size cap: 500 KB** per attachment AND per message total. Configurable via `LXMF_ATTACHMENT_MAX_BYTES` env var (default `524288`).
2. **Audio codec: pass-through** — send whatever the browser's `MediaRecorder` produces (webm/mp4-containered audio). Zero server codec dependencies. Recipient's browser plays via `<audio controls>`. If bandwidth becomes an observed issue later, add server-side Opus transcode as a follow-up.
3. **Sent-message attachment retention: forever** — matches current text-message lifecycle. No eviction. Reconsider if disk usage becomes an issue in practice (admin toggle can be added later).
4. **Contact-icon vs message-attachment split: proceed on assumption** — my read of MeshChat is that `FIELD_IMAGE` on an announce is a contact icon, `FIELD_IMAGE` on a chat message with text content is an attachment. If wrong, symptom will be obvious during receive-side testing (icon flipping on inbound messages, or attachments not rendering) and the fix is localized to `_on_delivery` in `messaging.py`. Not worth blocking build on a verification round-trip.

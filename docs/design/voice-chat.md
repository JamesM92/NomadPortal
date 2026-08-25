# Voice chat over Reticulum — implementation plan

Draft for review — this is a scoping document, not a build order. Written to let the operator decide whether the effort's worth the value, and if so, which of the two architectural approaches to take.

Author: 2026-08-09 session. Sister to `chat-uploads.md`.

---

## Feasibility answer

**Yes, technically feasible.** MeshChat ships a working implementation (`src/backend/audio_call_manager.py`, ~235 lines) that uses:

- **`call.audio` aspect Destination** — registered on the identity at startup, announced periodically so peers can discover it via `RNS.Transport.recall`.
- **`RNS.Link`** — established from caller's identity to callee's `call.audio` Destination. Once ACTIVE, both sides use `link.send(audio_bytes)` and `link.set_packet_callback(...)` to shuttle packets bidirectionally.
- **Opus codec** — MeshChat encodes audio to Opus frames client-side and sends the raw bytes; RNS transports them opaquely. Small frame size (~20 ms of audio) keeps latency low.

Nothing in the mesh protocol blocks us doing the same. The hard part is bridging the RNS-Python-server side to a browser client that has to capture and play back audio.

---

## Two architectural approaches

### Approach A — server-side codec (heavier server, thinner browser)

```
[mic capture]  browser  →  ws  →  Python  →  opus encode  →  RNS.Link.send
                                                                  ↓
[playback]     browser  ←  ws  ←  Python  ←  opus decode  ←  packet_callback
```

- Browser sends raw PCM (or MediaRecorder chunks) over a persistent websocket
- Server encodes/decodes with `opuslib` (Python binding) or spawning `ffmpeg`
- Server manages the RNS.Link, forwards packets both directions

**Pros:**
- Browser doesn't need Opus support (just `getUserMedia` + WebAudio)
- Same behavior on every browser — server owns the codec
- Matches MeshChat's protocol exactly on the wire (Opus frames as RNS Link payload)
- Testing / logging server-side is straightforward

**Cons:**
- New Python runtime dependency (`opuslib` — small C binding; or `ffmpeg-python` — much heavier)
- All audio traffic hits the server twice (once from browser, once to RNS) — modest CPU/bandwidth
- Larger container image if we go the ffmpeg route

### Approach B — browser-side codec (thinner server, heavier browser)

```
[mic capture]  browser  →  opus encode  →  ws  →  Python  →  RNS.Link.send
                                                                  ↓
[playback]     browser  ←  opus decode  ←  ws  ←  Python  ←  packet_callback
```

- Browser handles Opus encode/decode via WebAssembly (e.g., `opus-encoder`, `libopus.wasm`)
- Server is a dumb passthrough — packets in, packets out
- Reduces server CPU load; audio bytes only hit RAM once server-side

**Pros:**
- Server stays lightweight (no codec dependency, no ffmpeg)
- Lower audio latency (fewer hops)
- Encodes at capture, decodes at playback — RNS bytes are already the "wire format"

**Cons:**
- WebAssembly Opus libraries are 300 KB+ downloads (one-time per session)
- Browser compatibility variance (Safari especially finicky about audio worklets)
- Debugging codec issues is browser-side which is harder
- MediaRecorder can't directly produce raw Opus frames — you get webm-containered Opus, needing extra parse work OR a proper WASM encoder

**Recommendation: Approach A**, on grounds that:
- Codec correctness / debugging is easier server-side
- The Python runtime dep isn't huge (`opuslib` is ~50 KB compiled)
- MeshChat parity — their format is Opus-frames-over-Link, and we can match exactly
- Server CPU cost is modest — a single call is ~24-64 kbit/s encoded which is nothing

Rest of this doc assumes Approach A.

---

## Component breakdown (Approach A)

### 1. Server-side call manager (~4-6 hours)

Port MeshChat's `audio_call_manager.py` structure into `nomadnet_web/audio_calls.py`:

- `AudioCallManager` — owns the `call.audio` Destination on our identity, announces periodically, listens for incoming `call.audio` Links via `set_link_established_callback` on the local Destination
- `AudioCall` — one instance per active call, wraps the RNS.Link, exposes:
  - `send_audio_packet(bytes)` — encoded Opus frame → link.send
  - `set_audio_packet_listener(fn)` — decoded PCM → callback
  - `hangup()` — link.teardown
  - state: PENDING / ACTIVE / ENDED

Similar to how `PropagationSyncService` is a separate module; wire into `create_app` after RNS is up.

### 2. Opus encode/decode helpers (~2-4 hours)

Wrap `opuslib` (PyPI) in a small module `nomadnet_web/audio_codec.py`:

- Fixed encoder/decoder config: 48 kHz, mono, 20 ms frames, VOIP application preset, 24 kbps bitrate (bitrate configurable)
- `encode_pcm_to_opus(pcm_bytes) → opus_frame_bytes` — takes PCM samples, returns one Opus frame
- `decode_opus_to_pcm(opus_frame_bytes) → pcm_bytes` — reverse

The PCM shape matches what `getUserMedia` delivers when wired to an `AudioWorklet` → PCM 16-bit at 48 kHz mono.

Add `opuslib` to `requirements.txt`. Base image needs `libopus0` — small apt-get add in the Dockerfile.

### 3. WebSocket signalling + audio channel (~4-6 hours)

New endpoint `GET /ws/calls` (Flask-Sock or similar). Not authenticated the same way REST endpoints are — session cookie carries the user identity.

Message shapes (JSON control frames, binary audio frames):

```
→  {"type": "ring", "peer_hash": "..."}     start a call to peer
→  {"type": "accept"}                       accept incoming ring
→  {"type": "hangup"}                       terminate call
→  <binary audio bytes>                     20-ms Opus frame (call active)
←  {"type": "state", "state": "ringing|active|ended", "peer_hash": "..."}
←  {"type": "incoming", "peer_hash": "..."}
←  <binary audio bytes>                     20-ms Opus frame
```

Server routes binary frames to the AudioCall's `send_audio_packet`; received packets go the other way through the websocket.

**Backpressure:** if the mesh path is slow and the RNS.Link's send queue backs up, drop frames (audio) rather than buffer indefinitely — voice with 200 ms of gap is better than voice with 3 s of buffered audio catching up.

### 4. Browser client (~6-10 hours)

New JS module `static/js/calls.js`:

- **Capture:** `navigator.mediaDevices.getUserMedia({audio: true})` → `AudioContext` → `AudioWorkletNode` → 20-ms PCM chunks → websocket.send(pcm_bytes) — server encodes and forwards
- **Playback:** websocket binary frame in → server has already decoded to PCM → `AudioBufferSourceNode` scheduled at the next-in-line playback position → speakers
- **UI:**
  - Call button in each chat contact — "📞 Call"
  - Incoming-call overlay: contact name, Accept / Reject buttons
  - Active-call bar: mute toggle, hangup button, timer, RSSI/SNR when known
  - No video (out of scope), no group calls (LXMF has FIELD_GROUP but audio-call semantics for groups aren't in the ecosystem)

The AudioWorklet path is the tricky part on mobile — Safari support is inconsistent. Fall back to ScriptProcessorNode where AudioWorklet isn't available (deprecated but universally supported).

### 5. Signalling UX (~2-4 hours)

- Ringtone (JS-generated tone, no audio file dep — a 440 Hz sine burst is fine)
- Vibrate on incoming call for mobile (Vibration API)
- Hangup on tab close / navigation
- Auto-decline after N seconds of ringing with no answer
- Reject rules — per your `feedback_reliability_bugs_are_ours` memory, no per-caller blocklist yet; single global "call allow" setting in admin

### 6. Tests (~2-3 hours)

- Unit tests for the codec wrapper (encode → decode → check PCM matches within tolerance)
- Unit tests for AudioCall state machine (mock RNS.Link)
- Integration: run two containers, one calls the other, check both sides get audio through — can't fully test in CI without a mesh but at least smoke-test locally

### 7. Docs (~1-2 hours)

- README: call feature bullet
- CHANGELOG: full entry
- Admin settings page: "Enable audio calls" toggle (default on if `opuslib` imports OK, else disabled with a warning)

---

## Total effort estimate

**20-30 focused hours.** Roughly:

| Phase | Hours (low-high) |
|-------|------------------|
| Server-side call manager     |  4-6  |
| Opus codec wrapper           |  2-4  |
| Websocket audio channel      |  4-6  |
| Browser client               |  6-10 |
| Signalling UX                |  2-4  |
| Tests                        |  2-3  |
| Docs + release               |  1-2  |
| **Total**                    | **21-35** |

Realistic calendar time given one focused session per day: **1-2 weeks**.

---

## Risks + open questions

1. **`opuslib` on Python 3.14** — the container's on 3.14; opuslib's CI may lag. Need to verify install works before committing to the plan. Fallback: pin `python-opus` or vendor a minimal Opus binding.
2. **Mobile audio latency** — WebAudio + AudioWorklet on Chrome Android is ~30-50 ms latency, plus mesh RTT. On multi-hop Reticulum paths this can add up to a full second one-way. Voice usability degrades badly past ~400 ms one-way. Realistic expectation: 1-hop mesh (both peers on the same hub) will feel like a normal VoIP call; 3+ hops will feel like a satellite call with visible pauses.
3. **Backpressure choices** — is dropping frames the right call, or should we downshift bitrate under packet loss? Simpler: drop frames. Better UX under bad conditions: dynamic bitrate. Recommend simpler for v1.
4. **Concurrent-call support** — one call at a time per identity is fine for v1. Multiple simultaneous calls are theoretically possible (multiple concurrent RNS.Links to different peers) but adds UI complexity.
5. **Recording / voicemail** — MeshChat doesn't have this. Skip for v1.
6. **Announce cadence** — the `call.audio` Destination needs to be announced periodically so peers can discover us to call. Piggyback on the existing announce schedule or make its own timer? Same-frequency piggyback is simpler; do that.

---

## Value assessment

**Who benefits?** Operators running NomadPortal on a mesh where voice would be genuinely useful — typically: distributed community coordination, off-grid group operations, situations where LXMF text is too slow but SMS/regular VoIP isn't available.

**Ecosystem parity signal:** MeshChat has it; Sideband has it. Adding voice to NomadPortal closes a specific "MeshChat has this and you don't" gap.

**Realistic usage:** low, for most operators. Most Reticulum use is text; voice is a nice-to-have for a specific class of user.

**Recommendation:** **medium-priority.** Worth doing eventually for feature parity, but not urgent. Would sequence AFTER the chat-uploads feature (higher-frequency usage, lower risk, smaller scope). If operator wants to build one first, upload > voice.

---

## Deferred / not-in-scope

- **Group calls** — LXMF has `FIELD_GROUP (0x0b)` for group messaging but audio-call group semantics aren't in the ecosystem. Skip.
- **Video** — LXMF has no video field, and the bandwidth is unreasonable for typical Reticulum links. Skip.
- **Call recording** — MeshChat doesn't do it. Skip unless explicitly asked.
- **Screen-share** — nope.
- **Encryption beyond RNS's own** — RNS.Link is already end-to-end encrypted with a per-link ephemeral key. Additional layer would be gilt.

---

## Recommendation to operator

If you want voice, greenlight this plan (Approach A, ~1-2 weeks). If you're on the fence, ship chat-uploads first — it's smaller scope with broader utility, and lets you get a feel for the file/audio-attachment path before jumping into real-time audio.

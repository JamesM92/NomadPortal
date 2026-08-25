// ---------------------------------------------------------------------------
// Voice-call audio (Phase 1b) — WebCodecs Opus encode/decode over the
// same opaque frame relay CallManager.send_audio_frame()/
// pop_audio_frame() already provides for Phase 1a signalling
// (/api/calls/audio/send|recv). Chromium-only in practice today — no
// AudioEncoder/AudioDecoder in Firefox/Safari as of this writing — so
// this is entirely feature-detected: signalling (dial/ring/answer/
// hang-up) keeps working everywhere regardless of whether this file
// can do anything once a call actually connects.
//
// Wire shape matches call_manager.py's own module doc comment exactly:
// 48kHz mono, 20ms frames (960 samples), Opus @ 24kbps, a 1-byte codec
// header (0x01 = Opus) in front of each already-encoded frame — the
// same numbers the NomadPortal-Android sister project's own Kotlin
// CallAudioEngine uses, chosen there for real interop with LXST's own
// reference clients over a real mesh link.
//
// Depends on app.js already being loaded (uses its $, apiFetch, esc,
// setStatus globals — both are plain, non-module <script>s sharing one
// global scope, so load order between the two doesn't matter for
// correctness: neither one's audio functions are actually *called*
// until a real call reaches "established", well after both scripts
// have finished executing their own top-level code).
// ---------------------------------------------------------------------------

const CALL_AUDIO_SAMPLE_RATE  = 48000;
const CALL_AUDIO_FRAME_SAMPLES = 960;  // 20ms at 48kHz
const CALL_AUDIO_BITRATE = 24000;
const CALL_AUDIO_CODEC_HEADER_OPUS = 0x01; // matches call_manager.py's own wire format doc comment
const CALL_AUDIO_RECV_POLL_MS = 100;       // well under the 200ms server-side jitter buffer

function callAudioSupported() {
  return typeof AudioEncoder !== 'undefined' &&
         typeof AudioDecoder !== 'undefined' &&
         typeof AudioData !== 'undefined' &&
         typeof EncodedAudioChunk !== 'undefined';
}

let _callAudioActive       = false;
let _callAudioCtx          = null;
let _callAudioMicStream    = null;
let _callAudioWorkletNode  = null;
let _callAudioEncoder      = null;
let _callAudioDecoder      = null;
let _callAudioRecvTimer    = null;
let _callAudioNextPlayTime = 0;
let _callAudioMuted        = false;
let _callAudioEncodeTs     = 0;  // microseconds, monotonically increasing per encoded frame
let _callAudioDecodeTs     = 0;  // microseconds, monotonically increasing per decoded frame

async function startCallAudio() {
  if (_callAudioActive) return; // idempotent -- callers don't need to track our own state
  if (!callAudioSupported()) {
    setStatus('This browser can’t play call audio (needs a Chromium browser) — signalling only.', 'error');
    return;
  }
  _callAudioActive = true;
  _callAudioEncodeTs = 0;
  _callAudioDecodeTs = 0;

  try {
    _callAudioCtx = new AudioContext({ sampleRate: CALL_AUDIO_SAMPLE_RATE });
    await _callAudioCtx.audioWorklet.addModule('/static/js/call-audio-worklet.js');

    // ---- Capture: mic -> 20ms PCM frames (worklet) -> Opus encode -> send ----
    _callAudioMicStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    if (!_callAudioActive) { _teardownCallAudioStream(); return; } // call ended while awaiting mic permission

    const source = _callAudioCtx.createMediaStreamSource(_callAudioMicStream);
    _callAudioWorkletNode = new AudioWorkletNode(_callAudioCtx, 'call-capture-processor');
    source.connect(_callAudioWorkletNode);

    _callAudioEncoder = new AudioEncoder({
      output: chunk => {
        const payload = new Uint8Array(chunk.byteLength);
        chunk.copyTo(payload);
        const frame = new Uint8Array(payload.length + 1);
        frame[0] = CALL_AUDIO_CODEC_HEADER_OPUS;
        frame.set(payload, 1);
        _sendCallAudioFrame(frame);
      },
      error: err => console.warn('Call audio encoder error:', err),
    });
    _callAudioEncoder.configure({
      codec: 'opus',
      sampleRate: CALL_AUDIO_SAMPLE_RATE,
      numberOfChannels: 1,
      bitrate: CALL_AUDIO_BITRATE,
    });

    _callAudioWorkletNode.port.onmessage = ev => {
      if (!_callAudioActive || _callAudioMuted || !_callAudioEncoder) return;
      const samples = ev.data; // Float32Array(960), one 20ms frame
      const audioData = new AudioData({
        format: 'f32-planar',
        sampleRate: CALL_AUDIO_SAMPLE_RATE,
        numberOfFrames: samples.length,
        numberOfChannels: 1,
        timestamp: _callAudioEncodeTs,
        data: samples,
      });
      _callAudioEncodeTs += (samples.length / CALL_AUDIO_SAMPLE_RATE) * 1e6;
      try {
        _callAudioEncoder.encode(audioData);
      } catch (err) {
        console.warn('Call audio encode() failed:', err);
      } finally {
        audioData.close();
      }
    };

    // ---- Playback: recv (poll) -> Opus decode -> scheduled speaker output ----
    _callAudioNextPlayTime = _callAudioCtx.currentTime + 0.1; // small head start absorbs the first poll's latency
    _callAudioDecoder = new AudioDecoder({
      output: audioData => _playCallAudioData(audioData),
      error: err => console.warn('Call audio decoder error:', err),
    });
    _callAudioDecoder.configure({
      codec: 'opus',
      sampleRate: CALL_AUDIO_SAMPLE_RATE,
      numberOfChannels: 1,
    });

    _callAudioRecvTimer = setInterval(_pollCallAudioRecv, CALL_AUDIO_RECV_POLL_MS);
  } catch (err) {
    setStatus(`Could not start call audio: ${err.message}`, 'error');
    stopCallAudio();
  }
}

async function _sendCallAudioFrame(frame) {
  let binary = '';
  frame.forEach(b => { binary += String.fromCharCode(b); });
  try {
    await apiFetch('/api/calls/audio/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ frame_b64: btoa(binary) }),
    });
  } catch (_) {
    // A dropped frame is a normal, expected outcome for live audio --
    // the next one 20ms later carries on. Never surfaced to the user.
  }
}

async function _pollCallAudioRecv() {
  if (!_callAudioActive || !_callAudioDecoder) return;
  try {
    const d = await apiFetch('/api/calls/audio/recv');
    for (const frameB64 of (d.frames || [])) {
      const bytes = Uint8Array.from(atob(frameB64), c => c.charCodeAt(0));
      if (bytes.length < 2 || bytes[0] !== CALL_AUDIO_CODEC_HEADER_OPUS) continue; // not Opus -- skip
      const payload = bytes.slice(1);
      const chunk = new EncodedAudioChunk({
        type: 'key', // Opus has no inter-frame dependency the way video keyframes do -- every frame decodes standalone
        timestamp: _callAudioDecodeTs,
        data: payload,
      });
      _callAudioDecodeTs += (CALL_AUDIO_FRAME_SAMPLES / CALL_AUDIO_SAMPLE_RATE) * 1e6;
      try {
        _callAudioDecoder.decode(chunk);
      } catch (err) {
        console.warn('Call audio decode() failed:', err); // a bad/corrupt frame is just dropped
      }
    }
  } catch (_) {
    // transient -- next tick tries again
  }
}

function _playCallAudioData(audioData) {
  if (!_callAudioCtx) { audioData.close(); return; }
  const numFrames = audioData.numberOfFrames;
  const channelData = new Float32Array(numFrames);
  audioData.copyTo(channelData, { planeIndex: 0, format: 'f32-planar' });
  audioData.close();

  const buffer = _callAudioCtx.createBuffer(1, numFrames, CALL_AUDIO_SAMPLE_RATE);
  buffer.copyToChannel(channelData, 0);

  const source = _callAudioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(_callAudioCtx.destination);

  const now = _callAudioCtx.currentTime;
  if (_callAudioNextPlayTime < now) {
    // Fell behind (a real gap/underrun, e.g. a slow mesh link) --
    // restart the schedule a small buffer ahead rather than trying to
    // catch up by playing every buffered frame back-to-back with no
    // gaps, which would just sound sped-up.
    _callAudioNextPlayTime = now + 0.05;
  }
  source.start(_callAudioNextPlayTime);
  _callAudioNextPlayTime += numFrames / CALL_AUDIO_SAMPLE_RATE;
}

function setCallAudioMuted(muted) {
  _callAudioMuted = !!muted;
  if (_callAudioMicStream) {
    _callAudioMicStream.getAudioTracks().forEach(t => { t.enabled = !_callAudioMuted; });
  }
}

function isCallAudioMuted() {
  return _callAudioMuted;
}

function _teardownCallAudioStream() {
  if (_callAudioMicStream) {
    _callAudioMicStream.getTracks().forEach(t => t.stop());
    _callAudioMicStream = null;
  }
}

function stopCallAudio() {
  _callAudioActive = false;
  if (_callAudioRecvTimer) { clearInterval(_callAudioRecvTimer); _callAudioRecvTimer = null; }
  _teardownCallAudioStream();
  if (_callAudioWorkletNode) {
    _callAudioWorkletNode.port.onmessage = null;
    try { _callAudioWorkletNode.disconnect(); } catch (_) {}
    _callAudioWorkletNode = null;
  }
  if (_callAudioEncoder) {
    if (_callAudioEncoder.state !== 'closed') { try { _callAudioEncoder.close(); } catch (_) {} }
    _callAudioEncoder = null;
  }
  if (_callAudioDecoder) {
    if (_callAudioDecoder.state !== 'closed') { try { _callAudioDecoder.close(); } catch (_) {} }
    _callAudioDecoder = null;
  }
  if (_callAudioCtx) {
    try { _callAudioCtx.close(); } catch (_) {}
    _callAudioCtx = null;
  }
  _callAudioMuted = false;
}

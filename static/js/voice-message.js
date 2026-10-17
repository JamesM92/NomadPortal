// ---------------------------------------------------------------------------
// Voice-message recorder. Records the microphone, encodes it as Opus with the
// browser's WebCodecs AudioEncoder (the same settings voice calls use), and
// wraps the packets in an Ogg container (ogg-opus.js). The result is the
// stream LXMF calls AM_OPUS_OGG, which MeshChat plays inline.
//
// Feature-detected: VoiceMessageRecorder.supported() is false in browsers
// without AudioEncoder, and the record button simply does not appear there.
// Attaching an audio file still works everywhere.
//
// Depends on ogg-opus.js (OggOpusWriter) and reuses call-audio-worklet.js,
// which hands over 20 ms frames of microphone audio. Plain <script>, no
// module system, so it shares one global scope with app.js.
// ---------------------------------------------------------------------------

const VOICE_SAMPLE_RATE = 48000;
const VOICE_FRAME_SAMPLES = 960;      // 20 ms at 48 kHz
const VOICE_BITRATE = 24000;          // the same as voice calls: clear speech, about 3 KB a second
const VOICE_BYTES_PER_SECOND = 3600;  // a little over the real rate (24 kbit/s plus Ogg framing)

class VoiceMessageRecorder {
  static supported() {
    return typeof AudioEncoder !== 'undefined' &&
           typeof AudioData !== 'undefined' &&
           typeof EncodedAudioChunk !== 'undefined' &&
           typeof AudioWorkletNode !== 'undefined' &&
           typeof OggOpusWriter !== 'undefined' &&
           !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  // opts.maxSeconds: stop by itself at this length (opts.onLimit is called)
  // opts.onTick(seconds): about four times a second while recording
  constructor(opts) {
    const o = opts || {};
    this.maxSeconds = o.maxSeconds || 120;
    this.onTick = o.onTick || null;
    this.onLimit = o.onLimit || null;
    this._ctx = null;
    this._stream = null;
    this._node = null;
    this._encoder = null;
    this._writer = null;
    this._timer = null;
    this._samples = 0;        // 48 kHz samples captured so far
    this._ts = 0;             // encoder timestamp, microseconds
    this._recording = false;
    this._hitLimit = false;
  }

  get seconds() { return this._samples / VOICE_SAMPLE_RATE; }
  get recording() { return this._recording; }

  async start() {
    if (this._recording) return;
    this._writer = new OggOpusWriter({ channels: 1, inputSampleRate: VOICE_SAMPLE_RATE });
    this._samples = 0;
    this._ts = 0;
    this._hitLimit = false;
    try {
      this._ctx = new AudioContext({ sampleRate: VOICE_SAMPLE_RATE });
      await this._ctx.audioWorklet.addModule('/static/js/call-audio-worklet.js');
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });

      this._encoder = new AudioEncoder({
        output: chunk => {
          const packet = new Uint8Array(chunk.byteLength);
          chunk.copyTo(packet);
          const dur = chunk.duration ? Math.round(chunk.duration * VOICE_SAMPLE_RATE / 1e6) : VOICE_FRAME_SAMPLES;
          this._writer.addPacket(packet, dur || VOICE_FRAME_SAMPLES);
        },
        error: err => console.warn('Voice message encoder error:', err),
      });
      this._encoder.configure({
        codec: 'opus',
        sampleRate: VOICE_SAMPLE_RATE,
        numberOfChannels: 1,
        bitrate: VOICE_BITRATE,
      });

      this._node = new AudioWorkletNode(this._ctx, 'call-capture-processor');
      this._ctx.createMediaStreamSource(this._stream).connect(this._node);
      this._node.port.onmessage = ev => this._onFrame(ev.data);
      this._recording = true;
      this._timer = setInterval(() => this._tick(), 250);
    } catch (err) {
      this._teardown();
      throw err;
    }
  }

  _onFrame(samples) {
    if (!this._recording || !this._encoder || this._encoder.state !== 'configured') return;
    const data = new AudioData({
      format: 'f32-planar',
      sampleRate: VOICE_SAMPLE_RATE,
      numberOfFrames: samples.length,
      numberOfChannels: 1,
      timestamp: this._ts,
      data: samples,
    });
    this._ts += (samples.length / VOICE_SAMPLE_RATE) * 1e6;
    this._samples += samples.length;
    try {
      this._encoder.encode(data);
    } catch (err) {
      console.warn('Voice message encode() failed:', err);
    } finally {
      data.close();
    }
  }

  _tick() {
    if (this.onTick) this.onTick(this.seconds);
    if (this.seconds >= this.maxSeconds && !this._hitLimit) {
      this._hitLimit = true;
      if (this.onLimit) this.onLimit();
    }
  }

  // Finish and return {bytes, seconds}. Safe to call once.
  async stop() {
    if (!this._recording) throw new Error('Not recording');
    this._recording = false;
    clearInterval(this._timer);
    this._timer = null;
    if (this._node) this._node.port.onmessage = null;      // no more frames in
    try {
      if (this._encoder && this._encoder.state === 'configured') await this._encoder.flush();   // every packet out
    } finally {
      this._teardown();
    }
    const seconds = this.seconds;
    return { bytes: this._writer.finish(), seconds };
  }

  // Throw the recording away.
  cancel() {
    this._recording = false;
    this._teardown();
  }

  // Always lets go of the microphone, whatever state we are in. A leaked
  // microphone stream keeps the browser's recording indicator lit.
  _teardown() {
    clearInterval(this._timer);
    this._timer = null;
    if (this._node) {
      this._node.port.onmessage = null;
      try { this._node.disconnect(); } catch (_) {}
      this._node = null;
    }
    if (this._stream) {
      this._stream.getTracks().forEach(t => t.stop());
      this._stream = null;
    }
    if (this._encoder) {
      if (this._encoder.state !== 'closed') { try { this._encoder.close(); } catch (_) {} }
      this._encoder = null;
    }
    if (this._ctx) {
      const ctx = this._ctx;
      this._ctx = null;
      if (ctx.state !== 'closed') { ctx.close().catch(() => {}); }
    }
  }
}

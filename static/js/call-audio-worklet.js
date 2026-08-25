// AudioWorklet processor for voice-call mic capture (Phase 1b).
//
// Runs on the dedicated audio render thread (WebAudio spec's own
// 128-sample "render quantum" per process() call, not a size this file
// controls), not the main thread — so it just accumulates samples into
// exact 20ms/960-sample frames (matching call_manager.py's own
// SAMPLE_RATE_HZ=48000/FRAME_DURATION_MS=20 wire contract) and hands
// each finished frame to the main thread via postMessage for actual
// Opus encoding (WebCodecs AudioEncoder isn't available inside an
// AudioWorkletGlobalScope).
class CallCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = new Float32Array(960);
    this._offset = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      const channel = input[0];
      for (let i = 0; i < channel.length; i++) {
        this._buffer[this._offset++] = channel[i];
        if (this._offset === this._buffer.length) {
          // .slice() copies -- this._buffer is reused for the next
          // frame, and postMessage's own structured-clone would copy
          // it anyway, so this just makes the copy point explicit.
          this.port.postMessage(this._buffer.slice());
          this._offset = 0;
        }
      }
    }
    return true; // keep this processor alive for the life of the call
  }
}

registerProcessor('call-capture-processor', CallCaptureProcessor);

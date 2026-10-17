// ---------------------------------------------------------------------------
// Ogg Opus writer (RFC 3533 pages, RFC 7845 Opus mapping).
//
// A browser's Opus encoder (WebCodecs AudioEncoder) hands back bare Opus
// packets. A voice message has to travel as a stream a media player can open,
// and the format LXMF defines for it (AM_OPUS_OGG) is Opus inside Ogg. This
// puts the packets into that container without touching the audio.
//
//   const w = new OggOpusWriter();
//   w.addPacket(packet);            // one call per encoded 20 ms packet
//   const bytes = w.finish();       // Uint8Array, a complete .ogg file
//
// No DOM and no dependencies, so it also runs under Node (the tests do).
// ---------------------------------------------------------------------------

(function (root) {
  'use strict';

  const OPUS_RATE = 48000;            // Ogg Opus granule positions always count 48 kHz samples
  const DEFAULT_PRE_SKIP = 312;       // libopus's encoder delay at 48 kHz
  const MAX_SEGMENTS = 255;           // a page's segment table holds at most 255 entries
  const PACKETS_PER_PAGE = 50;        // one second of 20 ms packets

  // Ogg's CRC-32: polynomial 0x04C11DB7, no bit reflection, initial value 0,
  // no final XOR. (Not the CRC-32 that zip and PNG use.)
  const CRC_TABLE = (() => {
    const table = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let r = i << 24;
      for (let j = 0; j < 8; j++) r = (r & 0x80000000) ? ((r << 1) ^ 0x04C11DB7) : (r << 1);
      table[i] = r >>> 0;
    }
    return table;
  })();

  function crc32(bytes) {
    let crc = 0;
    for (let i = 0; i < bytes.length; i++) {
      crc = ((crc << 8) ^ CRC_TABLE[((crc >>> 24) ^ bytes[i]) & 0xFF]) >>> 0;
    }
    return crc >>> 0;
  }

  // Lacing values for one packet: runs of 255, then the remainder (which is
  // 0 when the packet is an exact multiple of 255, to mark where it ends).
  function lacing(length) {
    const out = [];
    let left = length;
    while (left >= 255) { out.push(255); left -= 255; }
    out.push(left);
    return out;
  }

  function ascii(s) {
    const out = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i) & 0x7F;
    return out;
  }

  function concat(parts) {
    let n = 0;
    for (const p of parts) n += p.length;
    const out = new Uint8Array(n);
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }

  function randomSerial() {
    if (root.crypto && root.crypto.getRandomValues) {
      return root.crypto.getRandomValues(new Uint32Array(1))[0];
    }
    return (Math.random() * 0xFFFFFFFF) >>> 0;
  }

  class OggOpusWriter {
    constructor(opts) {
      const o = opts || {};
      this.channels = o.channels || 1;
      this.inputRate = o.inputSampleRate || OPUS_RATE;
      this.preSkip = o.preSkip === undefined ? DEFAULT_PRE_SKIP : o.preSkip;
      this.serial = (o.serial === undefined ? randomSerial() : o.serial) >>> 0;
      this._pages = [];
      this._seq = 0;
      this._granule = 0;             // total 48 kHz samples written so far
      this._pending = [];            // packets waiting to go into the next page
      this._pendingSegments = 0;
      this._packets = 0;
      this._finished = false;
      this._writeHeaders();
    }

    get packetCount() { return this._packets; }
    get durationSeconds() { return this._granule / OPUS_RATE; }

    _page(headerType, granule, packets) {
      const segs = [];
      for (const p of packets) for (const v of lacing(p.length)) segs.push(v);
      const header = new Uint8Array(27 + segs.length);
      const dv = new DataView(header.buffer);
      header.set(ascii('OggS'), 0);
      header[4] = 0;                                   // stream structure version
      header[5] = headerType;                          // 0x02 first page, 0x04 last page
      // 64-bit little-endian granule position (the high word is always 0 here)
      dv.setUint32(6, granule >>> 0, true);
      dv.setUint32(10, Math.floor(granule / 4294967296) >>> 0, true);
      dv.setUint32(14, this.serial, true);
      dv.setUint32(18, this._seq++, true);
      // bytes 22-25: CRC, filled in below over the whole page
      header[26] = segs.length;
      header.set(segs, 27);
      const page = concat([header, ...packets]);
      new DataView(page.buffer).setUint32(22, crc32(page), true);
      this._pages.push(page);
    }

    _writeHeaders() {
      const head = new Uint8Array(19);
      const dv = new DataView(head.buffer);
      head.set(ascii('OpusHead'), 0);
      head[8] = 1;                                     // version
      head[9] = this.channels;
      dv.setUint16(10, this.preSkip, true);
      dv.setUint32(12, this.inputRate, true);          // informational
      dv.setInt16(16, 0, true);                        // output gain
      head[18] = 0;                                    // channel mapping family 0 (mono / stereo)
      this._page(0x02, 0, [head]);

      const vendor = ascii('NomadPortal');
      const tags = new Uint8Array(8 + 4 + vendor.length + 4);
      const tv = new DataView(tags.buffer);
      tags.set(ascii('OpusTags'), 0);
      tv.setUint32(8, vendor.length, true);
      tags.set(vendor, 12);
      tv.setUint32(12 + vendor.length, 0, true);       // no user comments
      this._page(0x00, 0, [tags]);
    }

    _flushPending(last) {
      if (!this._pending.length && !last) return;
      this._page(last ? 0x04 : 0x00, this._granule, this._pending);
      this._pending = [];
      this._pendingSegments = 0;
    }

    // `samples` is how many 48 kHz samples this packet decodes to (960 for
    // the 20 ms packets the recorder produces).
    addPacket(packet, samples) {
      if (this._finished) throw new Error('OggOpusWriter: already finished');
      const bytes = packet instanceof Uint8Array ? packet : new Uint8Array(packet);
      const need = lacing(bytes.length).length;
      if (this._pending.length &&
          (this._pendingSegments + need > MAX_SEGMENTS || this._pending.length >= PACKETS_PER_PAGE)) {
        this._flushPending(false);
      }
      this._pending.push(bytes);
      this._pendingSegments += need;
      this._granule += samples === undefined ? 960 : samples;
      this._packets++;
    }

    finish() {
      if (this._finished) throw new Error('OggOpusWriter: already finished');
      this._finished = true;
      this._flushPending(true);          // always writes a last page, even an empty one
      return concat(this._pages);
    }
  }

  const api = { OggOpusWriter, crc32 };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.OggOpusWriter = OggOpusWriter;
})(typeof window !== 'undefined' ? window : globalThis);

// Voice messages. Part 1: the Ogg Opus muxer, checked with an independent
// parser (a different CRC implementation, written from the RFC, not copied
// from the muxer) and, where ffmpeg exists, a real decoder. Part 2 (below):
// the recorder in a real browser with a fake microphone.
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const { OggOpusWriter } = require('../../../static/js/ogg-opus.js');

// ---- an independent Ogg reader --------------------------------------------
function bitwiseCrc(bytes) {                 // bit by bit, no table
  let crc = 0;
  for (const b of bytes) {
    crc ^= b << 24;
    for (let i = 0; i < 8; i++) crc = (crc & 0x80000000) ? ((crc << 1) ^ 0x04C11DB7) : (crc << 1);
    crc >>>= 0;
  }
  return crc >>> 0;
}

function readOgg(buf) {
  const pages = [];
  let off = 0;
  while (off < buf.length) {
    if (buf.slice(off, off + 4).toString('latin1') !== 'OggS') throw new Error(`no capture pattern at ${off}`);
    const nseg = buf[off + 26];
    const segs = Array.from(buf.slice(off + 27, off + 27 + nseg));
    const dataLen = segs.reduce((a, b) => a + b, 0);
    const total = 27 + nseg + dataLen;
    const page = Buffer.from(buf.slice(off, off + total));
    const stored = page.readUInt32LE(22);
    const zeroed = Buffer.from(page); zeroed.writeUInt32LE(0, 22);
    pages.push({
      version: page[4], flags: page[5],
      granule: Number(page.readBigUInt64LE(6)),
      serial: page.readUInt32LE(14), seq: page.readUInt32LE(18),
      crcOk: stored === bitwiseCrc(zeroed), segs, nseg,
      data: page.slice(27 + nseg), total,
    });
    off += total;
  }
  return pages;
}

function packetsOf(pages) {                  // reassemble packets from lacing values
  const out = [];
  let cur = [];
  for (const p of pages) {
    let o = 0;
    for (const s of p.segs) {
      cur.push(p.data.slice(o, o + s)); o += s;
      if (s < 255) { out.push(Buffer.concat(cur)); cur = []; }
    }
  }
  return out;
}

const pkt = (n, fill) => Buffer.alloc(n, fill === undefined ? n & 0xFF : fill);

module.exports = async ctx => {
  await muxerChecks(ctx);
  await recorderChecks(ctx);
};

async function muxerChecks(ctx) {
  // ---- structure ----------------------------------------------------------
  const w = new OggOpusWriter({ serial: 0x1234abcd });
  for (let i = 0; i < 120; i++) w.addPacket(pkt(60 + (i % 7), i));
  const buf = Buffer.from(w.finish());
  const pages = readOgg(buf);
  const packets = packetsOf(pages);

  ctx.check(pages.every(p => p.crcOk), `every page has a valid Ogg CRC (${pages.length} pages)`);
  ctx.check(pages.every(p => p.version === 0 && p.serial === 0x1234abcd), 'version 0 and one serial number throughout');
  ctx.check(pages.every((p, i) => p.seq === i), 'page sequence numbers count up from 0');
  ctx.check(pages[0].flags === 0x02 && pages.slice(1).every(p => !(p.flags & 0x02)), 'only the first page is marked beginning-of-stream');
  ctx.check((pages[pages.length - 1].flags & 0x04) !== 0 && pages.slice(0, -1).every(p => !(p.flags & 0x04)), 'only the last page is marked end-of-stream');

  const head = packets[0];
  ctx.check(head.slice(0, 8).toString() === 'OpusHead' && head[8] === 1 && head[9] === 1 && head.length === 19, 'page 1 holds a valid OpusHead (version 1, mono)');
  ctx.check(head.readUInt16LE(10) === 312 && head.readUInt32LE(12) === 48000 && head[18] === 0, 'pre-skip 312, 48 kHz, mapping family 0');
  ctx.check(packets[1].slice(0, 8).toString() === 'OpusTags' && packets[1].readUInt32LE(8) === 'NomadPortal'.length, 'page 2 holds OpusTags with a vendor string');
  ctx.check(pages[0].granule === 0 && pages[1].granule === 0, 'header pages have granule position 0');

  const audio = packets.slice(2);
  ctx.check(audio.length === 120 && audio.every((p, i) => p.equals(pkt(60 + (i % 7), i))), 'all 120 audio packets come back byte for byte, in order');
  const granules = pages.slice(2).map(p => p.granule);
  ctx.check(granules.every((g, i) => i === 0 || g > granules[i - 1]), 'granule positions only go up');
  ctx.check(granules[granules.length - 1] === 120 * 960, `the final granule position counts every sample (${granules[granules.length - 1]})`);
  ctx.check(pages.slice(2).every(p => p.nseg <= 255), 'no page has more than 255 segments');
  ctx.check(Math.abs(w.durationSeconds - 2.4) < 1e-9, `duration is 120 x 20 ms (${w.durationSeconds}s)`);

  // ---- lacing edge cases --------------------------------------------------
  const sizes = [0, 1, 254, 255, 256, 509, 510, 511, 1275];
  const w2 = new OggOpusWriter({ serial: 7 });
  sizes.forEach((n, i) => w2.addPacket(pkt(n, i + 1)));
  const out2 = packetsOf(readOgg(Buffer.from(w2.finish()))).slice(2);
  ctx.check(out2.length === sizes.length && out2.every((p, i) => p.length === sizes[i] && p.equals(pkt(sizes[i], i + 1))),
    `packets of ${sizes.join(', ')} bytes survive the lacing (exact multiples of 255 included)`);

  // ---- big packets never overflow a page -----------------------------------
  const w3 = new OggOpusWriter({ serial: 9 });
  for (let i = 0; i < 400; i++) w3.addPacket(pkt(1000, i), 960);
  const p3 = readOgg(Buffer.from(w3.finish()));
  ctx.check(p3.every(p => p.nseg <= 255) && p3.every(p => p.crcOk), 'large packets are split across pages, never over 255 segments');
  ctx.check(packetsOf(p3).slice(2).length === 400, 'and none are lost');

  // ---- edge cases ---------------------------------------------------------
  const empty = readOgg(Buffer.from(new OggOpusWriter({ serial: 1 }).finish()));
  ctx.check(empty.length === 3 && empty.every(p => p.crcOk) && (empty[2].flags & 0x04) !== 0, 'an empty recording is still a well-formed stream');
  let threw = false;
  const w4 = new OggOpusWriter(); w4.finish();
  try { w4.addPacket(pkt(10)); } catch (_) { threw = true; }
  ctx.check(threw, 'adding a packet after finishing is an error, not silent corruption');
  const a = new OggOpusWriter().finish(), b = new OggOpusWriter().finish();
  ctx.check(!Buffer.from(a).equals(Buffer.from(b)), 'each stream gets its own random serial number');

  // ---- against a real decoder (ffmpeg), when present ----------------------
  const have = c => spawnSync(c, ['-version']).status === 0;
  if (!have('ffmpeg') || !have('ffprobe')) {
    console.log('  skip ffmpeg cross-check (ffmpeg not installed)');
  } else {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ogg-'));
    const src = path.join(tmp, 'src.ogg');
    const enc = spawnSync('ffmpeg', ['-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
      '-ar', '48000', '-ac', '1', '-c:a', 'libopus', '-b:a', '24k', '-frame_duration', '20', '-y', src]);
    if (enc.status !== 0) {
      console.log('  skip ffmpeg cross-check (this ffmpeg has no libopus encoder)');
    } else {
      // take real Opus packets from a real encoder, put them in OUR container
      const sp = packetsOf(readOgg(fs.readFileSync(src))).slice(2);
      const w5 = new OggOpusWriter({ serial: 42 });
      sp.forEach(p => w5.addPacket(p, 960));
      const mine = path.join(tmp, 'mine.ogg');
      fs.writeFileSync(mine, Buffer.from(w5.finish()));

      const probe = spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'stream=codec_name,channels,sample_rate:format=format_name,duration',
        '-of', 'json', mine]);
      let info = {};
      try { info = JSON.parse(probe.stdout.toString()); } catch (_) {}
      ctx.check(info.format && /ogg/.test(info.format.format_name) && info.streams && info.streams[0].codec_name === 'opus' &&
                info.streams[0].channels === 1 && info.streams[0].sample_rate === '48000',
        `ffprobe reads it as mono 48 kHz Opus in Ogg (${JSON.stringify((info.streams || [{}])[0])})`);
      const dur = parseFloat(info.format && info.format.duration);
      ctx.check(Math.abs(dur - (sp.length * 0.02 - 312 / 48000)) < 0.02, `and reports the right duration (${dur}s for ${sp.length} packets)`);

      // decode both the original and ours: the audio must be identical
      const pcm = f => spawnSync('ffmpeg', ['-v', 'error', '-i', f, '-f', 's16le', '-ac', '1', '-ar', '48000', '-'], { maxBuffer: 1 << 28 }).stdout;
      const orig = pcm(src), ours = pcm(mine);
      ctx.check(orig.length > 0 && Buffer.compare(orig.slice(0, Math.min(orig.length, ours.length)), ours.slice(0, Math.min(orig.length, ours.length))) === 0,
        `decoded audio is bit-identical to the original encoding (${ours.length} bytes compared)`);
      const raw = Buffer.from(fs.readFileSync(mine)); raw[raw.length - 5] ^= 0xFF;
      ctx.check(readOgg(raw).some(p => !p.crcOk), 'a flipped byte is caught by the CRC check (so the CRC really covers the data)');
    }
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

// ---- Part 2: the recorder in a real browser, with a fake microphone --------
const PEER = 'bb'.repeat(16);

// Headless Chromium here has no audio hardware, and without an audio device
// the AudioWorklet thread never starts (addModule waits forever; the page
// server never even sees a request for the module). So the browser test
// replaces ONLY the microphone capture: a stand-in node emits the same
// 960-sample, 48 kHz frames call-audio-worklet.js does, carrying a 440 Hz
// tone. Everything after that is real: AudioEncoder, the Ogg muxer, the
// recorder, the UI, and the browser's own decoder on the result. The worklet
// file and the real microphone are covered by check H in the manual plan.
const fakeCapture = () => {
  Object.defineProperty(AudioContext.prototype, 'audioWorklet', {
    get() { return { addModule: async () => {} }; },
  });
  window.AudioWorkletNode = class extends GainNode {
    constructor(ctx) {
      super(ctx);
      this.port = { onmessage: null };
      let n = 0;
      this._timer = setInterval(() => {
        if (!this.port.onmessage) return;
        const f = new Float32Array(960);
        for (let i = 0; i < 960; i++) f[i] = 0.3 * Math.sin(2 * Math.PI * 440 * (n * 960 + i) / 48000);
        n++;
        this.port.onmessage({ data: f });
      }, 20);
    }
    disconnect(...a) { clearInterval(this._timer); return super.disconnect(...a); }
  };
};
const FAKE_CAPTURE = `(${fakeCapture})();`;

async function openChat(ctx, extra = {}) {
  const p = await ctx.open({ admin: true, contacts: [{ hash: PEER, name: 'Bea' }],
    received: [{ id: 'r1', source: PEER, content: 'hi', received_at: Math.floor(Date.now() / 1000), read: true }],
    ui_extra: { admins_messages_panel: true }, ...extra.scn },
    { initScript: FAKE_CAPTURE, ...(extra.opts || {}) });
  await p.click('#sidebar-tab-messages');
  await p.evaluate(h => { refreshChats().then(() => openConversation(h)); }, PEER);
  await p.waitForSelector('#chat-input', { state: 'visible', timeout: 5000 });
  return p;
}

// Remember every microphone track so we can prove they were all released.
const trackSpy = () => {
  const real = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  window.__tracks = [];
  navigator.mediaDevices.getUserMedia = async c => {
    const s = await real(c);
    s.getTracks().forEach(t => window.__tracks.push(t));
    return s;
  };
};
const tracksEnded = p => p.evaluate(() => window.__tracks.length > 0 && window.__tracks.every(t => t.readyState === 'ended'));

async function recorderChecks(ctx) {
  // 1. record, stop, and the clip is staged as an Ogg file the browser can decode
  let p = await openChat(ctx);
  await p.evaluate(trackSpy);
  ctx.check(await p.locator('#btn-chat-voice').isVisible(), 'the record button is shown in a browser that supports it');
  await p.click('#btn-chat-voice');
  await p.waitForSelector('#chat-voice-bar', { state: 'visible', timeout: 5000 });
  ctx.check(await p.locator('#btn-chat-voice').isDisabled(), 'the record button is disabled while recording');
  await p.waitForTimeout(2300);
  const clock = await p.locator('#chat-voice-timer').textContent();
  ctx.check(/^0:0[1-9]$/.test(clock), `the timer runs (${clock})`);
  ctx.check(/up to/.test(await p.locator('#chat-voice-limit').textContent()), 'and says how long the recording may be');
  await p.click('#btn-voice-stop');
  await p.waitForFunction(() => document.querySelectorAll('#chat-attach-list .attach-chip').length === 1, null, { timeout: 8000 });
  ctx.check(await p.locator('#chat-voice-bar').isHidden(), 'the recording bar goes away when it stops');
  ctx.check(await tracksEnded(p), 'the microphone is released after stopping');

  const info = await p.evaluate(async () => {
    const f = _stagedAttachments[0].file;
    const buf = await f.arrayBuffer();
    const bytes = new Uint8Array(buf);
    const ascii = (a, n) => String.fromCharCode(...bytes.slice(a, a + n));
    let decoded = null;
    try {
      const ac = new AudioContext({ sampleRate: 48000 });
      const audio = await ac.decodeAudioData(buf.slice(0));
      const ch = audio.getChannelData(0);
      let sum = 0; for (let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
      decoded = { seconds: audio.duration, channels: audio.numberOfChannels, rate: audio.sampleRate, rms: Math.sqrt(sum / ch.length) };
      ac.close();
    } catch (e) { decoded = { error: String(e) }; }
    return { name: f.name, type: f.type, size: f.size, capture: ascii(0, 4), head: ascii(28, 8), tags: null, decoded };
  });
  ctx.check(/^voice-message-\d{6}\.ogg$/.test(info.name) && info.type === 'audio/ogg', `it is staged as an audio/ogg file (${info.name}, ${info.size} bytes)`);
  ctx.check(info.capture === 'OggS' && info.head === 'OpusHead', 'it starts with an Ogg page holding OpusHead');
  ctx.check(info.size > 3000 && info.size < 30000, 'and its size fits Opus at 24 kbit/s');
  ctx.check(!info.decoded.error && Math.abs(info.decoded.seconds - 2.3) < 0.8 && info.decoded.channels === 1,
    `the browser's own decoder opens it: ${JSON.stringify(info.decoded)}`);
  ctx.check(info.decoded.rms > 0.001, 'and the audio is not silence (the fake microphone tone made it through)');

  // 2. sending carries it as an audio/ogg multipart file
  await p.click('#btn-chat-send');
  await p.waitForFunction(() => fetch('/__stats').then(r => r.json()).then(j => j.message_posts.length > 0), null, { timeout: 5000 });
  const posted = (await p.evaluate(() => fetch('/__stats').then(r => r.json()))).message_posts[0];
  const f = posted.files[0] || {};
  ctx.check(posted.content_type === 'multipart/form-data' && /^voice-message-\d{6}\.ogg$/.test(f.name) && f.type === 'audio/ogg' && f.head === 'OggS',
    `sending posts the clip as an audio/ogg multipart file (${JSON.stringify(f)})`);

  // 3. cancel throws it away and releases the microphone
  p = await openChat(ctx);
  await p.evaluate(trackSpy);
  await p.click('#btn-chat-voice');
  await p.waitForSelector('#chat-voice-bar', { state: 'visible', timeout: 5000 });
  await p.waitForTimeout(600);
  await p.click('#btn-voice-cancel');
  ctx.check(await p.locator('#chat-voice-bar').isHidden() && await p.locator('#chat-attach-list .attach-chip').count() === 0,
    'Cancel discards the recording and stages nothing');
  ctx.check(await tracksEnded(p) && !(await p.locator('#btn-chat-voice').isDisabled()), 'and the microphone is released and the button works again');

  // 4. Escape cancels too
  await p.click('#btn-chat-voice');
  await p.waitForSelector('#chat-voice-bar', { state: 'visible', timeout: 5000 });
  await p.keyboard.press('Escape');
  ctx.check(await p.locator('#chat-voice-bar').isHidden() && await tracksEnded(p), 'Escape cancels a recording and releases the microphone');

  // 5. a recording that is far too short is not sent
  await p.click('#btn-chat-voice');
  await p.waitForSelector('#chat-voice-bar', { state: 'visible', timeout: 5000 });
  await p.click('#btn-voice-stop');
  await p.waitForFunction(() => document.getElementById('chat-voice-bar').hidden, null, { timeout: 5000 });
  ctx.check(await p.locator('#chat-attach-list .attach-chip').count() === 0, 'a recording under half a second is dropped, not staged');

  // 6. permission denied: a clear message, nothing stuck
  p = await openChat(ctx);
  await p.evaluate(() => {
    navigator.mediaDevices.getUserMedia = async () => { const e = new Error('denied'); e.name = 'NotAllowedError'; throw e; };
  });
  await p.click('#btn-chat-voice');
  await p.waitForFunction(() => /allow/i.test(document.getElementById('status-text')?.textContent || ''), null, { timeout: 5000 });
  ctx.check(await p.locator('#chat-voice-bar').isHidden() && !(await p.locator('#btn-chat-voice').isDisabled()),
    'when the microphone is refused the reader is told how to fix it, and nothing is left stuck');

  // 7. no room left in the message
  p = await openChat(ctx);
  await p.evaluate(() => { _stagedAttachments = [{ file: { name: 'big.bin', size: 499 * 1024 }, size: 499 * 1024 }]; _renderAttachChips(); trackSpy2(); function trackSpy2() { window.__asked = 0; const r = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices); navigator.mediaDevices.getUserMedia = c => { window.__asked++; return r(c); }; } });
  await p.click('#btn-chat-voice');
  await p.waitForFunction(() => /No room/.test(document.getElementById('status-text')?.textContent || ''), null, { timeout: 5000 });
  ctx.check(await p.evaluate(() => window.__asked) === 0, 'with no room left under the size cap it says so and never opens the microphone');

  // 8. a browser without the encoder simply has no record button
  p = await openChat(ctx, { opts: { initScript: FAKE_CAPTURE + ' delete window.AudioEncoder;' } });
  ctx.check(await p.locator('#btn-chat-voice').isHidden(), 'without AudioEncoder the record button stays hidden');
  ctx.check(await p.locator('#btn-chat-attach').isVisible(), 'and attaching an audio file still works');
}

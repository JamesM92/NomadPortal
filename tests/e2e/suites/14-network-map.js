// Network map. Part 1: the layout engine as pure geometry, under random input
// from a seeded generator (so a failure can be reproduced). Part 2 (below):
// the map in a real browser.
const { layoutNetworkMap, UNROUTED } = require('../../../static/js/network-map.js');

function rng(seed) {                         // mulberry32
  return () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}
const hex = (r, n) => Array.from({ length: n }, () => Math.floor(r() * 16).toString(16)).join('');

function randomData(seed, count, nIface = 3, nVia = 6) {
  const r = rng(seed);
  const interfaces = Array.from({ length: nIface }, (_, i) => ({ name: `if${i}`, online: r() > 0.2, rxb: 100, txb: 50 }));
  const vias = Array.from({ length: nVia }, () => hex(r, 32));
  const kinds = ['site', 'peer', 'relay'];
  const nodes = Array.from({ length: count }, () => {
    const routed = r() > 0.1;
    return { hash: hex(r, 32), kind: kinds[Math.floor(r() * 3)], name: r() > 0.5 ? 'n' + hex(r, 4) : null,
             hops: routed ? 1 + Math.floor(r() * 9) : null,
             iface: routed ? interfaces[Math.floor(r() * nIface)].name : null,
             via: routed && r() > 0.3 ? vias[Math.floor(r() * nVia)] : null };
  });
  return { interfaces, nodes };
}
const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

module.exports = async ctx => {
  await layoutChecks(ctx);
  await mapChecks(ctx);
};

async function layoutChecks(ctx) {
  for (const [seed, count] of [[1, 5], [2, 60], [3, 400], [4, 1500]]) {
    const data = randomData(seed, count);
    const L = layoutNetworkMap(data);
    const ids = new Set(L.nodes.map(n => n.id));
    ctx.check(L.nodes.length === count && ids.size === count, `${count} destinations: each placed exactly once`);
    ctx.check(L.nodes.every(n => Number.isFinite(n.x) && Number.isFinite(n.y) && n.x >= 0 && n.y >= 0 && n.x <= L.width && n.y <= L.height),
      `${count}: all inside the drawing area (${Math.round(L.width)}x${Math.round(L.height)})`);
    let min = Infinity;
    const pts = L.nodes;
    for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < Math.min(pts.length, i + 400); j++) min = Math.min(min, dist(pts[i], pts[j]));
    // exhaustive for the smaller sets
    if (count <= 400) { min = Infinity; for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) min = Math.min(min, dist(pts[i], pts[j])); }
    ctx.check(min >= 13, `${count}: no two destinations closer than 13px (closest ${min.toFixed(1)})`);
  }

  // slices: a destination stays inside its interface's slice
  const data = randomData(7, 300);
  const L = layoutNetworkMap(data);
  const slice = Object.fromEntries(L.interfaces.map(i => [i.key, i]));
  const norm = a => { while (a < -Math.PI / 2) a += 2 * Math.PI; while (a >= 1.5 * Math.PI) a -= 2 * Math.PI; return a; };
  const outside = L.nodes.filter(n => {
    const key = n.iface === null ? UNROUTED : n.iface;
    const s = slice[key]; const a = norm(Math.atan2(n.y - L.cy, n.x - L.cx));
    return a < norm(s.a0) - 1e-6 || a > norm(s.a1) + 1e-6;
  });
  ctx.check(outside.length === 0, `every destination lies inside its own interface's slice (${outside.length} outside)`);

  // rings: more hops, further out
  const ringRadius = h => { const n = L.nodes.filter(x => x.hops === h); return n.length ? Math.min(...n.map(x => dist(x, L.portal))) : null; };
  const rs = [1, 2, 3, 4, 5].map(ringRadius).filter(x => x !== null);
  ctx.check(rs.every((r, i) => i === 0 || r > rs[i - 1]), `a destination further away sits on a further ring (${rs.map(Math.round).join(' < ')})`);
  const far = layoutNetworkMap({ interfaces: [{ name: 'x', online: true }], nodes: [
    { hash: '1'.repeat(32), kind: 'site', hops: 9, iface: 'x', via: null },
    { hash: '2'.repeat(32), kind: 'site', hops: null, iface: 'x', via: null }] });
  const d = id => dist(far.nodes.find(n => n.id === id), far.portal);
  ctx.check(d('2'.repeat(32)) > d('1'.repeat(32)), 'a destination with an unknown hop count sits beyond every numbered ring');

  // edges
  const idsAll = new Set(['portal', ...L.interfaces.map(i => i.id), ...L.hubs.map(h => h.id), ...L.nodes.map(n => n.id)]);
  ctx.check(L.edges.every(e => idsAll.has(e.from) && idsAll.has(e.to)), 'every edge joins two things that exist');
  ctx.check(L.edges.filter(e => e.kind === 'node').length === L.nodes.filter(n => n.iface !== null).length,
    'each routed destination has exactly one edge in; unrouted ones have none');
  const hubByVia = new Map();
  L.hubs.forEach(h => hubByVia.set(h.iface + '|' + h.via, (hubByVia.get(h.iface + '|' + h.via) || 0) + 1));
  ctx.check(Array.from(hubByVia.values()).every(c => c === 1), 'one hub per next hop per interface');
  ctx.check(L.hubs.every(h => h.count === L.nodes.filter(n => n.iface === h.iface && n.via === h.via).length), "each hub's count matches the destinations under it");

  // size of slices follows load
  const two = layoutNetworkMap({ interfaces: [{ name: 'busy', online: true }, { name: 'quiet', online: true }],
    nodes: Array.from({ length: 40 }, (_, i) => ({ hash: 'a' + String(i).padStart(31, '0'), kind: 'site', hops: 1, iface: 'busy', via: null }))
      .concat([{ hash: 'b'.repeat(32), kind: 'site', hops: 1, iface: 'quiet', via: null }]) });
  const span = k => { const i = two.interfaces.find(x => x.key === k); return i.a1 - i.a0; };
  ctx.check(span('busy') > span('quiet') && span('quiet') > 0.2, 'a busy interface gets a bigger slice, and a quiet one stays visible');

  // determinism and speed
  const a = JSON.stringify(layoutNetworkMap(randomData(11, 200))), b = JSON.stringify(layoutNetworkMap(randomData(11, 200)));
  ctx.check(a === b, 'the same input always gives the same picture');
  const t0 = Date.now(); layoutNetworkMap(randomData(12, 2000)); const ms = Date.now() - t0;
  ctx.check(ms < 1500, `2000 destinations lay out quickly (${ms} ms)`);

  // messy input
  ctx.check(layoutNetworkMap({ interfaces: [], nodes: [] }).width > 0, 'an empty network still gives a valid drawing');
  ctx.check(layoutNetworkMap({}).nodes.length === 0 && layoutNetworkMap(undefined).nodes.length === 0, 'missing data is tolerated');
  const only = layoutNetworkMap({ interfaces: [{ name: 'a', online: true }, { name: 'b', online: false }], nodes: [] });
  ctx.check(only.interfaces.length === 2 && only.interfaces[1].online === false, 'interfaces with nothing behind them are still drawn');
  const odd = layoutNetworkMap({ interfaces: [{ name: 'known', online: true }], nodes: [
    { hash: '1'.repeat(32), kind: 'peer', hops: 2, iface: 'ghost', via: null },
    { hash: '2'.repeat(32), kind: 'site', hops: 99, iface: 'known', via: null },
    { hash: '3'.repeat(32), kind: 'relay', hops: 0, iface: 'known', via: null }] });
  ctx.check(odd.nodes.length === 3 && odd.interfaces.some(i => i.key === 'ghost'), 'a destination on an unlisted interface gets that interface added');
  ctx.check(odd.nodes.every(n => n.ring >= 1 && n.ring <= 13), 'absurd hop counts are clamped onto a ring');
  const dup = layoutNetworkMap({ interfaces: [{ name: 'x' }, { name: 'x' }], nodes: [] });
  ctx.check(dup.interfaces.length === 1, 'a repeated interface name is drawn once');
  const same = layoutNetworkMap({ interfaces: [{ name: 'x' }], nodes: Array.from({ length: 50 }, (_, i) => ({ hash: String(i).padStart(32, '0'), kind: 'site', hops: 1, iface: 'x', via: null })) });
  let m2 = Infinity; for (let i = 0; i < same.nodes.length; i++) for (let j = i + 1; j < same.nodes.length; j++) m2 = Math.min(m2, dist(same.nodes[i], same.nodes[j]));
  ctx.check(m2 >= 13, `many destinations on one ring spread out instead of piling up (closest ${m2.toFixed(1)}px)`);
}


// ---- Part 2: the map in a browser ------------------------------------------
const H = c => c.repeat(32);
const now = Math.floor(Date.now() / 1000);
const seen = (extra) => ({ last_seen: now - 60, first_seen: now - 600, announce_count: 3, ...extra });
function scenario(extra) {
  const via1 = H('1'), via2 = H('2');
  return {
    admin: true, ui_extra: { admins_network_panel: true },
    net_nodes: [{ hash: H('a'), name: 'Alpha Site', hops: 2, ...seen() }, { hash: H('b'), name: 'Beta Site', hops: 3, ...seen() }, { hash: H('c'), name: 'Gamma Site', hops: 1, ...seen() }],
    net_peers: [{ hash: H('d'), name: 'Dora', hops: 2, ...seen() }, { hash: H('e'), name: 'Eli', hops: 2, ...seen() },
                { hash: H('f'), name: 'Fay', hops: 4, ...seen() }, { hash: H('9'), name: 'Ghost', hops: null, ...seen() }],
    net_relays: [{ hash: H('7'), hops: 1, picked: true, ...seen() }, { hash: H('8'), hops: 5, picked: false, ...seen() }],
    net_ifaces: [{ name: 'TCP hub', online: true, rxb: 5000, txb: 2000 }, { name: 'LoRa', online: false, rxb: 0, txb: 0 }],
    net_routes: {
      [H('a')]: { hops: 2, iface: 'TCP hub', via: via1 }, [H('b')]: { hops: 3, iface: 'TCP hub', via: via1 },
      [H('c')]: { hops: 1, iface: 'TCP hub', via: null }, [H('d')]: { hops: 2, iface: 'TCP hub', via: via2 },
      [H('e')]: { hops: 2, iface: 'TCP hub', via: via2 }, [H('f')]: { hops: 4, iface: 'LoRa', via: via2 },
      [H('7')]: { hops: 1, iface: 'TCP hub', via: null }, [H('8')]: { hops: 5, iface: 'TCP hub', via: via1 },
      [H('9')]: { hops: null, iface: null, via: null },
    },
    ...extra,
  };
}
const openMap = async (ctx, extra) => {
  const p = await ctx.open(scenario(extra));
  // With only the Network panel enabled it is already the panel, the tab strip
  // is hidden, and the sidebar starts collapsed: open it.
  if (!(await p.locator('#btn-network-map').isVisible())) await p.click('#btn-sidebar-collapse');
  await p.click('#btn-network-map');
  return p;
};
const vb = p => p.evaluate(() => document.getElementById('netmap-svg').getAttribute('viewBox').split(' ').map(Number));
const shown = (p, kind) => p.evaluate(k => Array.from(document.querySelectorAll('.nm-node.nm-' + k)).filter(e => getComputedStyle(e).display !== 'none').length, kind);

async function mapChecks(ctx) {
  let p = await openMap(ctx);
  await p.waitForSelector('#netmap-svg .nm-node', { timeout: 5000 });
  ctx.check(await p.locator('#netmap-svg .nm-node').count() === 9, 'every destination heard is drawn (9)');
  ctx.check(await shown(p, 'site') === 3 && await shown(p, 'peer') === 4 && await shown(p, 'relay') === 2, 'in the right kinds: 3 sites, 4 peers, 2 relays');
  const labels = await p.locator('#netmap-svg .nm-iface text').allTextContents();
  ctx.check(labels.some(t => /^TCP hub \(7\)$/.test(t)) && labels.some(t => /^LoRa \(1\)$/.test(t)) && labels.some(t => /No route known \(1\)/.test(t)),
    `interfaces are labelled with how many destinations use them (${labels.join(' | ')})`);
  ctx.check(await p.locator('#netmap-svg .nm-iface-down').count() === 1, 'an offline interface is marked');
  ctx.check(await p.locator('#netmap-svg .nm-hub').count() === 3, 'next-hop nodes are shown once each per interface (3)');
  ctx.check(/9 destinations on 2 interfaces; 1 with no route known/.test(await p.locator('#netmap-summary').textContent()), 'the summary says what is on the map');
  ctx.check(await p.locator('#netmap-svg .nm-ring-label').count() >= 4, 'rings are labelled with their hop count');

  const dists = await p.evaluate(() => { const c = Array.from(document.querySelectorAll('#netmap-svg .nm-node')).map(e => [+e.getAttribute('cx'), +e.getAttribute('cy')]);
    let m = Infinity; for (let i = 0; i < c.length; i++) for (let j = i + 1; j < c.length; j++) m = Math.min(m, Math.hypot(c[i][0] - c[j][0], c[i][1] - c[j][1])); return m; });
  ctx.check(dists >= 13, `no two drawn destinations overlap (closest ${dists.toFixed(1)}px)`);

  // click opens the details, with the map still behind
  await p.click('#netmap-svg .nm-node[data-hash="' + H('a') + '"]', { force: true });
  await p.waitForSelector('#announce-info-modal:not([hidden])', { timeout: 4000 });
  ctx.check((await p.locator('#announce-info-rows').innerText()).includes(H('a')), 'clicking a destination opens its details');
  ctx.check(await p.locator('#netmap-modal').isVisible(), 'and the map stays open behind them');
  await p.click('#btn-announce-info-close');

  // keyboard
  await p.focus('#netmap-svg .nm-node[data-hash="' + H('d') + '"]');
  await p.keyboard.press('Enter');
  await p.waitForSelector('#announce-info-modal:not([hidden])', { timeout: 4000 });
  ctx.check((await p.locator('#announce-info-rows').innerText()).includes(H('d')), 'a destination can be opened from the keyboard');
  await p.click('#btn-announce-info-close');

  // kind filters
  await p.uncheck('#netmap-kinds input[data-kind="peer"]');
  ctx.check(await shown(p, 'peer') === 0 && await shown(p, 'site') === 3, 'unticking Peers hides the peers and keeps the rest');
  const edgeShown = await p.evaluate(() => Array.from(document.querySelectorAll('.nm-edge-to-peer')).filter(e => getComputedStyle(e).display !== 'none').length);
  ctx.check(edgeShown === 0, 'and the lines to them');
  await p.check('#netmap-kinds input[data-kind="peer"]');
  ctx.check(await shown(p, 'peer') === 4, 'ticking it again brings them back');

  // zoom and pan
  const v0 = await vb(p);
  await p.click('#btn-netmap-zoom-in');
  const v1 = await vb(p);
  ctx.check(v1[2] < v0[2], 'zoom in narrows the view');
  await p.click('#btn-netmap-zoom-out');
  await p.click('#btn-netmap-zoom-out');
  ctx.check((await vb(p))[2] > v1[2], 'zoom out widens it');
  await p.click('#btn-netmap-reset');
  ctx.check((await vb(p)).join() === v0.join(), 'the fit button restores the full view');
  const box = await p.locator('#netmap-svg').boundingBox();
  await p.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await p.mouse.wheel(0, -300);
  await p.waitForTimeout(150);
  ctx.check((await vb(p))[2] < v0[2], 'the mouse wheel zooms');
  const before = await vb(p);
  await p.mouse.move(box.x + 30, box.y + 30);
  await p.mouse.down();
  await p.mouse.move(box.x + 130, box.y + 90, { steps: 4 });
  await p.mouse.up();
  const after = await vb(p);
  ctx.check(after[0] !== before[0] && after[1] !== before[1] && after[2] === before[2], 'dragging moves the view without changing the zoom');

  // closing
  await p.keyboard.press('Escape');
  ctx.check(await p.locator('#netmap-modal').isHidden(), 'Escape closes the map');
  await p.click('#btn-network-map');
  await p.click('#btn-netmap-close');
  ctx.check(await p.locator('#netmap-modal').isHidden(), 'and so does the Close button');

  // nothing heard
  p = await openMap(ctx, { net_nodes: [], net_peers: [], net_relays: [], net_routes: {}, net_ifaces: [] });
  await p.waitForFunction(() => !document.getElementById('netmap-empty').hidden, null, { timeout: 5000 });
  ctx.check(/Nothing has been heard/.test(await p.locator('#netmap-empty').textContent()), 'an empty network says so instead of showing a blank map');

  // interfaces but no destinations still draws the interfaces
  p = await openMap(ctx, { net_nodes: [], net_peers: [], net_relays: [], net_routes: {} });
  await p.waitForSelector('#netmap-svg .nm-iface', { timeout: 5000 });
  ctx.check(await p.locator('#netmap-svg .nm-iface').count() === 2 && await p.locator('#netmap-empty').isHidden(), 'interfaces with no destinations are still drawn');

  // the server fails
  p = await openMap(ctx, { net_routes_fail: true });
  await p.waitForFunction(() => !document.getElementById('netmap-empty').hidden, null, { timeout: 5000 });
  ctx.check(/Could not load the map/.test(await p.locator('#netmap-empty').textContent()), 'a server error is shown, not a blank map');
}


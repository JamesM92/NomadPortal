// ---------------------------------------------------------------------------
// Network map: where everything the portal has heard sits relative to it.
//
//   - the portal is at the centre;
//   - its interfaces form an inner ring, each with a slice of the circle that
//     grows with how many destinations are reached through it;
//   - inside a slice, destinations that share a next hop are grouped under
//     that hop (a small diamond), so you can see which neighbours carry what;
//   - destinations sit on rings by hop count: the nearer, the closer in;
//   - anything Reticulum has no route for goes in its own grey slice.
//
// layoutNetworkMap() is pure geometry (no DOM) so it can be tested in Node;
// renderNetworkMap() draws the result as SVG. Plain <script>, no modules.
// ---------------------------------------------------------------------------

(function (root) {
  'use strict';

  const UNROUTED = '\u0000unrouted';          // key of the "no route known" group
  const DIRECT = '';                          // key of "no next hop: reached directly"
  const MAX_RING = 12;                        // hop counts beyond this share the outer ring

  function sortKey(n) {
    return [n.hops === null || n.hops === undefined ? 1e9 : n.hops, (n.name || '').toLowerCase(), n.hash];
  }
  function byKey(a, b) {
    const x = sortKey(a), y = sortKey(b);
    return x[0] - y[0] || (x[1] < y[1] ? -1 : x[1] > y[1] ? 1 : 0) || (x[2] < y[2] ? -1 : x[2] > y[2] ? 1 : 0);
  }

  // data: { interfaces: [{name, online, rxb, txb}], nodes: [{hash, kind, name, hops, iface, via}] }
  function layoutNetworkMap(data, options) {
    const o = Object.assign({ ifaceRadius: 105, hubRadius: 175, ringStart: 245, ringStep: 58,
                              minGap: 15, gap: 0.07, margin: 46 }, options || {});
    const interfaces = (data && data.interfaces) || [];
    const nodes = ((data && data.nodes) || []).map(n => Object.assign({}, n));

    // ---- group: interface -> next hop -> nodes --------------------------
    const order = [];                                   // interface keys in display order
    const info = new Map();                             // key -> {name, online, rxb, txb}
    for (const i of interfaces) {
      if (!info.has(i.name)) { info.set(i.name, i); order.push(i.name); }
    }
    const byIface = new Map();
    for (const n of nodes) {
      let key = n.iface === null || n.iface === undefined ? UNROUTED : n.iface;
      if (key !== UNROUTED && !info.has(key)) {          // seen on an interface that is not (or no longer) configured
        info.set(key, { name: key, online: null });
        order.push(key);
      }
      if (!byIface.has(key)) byIface.set(key, []);
      byIface.get(key).push(n);
    }
    for (const key of order) if (!byIface.has(key)) byIface.set(key, []);
    if (byIface.has(UNROUTED)) order.push(UNROUTED);

    // ---- slices of the circle -------------------------------------------
    const weights = order.map(k => byIface.get(k).length + 2);
    const total = weights.reduce((a, b) => a + b, 0) || 1;
    const avail = Math.max(0.1, 2 * Math.PI - o.gap * order.length);
    let angle = -Math.PI / 2;
    const slices = order.map((key, i) => {
      const span = avail * weights[i] / total;
      const s = { key, a0: angle + o.gap / 2, a1: angle + o.gap / 2 + span, span };
      angle += span + o.gap;
      return s;
    });

    // ---- groups within each slice, and the rows each ring needs -----------
    const groups = [];                                  // {slice, via, nodes, a0, a1}
    for (const s of slices) {
      const list = byIface.get(s.key).slice().sort(byKey);
      const byVia = new Map();
      for (const n of list) {
        const v = s.key === UNROUTED || !n.via ? DIRECT : n.via;
        if (!byVia.has(v)) byVia.set(v, []);
        byVia.get(v).push(n);
      }
      const keys = Array.from(byVia.keys()).sort((a, b) => (a === DIRECT ? -1 : b === DIRECT ? 1 : a < b ? -1 : a > b ? 1 : 0));
      const gw = keys.map(k => byVia.get(k).length + (k === DIRECT ? 0 : 1));
      const gt = gw.reduce((a, b) => a + b, 0) || 1;
      let a = s.a0;
      keys.forEach((k, i) => {
        const span = s.span * gw[i] / gt;
        groups.push({ slice: s, via: k, nodes: byVia.get(k), a0: a, a1: a + span, span });
        a += span;
      });
    }

    // ---- rings: by hop count; a ring grows extra rows when it is crowded ---
    const ringOf = n => (n.hops === null || n.hops === undefined) ? MAX_RING + 1 : Math.min(Math.max(n.hops, 1), MAX_RING);
    const ringNumbers = Array.from(new Set(nodes.map(ringOf))).sort((a, b) => a - b);
    const ringBase = new Map();
    let base = o.ringStart;
    for (const r of ringNumbers) {
      ringBase.set(r, base);
      let maxRows = 1;
      for (const g of groups) {
        const count = g.nodes.filter(n => ringOf(n) === r).length;
        if (!count) continue;
        const capacity = Math.max(1, Math.floor(base * g.span / o.minGap));
        maxRows = Math.max(maxRows, Math.ceil(count / capacity));
      }
      base += (maxRows - 1) * o.minGap + o.ringStep;
    }

    // ---- place everything (centred on 0,0 first) ------------------------
    const placed = [], hubs = [], ifaceOut = [], edges = [];
    let maxR = o.ifaceRadius;
    const at = (r, a) => ({ x: r * Math.cos(a), y: r * Math.sin(a) });

    for (const s of slices) {
      const mid = (s.a0 + s.a1) / 2;
      const meta = info.get(s.key) || {};
      const p = at(o.ifaceRadius, mid);
      ifaceOut.push({ key: s.key, id: 'if:' + s.key, name: s.key === UNROUTED ? 'No route known' : s.key,
                      online: s.key === UNROUTED ? null : meta.online, rxb: meta.rxb || 0, txb: meta.txb || 0,
                      count: byIface.get(s.key).length, unrouted: s.key === UNROUTED, a0: s.a0, a1: s.a1, angle: mid, x: p.x, y: p.y });
      if (s.key !== UNROUTED) edges.push({ from: 'portal', to: 'if:' + s.key, kind: 'iface' });
    }

    for (const g of groups) {
      const ifaceId = 'if:' + g.slice.key;
      let parent = ifaceId;
      if (g.via !== DIRECT) {
        const p = at(o.hubRadius, (g.a0 + g.a1) / 2);
        const id = 'hub:' + g.slice.key + ':' + g.via;
        hubs.push({ id, via: g.via, iface: g.slice.key, count: g.nodes.length, x: p.x, y: p.y });
        edges.push({ from: ifaceId, to: id, kind: 'hub' });
        parent = id;
      }
      const rings = new Map();
      for (const n of g.nodes) {
        const r = ringOf(n);
        if (!rings.has(r)) rings.set(r, []);
        rings.get(r).push(n);
      }
      for (const [r, list] of rings) {
        const rb = ringBase.get(r);
        const capacity = Math.max(1, Math.floor(rb * g.span / o.minGap));
        for (let row = 0, i = 0; i < list.length; row++) {
          const chunk = list.slice(i, i + capacity);
          const radius = rb + row * o.minGap;
          chunk.forEach((n, j) => {
            const p = at(radius, g.a0 + (j + 0.5) * g.span / chunk.length);
            placed.push(Object.assign(n, { id: n.hash, x: p.x, y: p.y, ring: r }));
            if (g.slice.key !== UNROUTED) edges.push({ from: parent, to: n.hash, kind: 'node' });
            maxR = Math.max(maxR, radius);
          });
          i += chunk.length;
        }
      }
    }
    maxR = Math.max(maxR, o.hubRadius);

    // ---- translate so everything is positive ----------------------------
    const half = maxR + o.margin;
    const shift = p => { p.x += half; p.y += half; return p; };
    ifaceOut.forEach(shift); hubs.forEach(shift); placed.forEach(shift);
    return {
      width: half * 2, height: half * 2, cx: half, cy: half, portal: { id: 'portal', x: half, y: half },
      interfaces: ifaceOut, hubs, nodes: placed, edges, maxRadius: maxR,
      rings: ringNumbers.map(r => ({ ring: r, radius: ringBase.get(r), unknown: r === MAX_RING + 1 })),
    };
  }

  // ---- drawing ------------------------------------------------------------
  const SVG_NS = 'http://www.w3.org/2000/svg';
  function el(tag, attrs, text) {
    const e = document.createElementNS(SVG_NS, tag);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function short(h) { return h.slice(0, 8) + '…' + h.slice(-4); }
  function fmtBytes(n) { return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; }

  // Draw a layout into an <svg>. onSelect(node) is called when a destination
  // is clicked or activated with the keyboard. Returns {setKinds, zoom, reset}.
  function renderNetworkMap(svg, L, opts) {
    const o = Object.assign({ onSelect: null }, opts || {});
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute('viewBox', `0 0 ${L.width} ${L.height}`);
    svg.classList.add('nm-svg');

    const rings = el('g', { class: 'nm-rings', 'aria-hidden': 'true' });
    for (const r of L.rings) {
      rings.appendChild(el('circle', { class: 'nm-ring', cx: L.cx, cy: L.cy, r: r.radius }));
      rings.appendChild(el('text', { class: 'nm-ring-label', x: L.cx, y: L.cy - r.radius - 3, 'text-anchor': 'middle' },
        r.unknown ? 'hops unknown' : r.ring + (r.ring === 1 ? ' hop' : ' hops')));
    }
    svg.appendChild(rings);

    const pos = new Map([['portal', L.portal]]);
    L.interfaces.forEach(i => pos.set(i.id, i));
    L.hubs.forEach(h => pos.set(h.id, h));
    const kindOf = new Map(L.nodes.map(n => [n.id, n.kind]));
    L.nodes.forEach(n => pos.set(n.id, n));

    const edges = el('g', { class: 'nm-edges', 'aria-hidden': 'true' });
    for (const e of L.edges) {
      const a = pos.get(e.from), b = pos.get(e.to);
      const cls = 'nm-edge nm-edge-' + e.kind + (e.kind === 'node' ? ' nm-edge-to-' + kindOf.get(e.to) : '');
      edges.appendChild(el('line', { class: cls, x1: a.x, y1: a.y, x2: b.x, y2: b.y }));
    }
    svg.appendChild(edges);

    for (const h of L.hubs) {
      const g = el('g', { class: 'nm-hub' });
      g.appendChild(el('path', { d: `M${h.x} ${h.y - 7} L${h.x + 7} ${h.y} L${h.x} ${h.y + 7} L${h.x - 7} ${h.y} Z` }));
      g.appendChild(el('title', {}, `Next hop ${short(h.via)}: ${h.count} destination${h.count === 1 ? '' : 's'} through it`));
      svg.appendChild(g);
    }

    for (const i of L.interfaces) {
      const g = el('g', { class: 'nm-iface' + (i.unrouted ? ' nm-iface-unrouted' : i.online === false ? ' nm-iface-down' : '') });
      g.appendChild(el('rect', { x: i.x - 9, y: i.y - 9, width: 18, height: 18, rx: 4 }));
      const name = i.name.length > 22 ? i.name.slice(0, 21) + '…' : i.name;
      g.appendChild(el('text', { x: i.x, y: i.y + (i.y >= L.cy ? 24 : -14), 'text-anchor': 'middle' }, `${name} (${i.count})`));
      const state = i.unrouted ? 'no route known' : i.online === false ? 'offline' : i.online ? 'online' : 'state unknown';
      g.appendChild(el('title', {}, `${i.name}: ${state}; ${i.count} destination${i.count === 1 ? '' : 's'}` +
        (i.unrouted ? '' : `; received ${fmtBytes(i.rxb)}, sent ${fmtBytes(i.txb)}`)));
      svg.appendChild(g);
    }

    const portal = el('g', { class: 'nm-portal' });
    portal.appendChild(el('circle', { cx: L.cx, cy: L.cy, r: 15 }));
    portal.appendChild(el('text', { x: L.cx, y: L.cy + 4, 'text-anchor': 'middle' }, 'You'));
    portal.appendChild(el('title', {}, 'This portal'));
    svg.appendChild(portal);

    const nodes = el('g', { class: 'nm-nodes' });
    for (const n of L.nodes) {
      const label = `${n.name || short(n.hash)} · ${n.kind}` +
        (n.hops === null || n.hops === undefined ? '' : ` · ${n.hops} hop${n.hops === 1 ? '' : 's'}`);
      const c = el('circle', { class: 'nm-node nm-' + n.kind, cx: n.x, cy: n.y, r: 5.5, tabindex: '0', role: 'button',
        'aria-label': label, 'data-hash': n.hash });
      c.appendChild(el('title', {}, label + '\n' + n.hash));
      const pick = () => { if (o.onSelect) o.onSelect(n); };
      c.addEventListener('click', pick);
      c.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); pick(); } });
      nodes.appendChild(c);
    }
    svg.appendChild(nodes);

    // ---- zoom and pan: change the viewBox ---------------------------------
    let vb = { x: 0, y: 0, w: L.width, h: L.height };
    const apply = () => svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    const clamp = z => Math.min(L.width * 2, Math.max(L.width / 12, z));
    function zoom(factor, fx, fy) {
      const w = clamp(vb.w * factor), k = w / vb.w;
      const px = fx === undefined ? vb.x + vb.w / 2 : fx, py = fy === undefined ? vb.y + vb.h / 2 : fy;
      vb = { x: px - (px - vb.x) * k, y: py - (py - vb.y) * k, w, h: vb.h * k };
      apply();
    }
    function reset() { vb = { x: 0, y: 0, w: L.width, h: L.height }; apply(); }
    const toSvg = ev => { const r = svg.getBoundingClientRect();
      return { x: vb.x + (ev.clientX - r.left) / r.width * vb.w, y: vb.y + (ev.clientY - r.top) / r.height * vb.h }; };
    svg.onwheel = ev => { ev.preventDefault(); const p = toSvg(ev); zoom(ev.deltaY < 0 ? 0.85 : 1 / 0.85, p.x, p.y); };
    let drag = null;
    svg.onpointerdown = ev => { if (ev.target.closest && ev.target.closest('.nm-node')) return;
      drag = { x: ev.clientX, y: ev.clientY, vx: vb.x, vy: vb.y }; svg.setPointerCapture(ev.pointerId); svg.classList.add('nm-grabbing'); };
    svg.onpointermove = ev => { if (!drag) return; const r = svg.getBoundingClientRect();
      vb.x = drag.vx - (ev.clientX - drag.x) / r.width * vb.w; vb.y = drag.vy - (ev.clientY - drag.y) / r.height * vb.h; apply(); };
    const end = () => { drag = null; svg.classList.remove('nm-grabbing'); };
    svg.onpointerup = end; svg.onpointercancel = end;

    function setKinds(kinds) {
      for (const k of ['site', 'peer', 'relay']) svg.classList.toggle('nm-hide-' + k, !kinds.has(k));
    }
    return { zoom, reset, setKinds, viewBox: () => Object.assign({}, vb) };
  }

  const api = { layoutNetworkMap, renderNetworkMap, UNROUTED };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.layoutNetworkMap = layoutNetworkMap;
  root.renderNetworkMap = renderNetworkMap;
  root.NETMAP_UNROUTED = UNROUTED;
})(typeof window !== 'undefined' ? window : globalThis);

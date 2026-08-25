/* NomadPortal frontend — vanilla JS, no dependencies */
'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  history: [],
  historyIndex: -1,
  activeNodeHash: null,
  activePath: '/',
  loading: false,
};

// Page-level favorites: array of {hash, path, name, added, is_hosted}
let _favorites = [];

// Node hashes the current user has toggled persistent identification on for.
let _identifiedNodes = new Set();

// Last successful page fetch — kept so the Raw toggle can swap views
// without re-fetching from the node.
let _lastPage = { url: '', hash: '', path: '', html: '', micron: '' };

// ---------------------------------------------------------------------------
// Identicon — deterministic per-hash fallback avatar (GitHub/Columba-
// style symmetric dot grid), used whenever a node or contact has no
// explicit icon. Ported from the NomadPortal-Android sister project's
// Identicon.kt (itself ported from Columba, torlando-tech/columba) —
// same algorithm, re-expressed as an SVG string instead of a Compose
// Canvas, for direct innerHTML insertion (inline SVG, not a data: URI —
// keeps it crisp at any size and able to use this app's own CSS
// variables for the background, unlike _contactIcon's <img> path for a
// server-rendered icon).
//
// hash[0..2] become the primary color's RGB, hash[3..5] the secondary's
// (raw byte values, no palette/HSL). A 5-row grid only computes its left
// 3 columns — hash[(row*3+col) % hash.length] — a dot is drawn (primary
// if that byte is even, secondary if odd) whenever the byte exceeds 127,
// then columns 0-1 are mirrored onto columns 4-3 for left-right symmetry
// (column 2 is the untouched center axis). A hash under 6 bytes can't
// feed both colors, so it renders a plain grey circle instead, matching
// the degenerate case in the original.
//
// ringColor draws a thin colored stroke around the circle when set —
// same "kind" indicator Android's Identicon.kt already has (see its own
// ringColor doc comment): a real, distinct color per *kind of thing*
// (site / contact / relay), not left to the hash-derived dot colors to
// carry any of that meaning. Matches Android's own hex values exactly
// (_RING_* below) so the two apps read as the same visual language.
// Undefined/falsy draws no ring, the original plain look.
// ---------------------------------------------------------------------------
const _RING_SITE  = '#9B6BC8'; // purple — matches Android's NomadPortalPurple
const _RING_PEER  = '#5BC8C8'; // teal — matches Android's NomadIdenticonRingContact
const _RING_RELAY = '#C8C85B'; // gold — matches Android's NomadIdenticonRingRelay

function _identiconSvg(hashHex, size = 24, ringColor = null) {
  const hex   = (hashHex || '').replace(/[^0-9a-f]/gi, '');
  const bytes = [];
  for (let i = 0; i + 1 < hex.length; i += 2) bytes.push(parseInt(hex.substr(i, 2), 16));

  const open = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" ` +
               `width="${size}" height="${size}" style="display:block;flex-shrink:0;border-radius:50%;">`;
  // Stroke straddles the circle's own path, so the radius backs off by
  // half the stroke width (plus a hair of margin) to stay inside the
  // 32x32 viewBox rather than clipping at the edge.
  const ring = ringColor ? ` stroke="${ringColor}" stroke-width="1.5"` : '';
  const r    = ringColor ? 15 : 16;

  if (bytes.length < 6) {
    return `${open}<circle cx="16" cy="16" r="${r}" fill="#808080"${ring}/></svg>`;
  }

  const primary   = `rgb(${bytes[0]},${bytes[1]},${bytes[2]})`;
  const secondary = `rgb(${bytes[3]},${bytes[4]},${bytes[5]})`;
  const cell = 32 / 5;
  let dots = '';
  for (let row = 0; row < 5; row++) {
    for (let col = 0; col < 3; col++) {
      const val = bytes[(row * 3 + col) % bytes.length];
      if (val <= 127) continue;
      const color = (val % 2 === 0) ? primary : secondary;
      const cr = (cell / 2.5).toFixed(2);
      const cy = (row * cell + cell / 2).toFixed(2);
      const cx = (col * cell + cell / 2).toFixed(2);
      dots += `<circle cx="${cx}" cy="${cy}" r="${cr}" fill="${color}"/>`;
      if (col < 2) {
        const mx = ((4 - col) * cell + cell / 2).toFixed(2);
        dots += `<circle cx="${mx}" cy="${cy}" r="${cr}" fill="${color}"/>`;
      }
    }
  }
  return `${open}<circle cx="16" cy="16" r="${r}" fill="var(--bg3)"${ring}/>${dots}</svg>`;
}

// ---------------------------------------------------------------------------
// Contact icon — real image from FIELD_ICON_APPEARANCE, or an identicon
// fallback keyed to the contact's own hash (was blank space before —
// every contact is now visually distinct at a glance without needing an
// icon explicitly set on either end).
// ---------------------------------------------------------------------------
function _contactIcon(contact, size = 24) {
  // Ring only applies to the identicon fallback below, not a real icon
  // image — matches Android's ContactAvatar exactly (ringColor is never
  // passed on its ContactIcon.Appearance/successful-RawImage branches,
  // only its own Identicon-fallback ones). A real icon is already
  // visually distinct; the ring's job is telling "no custom icon"
  // contacts apart from sites/relays at a glance, not decorating every
  // avatar regardless of content.
  if (!contact?.icon) return contact?.hash ? _identiconSvg(contact.hash, size, _RING_PEER) : '';
  const mime = contact.icon_mime || 'image/png';
  const r    = Math.round(size * 0.12);
  return `<img src="data:${mime};base64,${contact.icon}" ` +
         `width="${size}" height="${size}" ` +
         `style="display:block;flex-shrink:0;border-radius:${r}px;object-fit:cover;">`;
}

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const $ = id => document.getElementById(id);
const addrBar      = $('address-bar');
const btnGo        = $('btn-go');
const btnBack      = $('btn-back');
const btnFwd       = $('btn-forward');
const btnRefresh   = $('btn-refresh-page');
const nodeList     = $('node-list');
const nodeFilter   = $('node-filter');
const nodeSort     = $('node-sort');
const nodeCount    = $('node-count');
const pageContent  = $('page-content');
const pageError    = $('page-error');
const loadingOver  = $('loading-overlay');
const pageNodeName = $('page-node-name');
const pagePath     = $('page-path');
const statusText   = $('status-text');
const cacheInfo    = $('cache-info');
const statusDot    = $('status-indicator');
const toggleRaw    = $('toggle-raw');
const toggleWrap   = $('toggle-wrap');

// ---------------------------------------------------------------------------
// Network helpers
// ---------------------------------------------------------------------------
const _csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';

async function apiFetch(url, opts = {}) {
  if (typeof url !== 'string') {
    throw new Error('apiFetch: url must be a string');
  }
  // Same-origin guard.  The dataflow through CodeQL's
  // js/client-side-request-forgery rule needs to be visibly
  // collapsed: the value handed to fetch() must be derived from a
  // strict allow-list, not the raw caller input.  We parse the
  // candidate with an explicit base, verify origin equality, then
  // require the pathname to match a strict ASCII regex (no control
  // chars, no backslashes, no protocol-relative sequences).  The
  // regex test is the recognised sanitisation barrier; the value
  // forwarded downstream is recomposed from .pathname/.search/.hash
  // of the parsed URL — never the original input string.
  let parsed;
  try {
    parsed = new URL(url, window.location.origin);
  } catch (_) {
    throw new Error('apiFetch: invalid URL');
  }
  if (parsed.origin !== window.location.origin) {
    throw new Error(`apiFetch: cross-origin URL rejected (${parsed.origin})`);
  }
  // Strict ASCII path allow-list; rejects anything that could decode
  // into an alternative origin or smuggle control chars into the
  // fetch target.
  const SAFE_PATH_RE = /^\/[A-Za-z0-9._~!$&'()*+,;=:@/%?#-]*$/;
  const candidate = parsed.pathname + parsed.search + parsed.hash;
  if (!SAFE_PATH_RE.test(candidate)) {
    throw new Error('apiFetch: path contains disallowed characters');
  }
  const safeUrl = candidate;
  const { headers: extraHeaders = {}, ...restOpts } = opts;
  const res = await fetch(safeUrl, {
    headers: {
      'Accept': 'application/json',
      'X-CSRF-Token': _csrfToken,
      ...extraHeaders,
    },
    ...restOpts,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.description || body.error || `HTTP ${res.status}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Node list
// ---------------------------------------------------------------------------
let _allNodes = [];
let _favCollapsed = false;

// ---------------------------------------------------------------------------
// Windowed list rendering — shared by the node list and the LXMF peer
// (Users tab) list below. A real mesh can announce thousands of nodes/
// peers; rendering every matched/sorted row into the DOM unconditionally
// on every render was a real, reported stall opening either list. Same
// fix as the Android sister project's rememberWindowedList()/
// LoadMoreTrigger (ui/components/WindowedList.kt), ported here as plain
// closures instead of a Composable: most people only ever look at the
// most recent ~50 of a list, so only ever build that many rows up
// front, growing one more page only once the user actually scrolls (or
// clicks) for more. Two rules, matching the Android version exactly:
// - Page 1 stays live — it rebuilds fresh from the source array on
//   every render, so a newly-discovered node/peer still shows up
//   immediately at the top.
// - The moment a second page loads, the whole window freezes against a
//   snapshot, so rows the user already scrolled past don't reorder or
//   shift underneath them. A changed resetKey (new filter/sort) snaps
//   back to page 1 and live tracking resumes.
// ---------------------------------------------------------------------------
const LIST_PAGE_SIZE = 50;

function _windowList(items, winState, resetKey) {
  if (resetKey !== winState.resetKey) {
    winState.resetKey = resetKey;
    winState.page = 1;
    winState.frozen = null;
  }
  const effective = winState.frozen || items;
  const visible = effective.slice(0, winState.page * LIST_PAGE_SIZE);
  return {
    visible,
    remaining: effective.length - visible.length,
    loadMore() {
      if (winState.frozen === null) winState.frozen = items;
      winState.page += 1;
    },
  };
}

// "Show N more…" sentinel row — click it, or scroll it into view, to
// grow a windowed list by one more page. `tag` matches the caller's own
// row element (<li> for #node-list, <div> for the peer list). `root`
// must be the list's real scrolling ancestor, not the viewport — these
// panels scroll inside their own overflow-y:auto container — so the
// IntersectionObserver fires as the user nears the actual bottom.
function _makeLoadMoreRow(remaining, root, onLoad, tag = 'li') {
  const el = document.createElement(tag);
  el.className = 'list-load-more';
  el.textContent = `Show ${Math.min(remaining, LIST_PAGE_SIZE)} more (${remaining} left)…`;
  let fired = false;
  const fire = () => {
    if (fired) return;
    fired = true;
    onLoad();
  };
  el.addEventListener('click', fire);
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {
        io.disconnect();
        fire();
      }
    }, { root, rootMargin: '200px' });
    io.observe(el);
  }
  return el;
}

let _nodeListWindow = { page: 1, frozen: null, resetKey: null };

async function refreshNodes() {
  try {
    const data = await apiFetch('/api/nodes');
    _allNodes = data.nodes || [];
    renderNodeList();
    nodeCount.textContent = `${_allNodes.length} node${_allNodes.length !== 1 ? 's' : ''}`;
    setStatus(`${_allNodes.length} node(s) discovered`, 'ok');
  } catch (e) {
    setStatus(`Node refresh failed: ${e.message}`, 'error');
  }
}

function renderNodeList() {
  const filter = nodeFilter.value.trim().toLowerCase();
  const visible = filter
    ? _allNodes.filter(n =>
        n.name.toLowerCase().includes(filter) ||
        n.hash.toLowerCase().includes(filter))
    : _allNodes;

  if (visible.length === 0) {
    nodeList.innerHTML = '<li class="node-placeholder">No nodes found…</li>';
    return;
  }

  nodeList.innerHTML = '';

  // Auto-favorites: hosted node + operator-configured default node. These
  // are surfaced in the Favorites section for every audience (guests too)
  // — `is_hosted` / `is_default` come from the backend and survive guest
  // stripping. Hosted always sorts before default if both exist.
  const autoFavs = visible.filter(n => n.is_hosted || n.is_default);
  autoFavs.sort((a, b) => (b.is_hosted ? 1 : 0) - (a.is_hosted ? 1 : 0));
  const autoFavHashes = new Set(autoFavs.map(n => n.hash));

  // Mixed favorites: nodes (path "/") render via the existing node row;
  // page bookmarks render with the custom name + path subtitle.
  const showFavSection = _authState.logged_in
    || autoFavs.length > 0
    || _favorites.some(f => f.is_hosted);
  if (showFavSection) {
    const hdr = document.createElement('li');
    hdr.className = 'node-section-header collapsible';
    hdr.innerHTML =
      `<span class="section-toggle">${_favCollapsed ? '▸' : '▾'}</span>Favorites`;
    hdr.addEventListener('click', () => {
      _favCollapsed = !_favCollapsed;
      renderNodeList();
    });
    nodeList.appendChild(hdr);
    if (!_favCollapsed) {
      // Auto-favorites first (hosted, default).
      for (const node of autoFavs) {
        nodeList.appendChild(makeNodeItem(node));
      }
      // User page bookmarks after.
      if (autoFavs.length === 0 && _favorites.length === 0) {
        const empty = document.createElement('li');
        empty.className = 'node-placeholder';
        empty.style.cssText = 'font-size:11px;padding:4px 12px;';
        empty.textContent = 'No favorites yet — click ☆ on any node or page.';
        nodeList.appendChild(empty);
      } else {
        for (const fav of _favorites) {
          // Skip page bookmarks that point at an auto-fav node's index —
          // we'd be double-rendering it otherwise.
          if ((fav.path || '/') === '/' && autoFavHashes.has(fav.hash)) continue;
          if ((fav.path || '/') === '/') {
            // Index favorite: render as a normal node row using the live node
            // data when available, otherwise a stub built from the favorite.
            const node = visible.find(n => n.hash === fav.hash) || {
              hash:        fav.hash,
              name:        fav.name,
              last_seen:   0,
              hops:        null,
              is_hosted:   !!fav.is_hosted,
              favorited:   true,
              last_load_ok: null,
            };
            nodeList.appendChild(makeNodeItem(node));
          } else {
            nodeList.appendChild(makePageFavItem(fav));
          }
        }
      }
    }
  }

  const hdr2 = document.createElement('li');
  hdr2.className = 'node-section-header';
  hdr2.textContent = 'Nodes';
  nodeList.appendChild(hdr2);

  // Sort key selected via the sidebar dropdown. Defaults to "last_seen"
  // (descending = most-recent first) so existing users see no change
  // until they pick a different option.
  //
  // - "last_seen":   newest announce first (existing behaviour)
  // - "name":        case-insensitive A → Z by node name
  // - "hops":        closest first (unknown hops sink to the end)
  // - "announces":   most-active first (RNS counter via total_announces)
  const sortKey = (nodeSort && nodeSort.value) || 'last_seen';
  const sortedNodes = [...visible].filter(n => !autoFavHashes.has(n.hash));
  if (sortKey === 'name') {
    sortedNodes.sort((a, b) =>
      (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase()));
  } else if (sortKey === 'hops') {
    sortedNodes.sort((a, b) => {
      const ah = a.hops == null ? Infinity : a.hops;
      const bh = b.hops == null ? Infinity : b.hops;
      return ah - bh;
    });
  } else if (sortKey === 'announces') {
    sortedNodes.sort((a, b) => (b.announce_count || 0) - (a.announce_count || 0));
  } else {
    sortedNodes.sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
  }

  // Windowed — see _windowList's own doc comment. resetKey covers both
  // criteria this section depends on (filter text + sort key); changing
  // either snaps back to page 1.
  const w = _windowList(sortedNodes, _nodeListWindow, `${filter}|${sortKey}`);
  for (const node of w.visible) nodeList.appendChild(makeNodeItem(node));
  if (w.remaining > 0) {
    nodeList.appendChild(_makeLoadMoreRow(w.remaining, nodeList, () => {
      w.loadMore();
      renderNodeList();
    }, 'li'));
  }
}

// Shared with the Network tab's site rows (makeNetworkItem) — one
// definition of "what does last-access status look like" rather than
// two copies that could drift.
function _nodeStatusDot(node) {
  return node.last_load_ok === true
    ? '<span class="node-dot node-dot-ok"       title="Last access succeeded">●</span>'
    : node.last_load_ok === false && node.ever_load_ok
    ? '<span class="node-dot node-dot-degraded" title="Last access failed — has worked before">◑</span>'
    : node.last_load_ok === false
    ? '<span class="node-dot node-dot-err"      title="Last access failed — never successfully loaded">✕</span>'
    : '<span class="node-dot node-dot-none"     title="Never accessed">○</span>';
}

function makeNodeItem(node) {
  const li = document.createElement('li');
  li.dataset.hash = node.hash;
  if (node.hash === state.activeNodeHash) li.classList.add('active');
  const age = formatAge(node.last_seen);
  const dot = _nodeStatusDot(node);

  // hops badge — `null` from server means "unreachable / unknown route"
  const hopsLabel = (node.hops === null || node.hops === undefined)
    ? '?'
    : node.hops === 0
      ? 'local'
      : node.hops === 1 ? '1 hop' : `${node.hops} hops`;
  const hopsClass = (node.hops === null || node.hops === undefined)
    ? 'node-hops node-hops-unknown'
    : 'node-hops';
  const hopsHTML =
    `<span class="${hopsClass}" title="Hops away on the Reticulum network">${hopsLabel}</span>`;

  // Right-side stack: star (or pin) above, hops badge below
  const right = document.createElement('div');
  right.className = 'node-right';

  if (node.is_hosted || node.is_default) {
    const pin = document.createElement('span');
    pin.className = 'node-fav-btn fav-active';
    pin.textContent = '★';
    pin.title = node.is_hosted
      ? 'This node (always pinned)'
      : 'Operator-pinned default node';
    pin.style.cursor = 'default';
    right.appendChild(pin);
  } else if (_authState.logged_in) {
    const starBtn = document.createElement('button');
    starBtn.className = 'node-fav-btn';
    starBtn.dataset.hash = node.hash;
    starBtn.dataset.fav  = node.favorited ? 'true' : 'false';
    starBtn.textContent  = node.favorited ? '★' : '☆';
    starBtn.classList.toggle('fav-active', !!node.favorited);
    starBtn.title        = node.favorited ? 'Unfavorite' : 'Favorite';
    starBtn.addEventListener('click', e => {
      e.stopPropagation();
      toggleFavorite(node.hash, starBtn.dataset.fav !== 'true');
    });
    right.appendChild(starBtn);
  }
  right.insertAdjacentHTML('beforeend', hopsHTML);
  li.appendChild(right);

  const isLocked = _lockedHash && !_isTrustedHash(node.hash);
  if (isLocked) li.classList.add('node-locked');

  li.insertAdjacentHTML('beforeend',
    `<div class="node-icon-row">` +
      `<span class="node-identicon">${_identiconSvg(node.hash, 22, _RING_SITE)}</span>` +
      `<div class="node-text">` +
        `<span class="node-name">${dot}${esc(node.name)}</span>` +
        `<span class="node-hash">${node.hash.slice(0, 24)}…</span>` +
        `<span class="node-age">${age}</span>` +
      `</div>` +
    `</div>`);

  li.addEventListener('click', e => {
    if (e.target.closest('.node-fav-btn')) return;
    if (isLocked) return;
    navigateTo(`hash://${node.hash}/page/index.mu`);
  });
  return li;
}

async function toggleFavorite(hash, newVal) {
  const node = _allNodes.find(n => n.hash === hash);
  if (!node) return;
  node.favorited = newVal;
  renderNodeList();
  try {
    await apiFetch(`/api/nodes/${hash}/favorite`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ favorited: newVal }),
    });
    await loadFavorites();
  } catch (e) {
    node.favorited = !newVal;
    renderNodeList();
    setStatus(`Could not save favorite: ${e.message}`, 'error');
  }
}

// Render a favorite that points at a specific page (path != "/").
function makePageFavItem(fav) {
  const li = document.createElement('li');
  li.dataset.hash = fav.hash;
  if (fav.hash === state.activeNodeHash && fav.path === state.activePath) {
    li.classList.add('active');
  }

  const right = document.createElement('div');
  right.className = 'node-right';
  const starBtn = document.createElement('button');
  starBtn.className = 'node-fav-btn fav-active';
  starBtn.textContent = '★';
  starBtn.title = 'Remove bookmark';
  starBtn.addEventListener('click', e => {
    e.stopPropagation();
    removePageFavorite(fav.hash, fav.path);
  });
  right.appendChild(starBtn);
  li.appendChild(right);

  // Wrapped in .node-text (flex:1; min-width:0) same as makeNodeItem's
  // own row — without it these two spans are direct flex children with
  // no shrink floor, so a long bookmark name/path can't ellipsis and
  // instead pushes the row into horizontal overflow.
  li.insertAdjacentHTML('beforeend',
    `<div class="node-text">` +
      `<span class="node-name">${esc(fav.name)}</span>` +
      `<span class="node-hash">${fav.hash.slice(0, 12)}…${esc(fav.path)}</span>` +
    `</div>`);

  li.addEventListener('click', e => {
    if (e.target.closest('.node-fav-btn')) return;
    navigateTo(`hash://${fav.hash}${fav.path}`);
  });
  return li;
}

async function loadFavorites() {
  if (!_authState.logged_in) {
    _favorites = [];
    updateFavPageButton();
    return;
  }
  try {
    const res = await apiFetch('/api/favorites');
    _favorites = res.favorites || [];
  } catch (e) {
    _favorites = [];
  }
  updateFavPageButton();
  renderNodeList();
}

function _isCurrentPageFavorited() {
  if (!state.activeNodeHash) return false;
  const path = state.activePath || '/';
  return _favorites.some(f =>
    f.hash === state.activeNodeHash && (f.path || '/') === path
  );
}

function updateFavPageButton() {
  const btn = $('btn-fav-page');
  if (!btn) return;
  const show = _authState.logged_in && !!state.activeNodeHash;
  btn.hidden = !show;
  if (!show) return;
  const isFav = _isCurrentPageFavorited();
  btn.textContent = isFav ? '★' : '☆';
  btn.classList.toggle('fav-active', isFav);
  btn.title = isFav ? 'Remove bookmark' : 'Bookmark this page';
}

async function toggleCurrentPageFavorite() {
  if (!_authState.logged_in || !state.activeNodeHash) return;
  const hash = state.activeNodeHash;
  const path = state.activePath || '/';
  const isFav = _isCurrentPageFavorited();

  if (isFav) {
    await removePageFavorite(hash, path);
    return;
  }

  // Pre-fill with "<NodeName> – <path>" for non-index pages, else just the
  // node name. Browser-style prompt — matches the lightweight bookmark UX.
  const node = _allNodes.find(n => n.hash === hash);
  const baseName = (node && node.name) || hash.slice(0, 12);
  const defaultName = path === '/' ? baseName : `${baseName} – ${path}`;
  const name = window.prompt('Bookmark name:', defaultName);
  if (name === null) return;          // user cancelled
  const trimmed = name.trim();
  if (!trimmed) return;

  try {
    await apiFetch('/api/favorites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hash, path, name: trimmed }),
    });
    await loadFavorites();
    setStatus('Bookmark saved.', 'ok');
  } catch (e) {
    setStatus(`Could not save bookmark: ${e.message}`, 'error');
  }
}

async function removePageFavorite(hash, path) {
  try {
    await apiFetch('/api/favorites', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hash, path }),
    });
    await loadFavorites();
  } catch (e) {
    setStatus(`Could not remove bookmark: ${e.message}`, 'error');
  }
}

function formatAge(ts) {
  const secs = Math.floor(Date.now() / 1000 - ts);
  if (secs < 60)   return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  return `${Math.floor(secs / 3600)}h ago`;
}

// ---------------------------------------------------------------------------
// Lockdown state  (set during init, read-only after that)
// ---------------------------------------------------------------------------
let _lockedHash  = null;   // truthy when access_mode restricts this user (value is the effective default for back-compat)
let _hostedHash  = null;   // node hash of this server's hosted site (if any)
let _defaultHash = null;   // operator-configured default node (may differ from _hostedHash)
let _externalWarningAccepted = false; // logged-in users accept once per session for all external nodes
let _allowGuestExternalBrowse = false; // set by ALLOW_GUEST_EXTERNAL_BROWSE env var at install time

// Trusted-local set: built-in node + operator-configured default. Used by
// both the lockdown check (these nodes stay reachable under lockdown) and
// the external-warning check (no popup when navigating between them).
function _isTrustedHash(hash) {
  if (!hash) return false;
  const h = hash.toLowerCase();
  return (_hostedHash && _hostedHash.toLowerCase() === h) ||
         (_defaultHash && _defaultHash.toLowerCase() === h);
}

function _extractNodeHash(url) {
  // Trailing slash is optional — `hash://<hash>` with no path (e.g. a bare
  // hash typed into the address bar) must still match, otherwise the
  // lockdown/external-warning check in navigateTo() never runs for it.
  const m = url.match(/hash:\/\/([0-9a-f]+)(?:\/|$)/i) ||
            url.match(/hash:\/([0-9a-f]+)(?:\/|$)/i) ||
            url.match(/nomadnetwork:\/\/([0-9a-f]+)(?:\/|$)/i);
  return m ? m[1].toLowerCase() : null;
}

/**
 * Normalise an address-bar entry to the canonical `hash://<hash>/<path>`
 * form used internally. Accepts:
 *   hash://<hash>/<path>          — already canonical, returned as-is
 *   hash:/<hash>/<path>           — single-slash variant, returned as-is
 *   nomadnetwork://<hash>/<path>  — alternate scheme, returned as-is
 *   <hash>:/<path>                — MeshChat-style colon separator
 *   <hash>/<path>                 — bare hex hash with slash separator
 *   <hash>                        — bare hex hash, no path
 */
function _normaliseAddress(input) {
  const s = (input || '').trim();
  if (!s) return s;
  if (/^(hash:\/\/|hash:\/[0-9a-f]|nomadnetwork:\/\/)/i.test(s)) return s;
  // MeshChat-style "<hash>:/path"
  const colonMatch = s.match(/^([0-9a-fA-F]{2,128}):(\/.*)?$/);
  if (colonMatch) return 'hash://' + colonMatch[1] + (colonMatch[2] || '');
  // Slash-separated or bare hash
  if (/^[0-9a-fA-F]{2,128}(\/.*)?$/.test(s)) return 'hash://' + s;
  return s;
}

/**
 * Convert a canonical `hash://<hash>/<path>` URL to the browser-URL
 * form used in ``window.location.pathname``. Rules:
 *   - Default-node URLs collapse the hash: ``hash://<default>/page/x``
 *     becomes ``/page/x`` (bare path). Bookmark-friendly and matches
 *     the URL you'd expect when you're "on the default site."
 *   - Non-default hashes keep the hash under an ``/n/`` prefix:
 *     ``hash://<other>/page/x`` becomes ``/n/<other>/page/x``.
 *   - Root of the default node is ``/``.
 * Returns null if the input isn't a recognisable ``hash://`` URL —
 * caller shouldn't push those to browser history (fetchPage still
 * runs, but the address bar stays whatever it was).
 */
function _urlToPathname(url) {
  if (!url) return null;
  const m = url.match(/^hash:\/\/([0-9a-f]+)(\/.*)?$/i) ||
            url.match(/^hash:\/([0-9a-f]+)(\/.*)?$/i) ||
            url.match(/^nomadnetwork:\/\/([0-9a-f]+)(\/.*)?$/i);
  if (!m) return null;
  const hash = m[1].toLowerCase();
  let path = m[2] || '/';
  if (path === '/') path = '';  // ``/`` and ``/page/index.mu`` render the same page; prefer the shorter
  if (_defaultHash && hash === _defaultHash.toLowerCase()) {
    return path || '/';
  }
  return '/n/' + hash + path;
}

/**
 * Inverse of ``_urlToPathname``. Translates a browser
 * ``window.location.pathname`` back to the canonical ``hash://...``
 * URL that ``navigateTo`` expects. Uses ``_defaultHash`` for any
 * path that isn't under ``/n/``.
 *
 * Returns null when we can't build a URL — either because the
 * default hash isn't known yet (RNS still coming up) or the path
 * matches one of the SPA's own reserved shells (``/page`` served
 * as the SPA entry point). Callers fall back to their default
 * boot behaviour in that case.
 */
function _pathnameToUrl(pathname) {
  if (!pathname || pathname === '/') {
    // Bare root — the default node's index. Only meaningful once
    // we know the default hash; boot flow handles the null case.
    if (!_defaultHash) return null;
    return 'hash://' + _defaultHash + '/page/index.mu';
  }
  // Explicit external-node form: /n/<hash>[/path]
  const ext = pathname.match(/^\/n\/([0-9a-f]{2,128})(\/.*)?$/i);
  if (ext) {
    const path = ext[2] || '/page/index.mu';
    return 'hash://' + ext[1].toLowerCase() + path;
  }
  // /page served as SPA entry (from the ``?url=`` boot flow); not a real path
  if (pathname === '/page' || pathname === '/page/') return null;
  // Anything else is a default-node path — /page/foo.mu, /file/x.pdf, etc.
  if (!_defaultHash) return null;
  return 'hash://' + _defaultHash + pathname;
}

/**
 * Push or replace the browser's URL to reflect the given canonical
 * ``hash://`` URL. Silently no-ops if we can't translate the URL
 * (see ``_urlToPathname``) so navigation to weird internal states
 * doesn't blow away a good URL bar.
 */
function _syncBrowserUrl(url, replace) {
  const pathname = _urlToPathname(url);
  if (!pathname) return;
  const current = window.location.pathname + window.location.search;
  if (pathname === current) return;
  try {
    if (replace) {
      window.history.replaceState({ url }, '', pathname);
    } else {
      window.history.pushState({ url }, '', pathname);
    }
  } catch (_) { /* pushState can throw under weird sandboxing */ }
}

/**
 * Convert a canonical `hash://<hash>/<path>` URL to the MeshChat-style
 * `<hash>:/<path>` shown in the address bar. Internal state keeps the
 * canonical form; only the user-facing display is shortened so users
 * can copy/paste address strings between MeshChat and NomadPortal.
 */
function _displayAddress(url) {
  if (!url) return '';
  let s = String(url)
    .replace(/^hash:\/\//i, '')
    .replace(/^hash:\/(?=[0-9a-f])/i, '')
    .replace(/^nomadnetwork:\/\//i, '');
  // "<hash>/path" → "<hash>:/path" so the colon separates hash from path.
  // Bare "<hash>" with no path passes through unchanged.
  const m = s.match(/^([0-9a-f]{2,128})(\/.*)$/i);
  if (m) return m[1] + ':' + m[2];
  return s;
}

// ---------------------------------------------------------------------------
// Per-page auto-refresh with form-data persistence
// ---------------------------------------------------------------------------
// User picks an interval from the breadcrumb dropdown. We capture all
// <input>/<select>/<textarea> values on the current page before
// re-navigating to the same history entry, then re-inject those values
// after the new HTML renders. Useful for forum boards, status pages, and
// any page that wants both auto-update AND in-flight form input.
//
// Timer resets on navigation — picking "every 5 min" on page A and
// navigating to page B turns the timer off automatically (you didn't
// ask for page B to refresh).

const _autoReload = {
  intervalSec: 0,
  remainingSec: 0,
  tickHandle: null,
  pendingFormState: null,
};

function _stopAutoReload() {
  if (_autoReload.tickHandle) {
    clearInterval(_autoReload.tickHandle);
    _autoReload.tickHandle = null;
  }
  _autoReload.intervalSec = 0;
  _autoReload.remainingSec = 0;
  const chip = $('page-autoreload-countdown');
  if (chip) { chip.hidden = true; chip.textContent = ''; }
}

function _captureFormState() {
  const out = {};
  const root = $('page-content');
  if (!root) return out;
  // Index by name attribute (falls back to id, then positional) so
  // restoration is robust even if the new render slightly reorders.
  root.querySelectorAll('input, select, textarea').forEach((el, i) => {
    const key = el.name || el.id || `_idx${i}`;
    if (el.type === 'checkbox' || el.type === 'radio') {
      out[key] = out[key] || {};
      out[key][el.value] = el.checked;
    } else {
      out[key] = el.value;
    }
  });
  return out;
}

function _restoreFormState(saved) {
  if (!saved) return;
  const root = $('page-content');
  if (!root) return;
  root.querySelectorAll('input, select, textarea').forEach((el, i) => {
    const key = el.name || el.id || `_idx${i}`;
    const v = saved[key];
    if (v == null) return;
    if (el.type === 'checkbox' || el.type === 'radio') {
      if (typeof v === 'object' && v[el.value] != null) {
        el.checked = !!v[el.value];
      }
    } else if (typeof v === 'string') {
      el.value = v;
    }
  });
}

function _autoReloadTick() {
  if (_autoReload.remainingSec <= 0) return;
  _autoReload.remainingSec -= 1;
  const chip = $('page-autoreload-countdown');
  if (chip) chip.textContent = `refresh in ${_autoReload.remainingSec}s`;
  if (_autoReload.remainingSec > 0) return;
  // Fire the reload — capture form state, navigate to the current
  // history entry without pushing it, then re-arm for the next cycle.
  _autoReload.pendingFormState = _captureFormState();
  const current = state.history[state.historyIndex];
  if (!current) {
    _stopAutoReload();
    return;
  }
  Promise.resolve(navigateTo(current, false)).then(() => {
    _autoReload.remainingSec = _autoReload.intervalSec;
    if (chip && _autoReload.intervalSec) {
      chip.textContent = `refresh in ${_autoReload.remainingSec}s`;
    }
  });
}

function _startAutoReload(seconds) {
  _stopAutoReload();
  if (!seconds || seconds < 1) return;
  _autoReload.intervalSec  = seconds;
  _autoReload.remainingSec = seconds;
  const chip = $('page-autoreload-countdown');
  if (chip) {
    chip.hidden = false;
    chip.textContent = `refresh in ${seconds}s`;
  }
  _autoReload.tickHandle = setInterval(_autoReloadTick, 1000);
}

// ---------------------------------------------------------------------------
// Breadcrumb + per-page network diagnostics
// ---------------------------------------------------------------------------
// Surfaces hops + next-hop interface for the current page's destination
// in the breadcrumb strip above #page-content. The ping button issues a
// live link-establishment latency measurement on demand (requires login;
// rate-limited server-side).

const _bc = {
  el:   () => $('page-breadcrumb'),
  hops: () => $('page-diag-hops'),
  iface:() => $('page-diag-iface'),
  ping: () => $('page-diag-ping'),
  btn:  () => $('btn-page-ping'),
};

function _hopsLabel(hops) {
  if (hops == null) return 'no route';
  if (hops === 0)  return 'local';
  return hops === 1 ? '1 hop' : `${hops} hops`;
}

async function _updateBreadcrumb(hash) {
  const bar = _bc.el();
  if (!bar) return;
  bar.hidden = false;
  // Always reset the ping chip on a page change; the btn-page-ping
  // dataset.hash gets re-stamped so the next click measures the right
  // destination.
  const pingChip = _bc.ping(); if (pingChip) { pingChip.hidden = true; pingChip.textContent = ''; }
  const btn = _bc.btn();
  if (btn) {
    btn.hidden  = !hash;
    btn.disabled = false;
    btn.dataset.hash = hash || '';
  }
  if (!hash) return;
  try {
    const diag = await apiFetch(`/api/nodes/${hash}/diagnostics`);
    const hopsChip = _bc.hops();
    if (hopsChip) {
      hopsChip.textContent = _hopsLabel(diag.hops);
      hopsChip.hidden = false;
    }
    const ifaceChip = _bc.iface();
    if (ifaceChip) {
      if (diag.next_hop_iface) {
        ifaceChip.textContent = `via ${diag.next_hop_iface}`;
        ifaceChip.hidden = false;
      } else {
        ifaceChip.hidden = true;
      }
    }
  } catch (_) {
    // Diagnostics are decorative — silent on failure.
  }
}

async function _pingCurrentPage() {
  const btn = _bc.btn();
  if (!btn || btn.disabled) return;
  const hash = btn.dataset.hash;
  if (!hash) return;
  btn.disabled = true;
  const chip = _bc.ping();
  if (chip) { chip.hidden = false; chip.textContent = 'pinging…'; }
  try {
    const res = await apiFetch(`/api/nodes/${hash}/ping`, { method: 'POST' });
    if (chip) chip.textContent = `${res.ms} ms`;
  } catch (e) {
    if (chip) chip.textContent = `ping failed: ${e.message || e}`;
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------
async function navigateTo(url, pushHistory = true, extraFields = null) {
  if (!url) return false;
  url = url.trim();

  // External node check — warning and/or lockdown.
  // Admins are exempt entirely.
  if (!_authState.is_admin) {
    const targetHash = _extractNodeHash(url);
    const isGuest    = !_authState.logged_in;

    if (targetHash) {
      // Operator-trusted nodes (built-in + configured default) are always
      // reachable. They don't trigger the external-node warning, and they
      // aren't blocked by lockdown — the operator deliberately surfaced
      // them to visitors. Lockdown blocks everything outside this set.
      const isTrusted = _isTrustedHash(targetHash);

      if (_lockedHash && !isTrusted) {
        window.alert(
          'External NomadNet content is not available to guests on this site.\n\n' +
          'Sign in if you have an account, or contact the site operator.'
        );
        return false;
      }

      // Not locked, going somewhere outside the trusted set → warn.
      // Guests: warn every time (unless operator explicitly disabled it at install).
      // Logged-in users: warn once per session, applies to all external nodes.
      if (!_lockedHash && !isTrusted) {
        const guestShouldWarn = isGuest && !_allowGuestExternalBrowse;
        if (guestShouldWarn || !_externalWarningAccepted) {
          const go = window.confirm(
            'You are about to leave this site and browse external NomadNet content.\n\n' +
            'External nodes are not moderated and may contain content that is ' +
            'unsuitable, offensive, or illegal in your jurisdiction.\n\n' +
            (isGuest
              ? 'Continue?'
              : 'Accepting will apply to ALL external nodes for the rest of this ' +
                'session — you will not be warned again.\n\nContinue?')
          );
          if (!go) return false;
          if (!isGuest) _externalWarningAccepted = true;
        }
      }
    }
  }

  // Navigating away from the node/message list should show the destination,
  // not leave the full-page mobile sidebar covering it — close it now that
  // we're committed to navigating (past the lockdown/warning checks above).
  _closeMobileSidebar();

  addrBar.value = _displayAddress(url);

  if (pushHistory) {
    state.history = state.history.slice(0, state.historyIndex + 1);
    state.history.push(url);
    state.historyIndex = state.history.length - 1;
    // Real navigation (not an auto-refresh, not back/forward) resets
    // the per-page auto-refresh timer — the new page didn't ask to be
    // refreshed on a schedule.
    _stopAutoReload();
    const dd = $('page-autoreload');
    if (dd) dd.value = '0';
    // Reflect the target in the browser URL bar so refresh preserves
    // state and users can bookmark specific pages. pushState only
    // when this is a real (user-driven) navigation; ``pushHistory``
    // being false already means "no new history entry" — reuse the
    // signal here to match. Back/forward paths use replaceState via
    // the popstate handler below.
    _syncBrowserUrl(url, false);
  }
  updateNavButtons();

  const match = url.match(/hash:\/\/([0-9a-f]+)(\/.*)?$/i) ||
                url.match(/hash:\/([0-9a-f]+)(\/.*)?$/i) ||
                url.match(/nomadnetwork:\/\/([0-9a-f]+)(\/.*)?$/i);
  state.activeNodeHash = match ? match[1].toLowerCase() : null;
  state.activePath     = match && match[2] ? match[2] : '/';
  renderNodeList();
  updateFingerprintButton();
  updateFavPageButton();

  // LXMF address links (e.g. hash://<node>/lxmf@<dest_hash>) open the chat panel
  const lxmfMatch = url.match(/\/lxmf@([0-9a-f]+)/i);
  if (lxmfMatch) {
    _handleLxmfLink(lxmfMatch[1].toLowerCase());
    return false;
  }

  return await fetchPage(url, extraFields);
}

async function _handleLxmfLink(destHash) {
  if (!_authState.logged_in) {
    setStatus('Sign in to send messages to this address.', 'error');
    return;
  }
  showSidebarPanel('messages');
  await Promise.all([loadIdentities(), loadContacts(), refreshChats()]);

  // Auto-create contact using the announcing node's name if we don't know this hash yet
  if (!_contacts.find(c => c.hash === destHash)) {
    const node = _allNodes.find(n => n.hash === state.activeNodeHash);
    if (node) {
      try {
        const d = await apiFetch('/api/contacts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ hash: destHash, name: node.name }),
        });
        if (d.ok) _contacts = [..._contacts, d.contact];
      } catch (_) {}
    }
  }

  switchMsgTab('chats');
  openConversation(destHash);
  setStatus(`Opened conversation with ${destHash.slice(0, 16)}…`, 'ok');
}

function collectPageFields() {
  const fields = {};
  pageContent.querySelectorAll('input:not([disabled]), select:not([disabled]), textarea:not([disabled])').forEach(el => {
    if (!el.name) return;
    if (el.type === 'checkbox' || el.type === 'radio') {
      if (el.checked) fields[el.name] = el.value || 'on';
    } else {
      fields[el.name] = el.value;
    }
  });
  return fields;
}

// Show a "no virus scan was performed" warning prompt when the file
// wasn't given a clean bill of health by the backend scanner. Returns
// true when the user opts to proceed, false when they cancel. A clean
// scan ("verdict": "clean") skips the prompt entirely.
function _confirmScanResultOrCancel(filename, scan) {
  const verdict = scan && scan.verdict || 'skipped';
  if (verdict === 'clean') return true;
  let body;
  if (verdict === 'skipped') {
    body =
      'No virus scan was performed on this download.\n\n' +
      'NomadPortal is not configured with a virus scanner (VIRUS_SCAN=off). ' +
      'Verify the source before opening this file.';
  } else if (verdict === 'unavailable') {
    body =
      'The configured virus scanner was unreachable.\n\n' +
      ((scan && scan.detail) ? `Detail: ${scan.detail}\n\n` : '') +
      'No scan was completed on this file. Proceed only if you trust the source.';
  } else if (verdict === 'too-large') {
    body =
      'File exceeds the configured maximum scan size and was NOT scanned.\n\n' +
      ((scan && scan.detail) ? `${scan.detail}\n\n` : '') +
      'Proceed only if you trust the source.';
  } else {
    body =
      `Scanner returned verdict: ${verdict}.\n\n` +
      ((scan && scan.detail) ? `${scan.detail}\n\n` : '') +
      'No clean scan was confirmed. Proceed only if you trust the source.';
  }
  return window.confirm(`Save "${filename}"?\n\n${body}`);
}

// Human-readable byte sizes for the file-download status messages.
function _fmtBytes(n) {
  if (n == null) return 'unknown size';
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`;
  if (n >= 1024)    return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}

// Start a file download with a confirm-then-fetch flow:
//   1. Show a confirm() dialog with filename + MIME type (extension-derived)
//   2. POST /api/file/fetch to begin an async transfer
//   3. Poll /api/file/poll, reporting received bytes via setStatus()
//   4. On completion, trigger the browser's native save dialog by
//      navigating to /api/file/download?id=<job_id>
//
// Errors and user cancellation both restore the status indicator and
// surface a message; the in-flight job on the server times out on its
// own and gets reaped by cleanup_jobs() if abandoned.
async function startFileDownload(fileUrl) {
  // Parse the canonical URL to extract the filename for the confirm dialog.
  const m = fileUrl.match(/^hash:\/\/[0-9a-f]+\/file\/(.*)$/i);
  const filenameFromUrl = m ? decodeURIComponent(m[1].split('?')[0].split('/').pop()) : 'download';
  // Cheap client-side MIME guess from extension — final type comes from
  // the backend's mimetypes.guess_type() but we want something to show
  // before any network call.
  const ext = filenameFromUrl.includes('.') ? filenameFromUrl.split('.').pop().toLowerCase() : '';
  const guessedType = ({
    txt:  'text/plain',
    md:   'text/markdown',
    pdf:  'PDF document',
    zip:  'ZIP archive',
    tar:  'tar archive',
    gz:   'gzip archive',
    png:  'PNG image',
    jpg:  'JPEG image', jpeg: 'JPEG image',
    gif:  'GIF image',
    svg:  'SVG image',
    mp3:  'MP3 audio',
    mp4:  'MP4 video',
    json: 'JSON data',
    csv:  'CSV data',
  })[ext] || (ext ? `${ext.toUpperCase()} file` : 'unknown type');

  // Initial confirm — no scan info yet (we don't know the verdict until
  // after the fetch). The post-fetch scan verdict drives a second prompt
  // when the file was NOT scanned, so the user gets a "no virus scan"
  // flag at the moment they're actually about to receive the bytes.
  const ok = window.confirm(
    `Download "${filenameFromUrl}"?\n\n` +
    `Type: ${guessedType}\n` +
    `Source: ${fileUrl}\n\n` +
    `Files are transferred over Reticulum and may be slow on multi-hop ` +
    `LoRa networks. Always verify the source before opening any download.`
  );
  if (!ok) return;

  setStatus(`Starting download of ${filenameFromUrl}…`, 'busy');
  showLoading(true, 0);
  let filename = filenameFromUrl;
  try {
    const startResp = await apiFetch('/api/file/fetch', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ url: fileUrl }),
    });
    const jobId = startResp.job_id;
    filename = startResp.filename || filenameFromUrl;

    // Poll for progress; the file is held in the server's job buffer
    // until /api/file/download collects it. The loadingOver overlay
    // duplicates the status-bar text so failures are obvious even when
    // the topbar status indicator is out of view.
    const interval = 750;
    const maxWait  = 5 * 60 * 1000;   // 5 min cap for very large transfers
    const start    = Date.now();
    while (Date.now() - start < maxWait) {
      const poll = await apiFetch(`/api/file/poll?id=${encodeURIComponent(jobId)}`);
      if (poll.status === 'done') {
        showLoading(false);
        // Decide whether to require a second user-acknowledgement based
        // on the scan verdict. A clean scan (clamd → OK) goes straight
        // to the save dialog. Anything else — no scanner configured,
        // scanner unreachable, file too large to scan — gets an explicit
        // "no virus scan was performed" warning the user must accept.
        if (!_confirmScanResultOrCancel(filename, poll.scan_result)) {
          setStatus('Download cancelled.', 'idle');
          return;
        }
        const verdict = (poll.scan_result && poll.scan_result.verdict) || 'skipped';
        const verdictTxt = verdict === 'clean' ? '✓ scan clean'
                       : verdict === 'skipped' ? '⚠ no virus scan'
                       : `⚠ scan: ${verdict}`;
        setStatus(
          `${verdictTxt} — saving ${filename} (${_fmtBytes(poll.bytes_received)})…`,
          verdict === 'clean' ? 'ok' : 'busy',
        );
        // Browser handles the native save dialog via Content-Disposition.
        window.location.assign(`/api/file/download?id=${encodeURIComponent(jobId)}`);
        return;
      }
      if (poll.status === 'error') {
        throw new Error(poll.error || 'File fetch failed');
      }
      if (poll.status === 'scanning') {
        const msg = `Scanning ${filename} for viruses…`;
        setStatus(msg, 'busy');
        showLoading(true, 100);
        const loadingTxt = document.getElementById('loading-text');
        if (loadingTxt) loadingTxt.textContent = msg;
        await new Promise(r => setTimeout(r, interval));
        continue;
      }
      const received = poll.bytes_received || 0;
      const total = poll.total_size;
      const pct = Math.round((poll.progress || 0) * 100);
      // Compose the most informative status string we can given the
      // signals available at this point in the transfer.
      let msg;
      if (pct >= 100) {
        msg = total
          ? `Finalising ${filename} (${_fmtBytes(total)})…`
          : `Finalising ${filename}…`;
      } else if (received > 0 && total) {
        msg = `Downloading ${filename}… ${_fmtBytes(received)} of ${_fmtBytes(total)} (${pct}%)`;
      } else if (received > 0) {
        msg = `Downloading ${filename}… ${_fmtBytes(received)} (${pct}%)`;
      } else if (pct > 0) {
        msg = `Downloading ${filename}… ${pct}%`;
      } else {
        msg = `Connecting for ${filename}…`;
      }
      setStatus(msg, 'busy');
      showLoading(true, pct);
      const loadingTxt = document.getElementById('loading-text');
      if (loadingTxt) loadingTxt.textContent = msg;
      await new Promise(r => setTimeout(r, interval));
    }
    throw new Error('Download timed out waiting for the file');
  } catch (err) {
    showLoading(false);
    const reason = err && err.message ? err.message : String(err);
    setStatus(`Download failed: ${reason}`, 'error');
    // Status bar is easy to miss — surface failures with an alert too.
    window.alert(`Download failed: ${filename}\n\n${reason}`);
  }
}

// Poll a fetch job until it completes (or errors), updating the loading
// overlay with the % progress as the underlying RNS Resource transfers.
async function pollFetchJob(jobId) {
  // Tunables: poll interval (ms) and a soft cap on total wait time. The
  // server has its own timeout (PAGE_TIMEOUT) so we just need to outlast it.
  const interval = 500;
  const maxWait  = 90 * 1000;   // 90s — slightly past server timeout
  const start    = Date.now();

  while (Date.now() - start < maxWait) {
    const data = await apiFetch(`/api/page/poll?id=${encodeURIComponent(jobId)}`);
    if (data.status === 'done')  return data;
    if (data.status === 'error') throw new Error(data.error || 'Page fetch failed');
    // status === 'fetching' — update progress display
    const pct = Math.round((data.progress || 0) * 100);
    showLoading(true, pct);
    await new Promise(r => setTimeout(r, interval));
  }
  throw new Error('Fetch timed out waiting for response');
}

// Render whichever cached view (raw micron source or rendered HTML) the
// Raw toggle is currently set to. Called after a fresh fetch and on every
// toggle — never re-fetches from the node.
function renderPageContent() {
  if (toggleRaw.checked) {
    pageContent.innerHTML = `<pre>${esc(_lastPage.micron || '')}</pre>`;
    return;
  }

  pageContent.innerHTML = _lastPage.html || '';
  // Post-render form-state restoration for auto-refresh cycles. Cleared
  // immediately so a regular click-navigation that lands on the same
  // page doesn't pick up stale form values from a long-ago reload.
  if (_autoReload.pendingFormState) {
    _restoreFormState(_autoReload.pendingFormState);
    _autoReload.pendingFormState = null;
  }
  pageContent.querySelectorAll('a.mu-link, a.mu-dynamic').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      const href = a.getAttribute('href');
      if (!href || href === '#') return;
      if (href === '#blocked-download') {
        setStatus('Downloads are not supported.', 'error');
        return;
      }
      if (href.startsWith('/page?url=')) {
        const inner = decodeURIComponent(href.slice('/page?url='.length));
        // Micron links carry optional field data via data-field-spec, with
        // NomadNet's pipe syntax for input refs:
        //   `[label`:url`key=literal_value|input1|input2`key2=…]`
        //   → submit key=literal_value AND each input_name=<current value>.
        // Each backtick-separated spec is parsed independently. Inputs not
        // referenced by any spec are NOT submitted (matches NomadNet —
        // unrelated form values on the page must not leak with this click).
        const fieldSpec = a.getAttribute('data-field-spec') || '';
        const fields = {};
        if (fieldSpec) {
          // Each backtick-separated spec is a pipe-separated list of tokens.
          // Each token is one of:
          //   `*`           — wildcard: merge in every input on the current
          //                   page via collectPageFields(). Used by forum
          //                   actions like *|action=preview|board_id=1.
          //   `key=literal` — literal pair: fields[key] = literal.
          //   `inputname`   — input-name reference: look up an <input> with
          //                   that name on the page and submit its current
          //                   value as fields[inputname].
          // Tokens are independent, order-agnostic, and all contribute to
          // the outgoing submission. This handles the original NomadNet
          // syntax `key=val|input1|input2` (one literal + N input refs) and
          // the mixed wildcard form `*|key=lit|inputname` equivalently.
          fieldSpec.split('`').forEach(spec => {
            spec.split('|').forEach(token => {
              const t = token.trim();
              if (!t) return;
              if (t === '*') {
                Object.assign(fields, collectPageFields());
                return;
              }
              const eq = t.indexOf('=');
              if (eq > 0) {
                const key = t.slice(0, eq).trim();
                if (key) fields[key] = t.slice(eq + 1);
                return;
              }
              // No `=` — treat as input-name reference.
              const inputs = pageContent.querySelectorAll(
                `input[name="${CSS.escape(t)}"]`,
              );
              if (!inputs.length) return;
              const first = inputs[0];
              if (first.type === 'radio') {
                // For radio groups (multiple inputs sharing a name), pick
                // the *checked* one — querySelector returns only the first,
                // which would silently drop the actual selection.
                const checked = Array.from(inputs).find(i => i.checked);
                if (checked) fields[t] = checked.value || 'on';
              } else if (first.type === 'checkbox') {
                if (first.checked) fields[t] = first.value || 'on';
              } else {
                fields[t] = first.value;
              }
            });
          });
        }
        navigateTo(inner, true, Object.keys(fields).length ? fields : null);
        return;
      }
      if (href.startsWith('/file?url=')) {
        const fileUrl = decodeURIComponent(href.slice('/file?url='.length));
        startFileDownload(fileUrl);
        return;
      }
      if (href.startsWith('http://') || href.startsWith('https://')) {
        // NomadNet page content is untrusted (HTML-escaped, no JS
        // execution path — see the trust model in README.md), but a
        // plain-text link label can still claim to be anything while
        // pointing at an arbitrary clearnet URL: a classic phishing
        // pattern, and worse here since it's also the one way a mesh
        // page can walk a visitor off the mesh entirely without them
        // necessarily noticing. Always confirm — regardless of login/
        // admin state, unlike the once-per-session external-*node*
        // warning in navigateTo() — and show the real destination, not
        // just whatever the link text says.
        const go = window.confirm(
          'This link leaves NomadPortal and opens an external website ' +
          '(outside the Reticulum mesh) in a new tab:\n\n' + href +
          '\n\nOnly continue if you trust this destination.'
        );
        if (!go) return;
        window.open(href, '_blank', 'noopener,noreferrer');
        return;
      }
    });
  });
  pageContent.querySelectorAll('form.mu-form').forEach(f => {
    f.addEventListener('submit', e => {
      e.preventDefault();
      handleFormSubmit(f, _lastPage.hash, _lastPage.path);
    });
  });
}

async function fetchPage(url, extraFields = null) {
  if (state.loading) return false;
  state.loading = true;
  showLoading(true, 0);
  setStatus('Fetching page…', 'busy');
  pageError.hidden = true;

  try {
    // Only attach field data when the caller passed `extraFields` — i.e.
    // this navigation came from a link click with `data-field-spec` or
    // from a form submit. The caller is responsible for picking up any
    // referenced input values; plain navigation must not leak unrelated
    // form state on the page (and the backend treats any request with
    // fields as a form submission, gated by _can_interact).
    const fields = extraFields || {};
    const hasFields = Object.keys(fields).length > 0;
    // Start the fetch — server returns a job_id immediately.
    const startBody = { url };
    if (hasFields) startBody.fields = fields;
    const startResp = await apiFetch('/api/page/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(startBody),
    });
    const data = await pollFetchJob(startResp.job_id);
    showLoading(false);

    if (pageNodeName) pageNodeName.textContent = getNodeName(data.hash);
    if (pagePath)     pagePath.textContent = data.path || '/';
    _updateBreadcrumb(data.hash);
    addrBar.value = _displayAddress(url);

    _lastPage = {
      url,
      hash:   data.hash || '',
      path:   data.path || '/',
      html:   data.html   || '',
      micron: data.micron || '',
    };
    renderPageContent();

    setStatus(`Loaded: ${data.path}`, 'ok');
    return true;
  } catch (e) {
    showLoading(false);
    showError(e.message);
    setStatus(humanizeError(e.message), 'error');
    return false;
  } finally {
    state.loading = false;
  }
}

async function handleFormSubmit(form, nodeHash, currentPath) {
  const inputs = form.querySelectorAll('input:not([disabled]), select:not([disabled])');
  const fields = {};
  inputs.forEach(el => {
    if (el.name) fields[el.name] = el.value;
  });
  const action = form.dataset.action || currentPath;

  try {
    state.loading = true;
    showLoading(true, 0);
    const startResp = await apiFetch('/api/page/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hash: nodeHash, path: action, fields }),
    });
    const data = await pollFetchJob(startResp.job_id, false);
    showLoading(false);
    pageContent.innerHTML = data.html || '';
    setStatus(`Submitted: ${action}`, 'ok');
  } catch (e) {
    showLoading(false);
    showError(e.message);
  } finally {
    state.loading = false;
  }
}

function getNodeName(hash) {
  if (!hash) return '';
  const node = _allNodes.find(n => n.hash === hash);
  return node ? node.name : hash.slice(0, 16) + '…';
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------
function updateNavButtons() {
  btnBack.disabled = state.historyIndex <= 0;
  btnFwd.disabled  = state.historyIndex >= state.history.length - 1;
  btnRefresh.disabled =
    state.historyIndex < 0 || !state.history[state.historyIndex];
}

btnBack.addEventListener('click', () => {
  if (state.historyIndex > 0) {
    state.historyIndex--;
    navigateTo(state.history[state.historyIndex], false);
  }
});
btnFwd.addEventListener('click', () => {
  if (state.historyIndex < state.history.length - 1) {
    state.historyIndex++;
    navigateTo(state.history[state.historyIndex], false);
  }
});
btnRefresh.addEventListener('click', () => {
  const current = state.history[state.historyIndex];
  if (current) navigateTo(current, false);
});
const _btnPagePing = $('btn-page-ping');
if (_btnPagePing) _btnPagePing.addEventListener('click', _pingCurrentPage);
const _ddAutoReload = $('page-autoreload');
if (_ddAutoReload) _ddAutoReload.addEventListener('change', () => {
  _startAutoReload(parseInt(_ddAutoReload.value, 10) || 0);
});

// ---------------------------------------------------------------------------
// Address bar
// ---------------------------------------------------------------------------
function _submitAddrBar() {
  const fixed = _normaliseAddress(addrBar.value);
  if (fixed !== addrBar.value) addrBar.value = fixed;
  navigateTo(fixed);
}
btnGo.addEventListener('click', _submitAddrBar);
addrBar.addEventListener('keydown', e => {
  if (e.key === 'Enter') _submitAddrBar();
});

// ---------------------------------------------------------------------------
// Global keyboard shortcuts
//   /          focus the address bar (skipped when typing in another input)
//   Ctrl+L     focus the address bar (browser-style)
//   Alt+R      refresh current page (re-fetch the active history entry)
//   Alt+F      toggle favorite on the current page (requires login)
//   Alt+B      back (mirrors the topbar Back button)
//   Esc        close any open modal, then blur the address bar
// ---------------------------------------------------------------------------
function _isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}

document.addEventListener('keydown', e => {
  // Ctrl+L / Cmd+L → focus address bar (and select for easy retype)
  if ((e.ctrlKey || e.metaKey) && e.key === 'l') {
    e.preventDefault();
    addrBar.focus();
    addrBar.select();
    return;
  }
  // "/" → focus address bar, but only when not already typing somewhere
  if (e.key === '/' && !_isTypingTarget(e.target) && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    addrBar.focus();
    addrBar.select();
    return;
  }
  // Alt-prefixed power-user shortcuts. Alt+letter combos don't have a
  // browser-default meaning we'd clobber, so we don't gate them on
  // "is the user typing in an input" — useful for, e.g., refreshing a
  // page while a form field is focused. We still skip when no key is
  // truthy (e.g., a stray Alt press) and when both Ctrl/Meta are held
  // (those are the browser-shortcut namespace).
  if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    if (e.key === 'r' || e.key === 'R') {
      e.preventDefault();
      const current = state.history[state.historyIndex];
      if (current) navigateTo(current, false);
      return;
    }
    if (e.key === 'b' || e.key === 'B') {
      e.preventDefault();
      if (state.historyIndex > 0) {
        state.historyIndex--;
        navigateTo(state.history[state.historyIndex], false);
      }
      return;
    }
    if (e.key === 'f' || e.key === 'F') {
      e.preventDefault();
      const btn = $('btn-fav-page');
      if (btn && !btn.hidden) btn.click();
      return;
    }
  }
  // Esc → close any visible modal; then blur the address bar
  if (e.key === 'Escape') {
    const openModal = document.querySelector('#msg-modal:not([hidden])');
    if (openModal) {
      openModal.hidden = true;
      e.preventDefault();
      return;
    }
    if (document.activeElement === addrBar) {
      addrBar.blur();
    }
  }
});

// ---------------------------------------------------------------------------
// Node filter
// ---------------------------------------------------------------------------
nodeFilter.addEventListener('input', renderNodeList);
if (nodeSort) nodeSort.addEventListener('change', renderNodeList);

// ---------------------------------------------------------------------------
// Raw toggle
// ---------------------------------------------------------------------------
toggleRaw.addEventListener('change', () => {
  // Swap displays from the cached fetch — never re-call the node.
  if (_lastPage.url) renderPageContent();
});

// Word-wrap toggle — flips ``#page-content`` between
// ``white-space: pre`` (default; keeps ASCII art column alignment
// intact) and ``white-space: pre-wrap`` (wraps long prose lines
// inside the content column). Operator-level preference, not per
// page — the toggle state persists across navigation. No auto-
// detection: ASCII art is the common case on NomadNet, so the
// default stays unchanged from prior releases.
if (toggleWrap) toggleWrap.addEventListener('change', () => {
  if (toggleWrap.checked) {
    pageContent.classList.add('wrap-prose');
  } else {
    pageContent.classList.remove('wrap-prose');
  }
});

// ---------------------------------------------------------------------------
// Refresh button
// ---------------------------------------------------------------------------
$('btn-refresh-nodes').addEventListener('click', refreshNodes);

// ---------------------------------------------------------------------------
// Status / UI helpers
// ---------------------------------------------------------------------------
function setStatus(msg, level = 'idle') {
  statusText.textContent = msg;
  statusDot.className = `status-${level}`;
}

function showLoading(show, percent) {
  loadingOver.hidden = !show;
  if (show) {
    const txt = document.getElementById('loading-text');
    if (txt) {
      // Backend bumps `progress` only once the RNS Resource transfer begins.
      // Before that — path discovery, link establishment, request awaiting
      // first byte — progress stays at 0, so we show a neutral phrase
      // instead of "0%" to avoid the appearance of being stuck.
      txt.textContent = (typeof percent === 'number' && percent > 0)
        ? `Receiving ${percent}%…`
        : 'Connecting to node…';
    }
  }
}

// Map common technical / network errors to friendlier user-facing strings.
// Backend errors are already mostly user-friendly; this layer catches the
// remaining low-level ones (HTTP statuses, fetch failures) and trims the
// noise so the status bar reads naturally.
function humanizeError(msg) {
  if (!msg) return 'Something went wrong.';
  const m = String(msg);
  if (/^HTTP 401$/i.test(m))               return 'Sign in required.';
  if (/^HTTP 403$/i.test(m))               return 'You don’t have permission for that.';
  if (/^HTTP 404$/i.test(m))               return 'Not found.';
  if (/^HTTP 413$/i.test(m))               return 'That request is too large.';
  if (/^HTTP 429$/i.test(m))               return 'Too many requests — slow down.';
  if (/^HTTP 5\d\d$/i.test(m))             return 'Server error — see the logs.';
  if (/Failed to fetch|NetworkError|Load failed/i.test(m))
                                           return 'Network error — is the server running?';
  if (/Fetch timed out/i.test(m))          return 'Page fetch timed out.';
  // Strip redundant prefixes.
  return m.replace(/^Error:\s*/i, '').replace(/^fetch_page:\s*/i, '');
}

function showError(msg) {
  pageContent.innerHTML = '';
  pageError.hidden = false;
  pageError.textContent = humanizeError(msg);
}

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------
// Status polling
// ---------------------------------------------------------------------------
async function pollStatus() {
  try {
    const data = await apiFetch('/api/status');
    cacheInfo.textContent =
      `cache: ${data.cache.live}/${data.cache.entries} entries`;
    if (data.nodes_discovered !== _allNodes.length) {
      await refreshNodes();
    }
    _updateStatusTooltip(data);
  } catch (_) { /* silently ignore polling errors */ }
  if (_authState.logged_in) _pollUnread();
  // Sites refresh for guests too (refreshNetworkPanel's own guest/
  // logged-in branching handles peers/relays) — not gated behind
  // logged_in the way _pollUnread is.
  if (!$('sidebar-panel-network').hidden) refreshNetworkPanel();
}

function _updateStatusTooltip(data) {
  if (!statusDot) return;
  const ifaces = data.interfaces || [];
  const online = ifaces.filter(i => i.online).length;
  const total  = ifaces.length;
  const lines = [
    `Network status — ${online}/${total} interfaces online`,
    `Nodes discovered: ${data.nodes_discovered}`,
    `Total announces seen: ${data.total_announces}`,
    '',
    ...ifaces.map(i => {
      const dot = i.online ? '●' : '○';
      const rx  = _formatBytes(i.life_rxb || 0);
      const tx  = _formatBytes(i.life_txb || 0);
      return `${dot} ${i.name}  ↓${rx}  ↑${tx}`;
    }),
  ];
  statusDot.title = lines.join('\n');
}

function _formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

async function _pollUnread() {
  try {
    const data  = await apiFetch('/api/messages/received');
    const msgs  = data.messages || [];
    const total = msgs.filter(m => !m.read).length;
    _updateUnreadBadges(total);
    // Keep conversation list fresh when the messages panel is open
    if (!$('sidebar-panel-messages').hidden) refreshChats();
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// Auth state
// ---------------------------------------------------------------------------
let _authState = { logged_in: false, is_admin: false, user: null };

async function loadAuthState() {
  try {
    _authState = await apiFetch('/api/auth/status');
  } catch (_) { _authState = { logged_in: false, is_admin: false }; }
  applyAuthUI();
}

function applyAuthUI() {
  const { logged_in, is_admin, user } = _authState;

  $('ub-guest-section').hidden  = logged_in;
  $('ub-user-section').hidden   = !logged_in;
  if (logged_in && user) {
    $('ub-name').textContent    = user.name || user.email || '';
    $('ub-admin-badge').hidden  = !is_admin;
    $('ub-admin-link').hidden   = !is_admin;
    $('ub-admin-sep').hidden    = !is_admin;
  }

  $('welcome-guest').hidden = logged_in;
  $('welcome-user').hidden  = !logged_in;

  const composeBtn = $('btn-compose-new');
  if (composeBtn) composeBtn.disabled = !logged_in;

  renderNodeList();
  updateFingerprintButton();
  loadFavorites();
  loadIdentities();
}

function updateFingerprintButton() {
  // Persistent identification toggle, scoped to the active node only.
  const btn = $('btn-identify');
  if (!btn) return;
  const show = _authState.logged_in && !!state.activeNodeHash;
  btn.hidden = !show;
  if (!show) return;
  const on = _identifiedNodes.has(state.activeNodeHash);
  btn.classList.toggle('identify-active', on);
  btn.title = on
    ? 'Identifying as you on this site — click to stop'
    : 'Identify to this site (sticky)';
}

async function toggleIdentifyActive() {
  if (!_authState.logged_in || !state.activeNodeHash) return;
  const hash = state.activeNodeHash;
  const wasOn = _identifiedNodes.has(hash);
  // Optimistic flip so the button reflects the click immediately.
  if (wasOn) _identifiedNodes.delete(hash); else _identifiedNodes.add(hash);
  updateFingerprintButton();

  try {
    await apiFetch('/api/fingerprint', {
      method:  wasOn ? 'DELETE' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ dest_hash: hash }),
    });
    setStatus(
      wasOn
        ? 'Stopped identifying — future page loads on this site go anonymous.'
        : 'Identifying — reloading the page so the site sees you.',
      'ok',
    );
    // Reload the current page so the next fetch picks up the new state.
    if (state.history.length && state.historyIndex >= 0) {
      navigateTo(state.history[state.historyIndex], false);
    }
  } catch (e) {
    // Roll back the optimistic flip.
    if (wasOn) _identifiedNodes.add(hash); else _identifiedNodes.delete(hash);
    updateFingerprintButton();
    setStatus(`Could not toggle identification: ${e.message}`, 'error');
  }
}

$('btn-identify')?.addEventListener('click', toggleIdentifyActive);
$('btn-fav-page')?.addEventListener('click', toggleCurrentPageFavorite);

// ---------------------------------------------------------------------------
// Sidebar panel switcher (Nodes / Messages)
// ---------------------------------------------------------------------------
function showSidebarPanel(name) {
  $('sidebar-panel-nodes').hidden    = (name !== 'nodes');
  $('sidebar-panel-messages').hidden = (name !== 'messages');
  $('sidebar-panel-network').hidden  = (name !== 'network');
  $('sidebar-tab-nodes').classList.toggle('active',    name === 'nodes');
  $('sidebar-tab-messages').classList.toggle('active', name === 'messages');
  $('sidebar-tab-network').classList.toggle('active',  name === 'network');
}

$('sidebar-tabs').addEventListener('click', e => {
  const tab = e.target.closest('.sidebar-tab');
  if (!tab) return;
  const panel = tab.dataset.panel;
  showSidebarPanel(panel);
  if (panel === 'messages') {
    loadIdentities();
    refreshChats();
  } else if (panel === 'network') {
    refreshNetworkPanel();
  }
});


// ---------------------------------------------------------------------------
// Identity — one per user, auto-created on login
// ---------------------------------------------------------------------------
let _myIdentity = null;

async function loadIdentities() {
  if (!_authState.logged_in) {
    _identifiedNodes = new Set();
    updateFingerprintButton();
    return;
  }
  try {
    const data = await apiFetch('/api/my-identity');
    _myIdentity = data.identity || null;
    _identifiedNodes = new Set(_myIdentity?.identified_nodes || []);
    _updateAnnounceStatus();
    _renderMyIcon();
    updateFingerprintButton();
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// User icon (FIELD_ICON_APPEARANCE — glyph + 2 colors)
// ---------------------------------------------------------------------------
// Strict #RRGGBB validator — CodeQL's js/xss-through-dom rule sees
// `fg`/`bg` flowing into innerHTML and warns. Constraining the values
// to a regex-validated hex colour collapses the dataflow to a known
// safe shape before splicing into the SVG attribute. Anything that
// isn't ``#`` + 6 hex chars falls back to a neutral default.
const _HEX_COLOR_RE = /^#[0-9A-Fa-f]{6}$/;
function _safeHexColor(value, fallback) {
  return (typeof value === 'string' && _HEX_COLOR_RE.test(value)) ? value : fallback;
}

// Same rationale as _safeHexColor above, applied to MDI path "d" data
// (fetched from this app's own bundled static/data/mdi_icons.json, not
// remote/user-controlled — but still flows into innerHTML, so it's
// worth the same collapse-the-dataflow treatment CodeQL's rule wants).
// Legitimate SVG path data is only command letters, digits, '.', '-',
// ',' and whitespace — never '"', '<', or '>' — so this is a strict
// allowlist, not a guess at what to block.
const _SVG_PATH_RE = /^[MmLlHhVvCcSsQqTtAaZz0-9.,\-\s]+$/;
function _safeSvgPath(value) {
  return (typeof value === 'string' && _SVG_PATH_RE.test(value)) ? value : null;
}

// ---------------------------------------------------------------------------
// Real Material Design Icons catalog — lazy-fetched client-side mirror of
// mdi_icons.py's server-side lookup, backing both this app's own live
// icon preview (_iconSvg below) and the icon picker. Loaded once, only
// when actually needed (icon editor opened) — static/data/mdi_icons.json
// is ~2.7MB, no reason to fetch it on every page load. See NOTICE.md for
// the data's own license (Apache-2.0, Material Design Icons project).
// ---------------------------------------------------------------------------
let _mdiPaths      = null;  // name -> SVG path "d" data, once loaded
let _mdiCategories = null;  // category -> [names], once loaded
let _mdiLoading    = null;  // in-flight fetch promise, so concurrent callers share it

function _normalizeMdiName(name) {
  return (name || '').trim().toLowerCase().replace(/[ _]/g, '-');
}

function _ensureMdiCatalog() {
  if (_mdiPaths) return Promise.resolve();
  if (_mdiLoading) return _mdiLoading;
  _mdiLoading = Promise.all([
    fetch('/static/data/mdi_icons.json').then(r => r.json()),
    fetch('/static/data/mdi_categories.json').then(r => r.json()),
  ]).then(([paths, categories]) => {
    _mdiPaths = paths;
    _mdiCategories = categories;
  }).catch(e => {
    // Missing/corrupt asset degrades every lookup to "not found" — same
    // contract as mdi_icons.py's server-side loader. Reset _mdiLoading
    // so a later call can retry (e.g. a transient network blip) instead
    // of permanently remembering this one failure.
    _mdiPaths = {};
    _mdiCategories = {};
    setStatus(`Could not load icon catalog: ${e.message}`, 'error');
  }).finally(() => { _mdiLoading = null; });
  return _mdiLoading;
}

function _iconSvg(glyph, fg, bg, size = 28) {
  const safeFg = _safeHexColor(fg, '#ffffff');
  const safeBg = _safeHexColor(bg, '#5ba3c9');
  const path = _mdiPaths && _safeSvgPath(_mdiPaths[_normalizeMdiName(glyph)]);

  // Same 24x24-inset-in-32x32-circle math as messaging.py's
  // _render_appearance_svg, so a self-rendered preview and a
  // server-rendered contact icon look identical for the same inputs.
  const glyphSvg = path
    ? `<g transform="translate(6,6) scale(${(20 / 24).toFixed(4)})">` +
      `<path d="${path}" fill="${safeFg}"/></g>`
    : (() => {
        const g = (glyph || '?').slice(0, 1).toUpperCase();
        const fontSize = (size * 0.55) * 32 / size;
        return `<text x="16" y="22" text-anchor="middle" font-size="${fontSize}" ` +
               `font-family="sans-serif" font-weight="bold" fill="${safeFg}">${esc(g)}</text>`;
      })();

  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" ' +
    `width="${size}" height="${size}">` +
    `<circle cx="16" cy="16" r="16" fill="${safeBg}"/>${glyphSvg}` +
    '</svg>'
  );
}

function _myIconDefaults() {
  const name = (_authState && _authState.user && _authState.user.name) || '?';
  return { glyph: name.charAt(0).toUpperCase() || '?', fg: '#ffffff', bg: '#5ba3c9' };
}

function _renderMyIcon() {
  const slot = $('my-icon-preview');
  if (!slot) return;
  const ic = (_myIdentity && _myIdentity.icon) || _myIconDefaults();
  slot.innerHTML = _iconSvg(ic.glyph, ic.fg, ic.bg, 28);
}

function _renderIconEditorPreview() {
  const slot = $('icon-editor-preview');
  if (!slot) return;
  const glyph = $('icon-glyph').value;
  slot.innerHTML = _iconSvg(glyph, $('icon-fg').value, $('icon-bg').value, 36);
  const label = $('icon-glyph-label');
  if (label) {
    // Real MDI name once the catalog's loaded and recognizes it;
    // otherwise show the raw stored value so a not-yet-migrated old
    // single-letter glyph is still legible as "what's saved", not blank.
    label.textContent = (_mdiPaths && _mdiPaths[_normalizeMdiName(glyph)]) ? glyph : (glyph || '?');
  }
}

function _openIconEditor() {
  const ic = (_myIdentity && _myIdentity.icon) || _myIconDefaults();
  $('icon-glyph').value = ic.glyph;
  $('icon-fg').value    = ic.fg;
  $('icon-bg').value    = ic.bg;
  _renderIconEditorPreview();
  $('icon-editor').hidden = false;
  // Warm the catalog now rather than waiting for "Choose icon…" — by the
  // time that click happens the fetch is usually already done, so the
  // picker opens with real icons rendered immediately instead of a
  // blank grid for a beat.
  _ensureMdiCatalog().then(_renderIconEditorPreview);
}

if ($('btn-edit-icon')) {
  $('btn-edit-icon').addEventListener('click', _openIconEditor);
  $('btn-icon-cancel').addEventListener('click', () => { $('icon-editor').hidden = true; });
  ['icon-fg', 'icon-bg'].forEach(id => {
    $(id).addEventListener('input', _renderIconEditorPreview);
  });
  $('btn-icon-save').addEventListener('click', async () => {
    try {
      const d = await apiFetch('/api/my-identity/icon', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          glyph: $('icon-glyph').value,
          fg:    $('icon-fg').value,
          bg:    $('icon-bg').value,
        }),
      });
      if (d.ok && d.icon) {
        if (_myIdentity) _myIdentity.icon = d.icon;
        _renderMyIcon();
        $('icon-editor').hidden = true;
      }
    } catch (e) {}
  });
}

// ---------------------------------------------------------------------------
// Icon picker — search + category chips over the full MDI catalog.
// Renders a capped number of results at a time (real result sets can run
// into the thousands; an unbounded grid would be both slow to build and
// useless to scroll through) — search narrows fast in practice, and a
// truncation note tells the user to narrow further rather than silently
// hiding results with no explanation.
// ---------------------------------------------------------------------------
const _ICON_PICKER_MAX_RESULTS = 300;
let _iconPickerCategory = null;  // null = "All" / search-only mode

function _renderIconPickerGrid(names) {
  const grid = $('icon-picker-grid');
  const status = $('icon-picker-status');
  if (!grid) return;
  const total = names.length;
  const shown = names.slice(0, _ICON_PICKER_MAX_RESULTS);
  grid.innerHTML = shown.map(name => {
    const path = _safeSvgPath(_mdiPaths[name]);
    if (!path) return '';  // shouldn't happen against trusted bundled data — skip, don't render garbage
    const svg = `<svg viewBox="0 0 24 24"><path d="${path}" fill="currentColor"/></svg>`;
    return `<button type="button" class="icon-picker-item" data-name="${esc(name)}" ` +
           `title="${esc(name)}">${svg}</button>`;
  }).join('');
  status.textContent = total === 0
    ? 'No icons match.'
    : total > _ICON_PICKER_MAX_RESULTS
      ? `Showing ${_ICON_PICKER_MAX_RESULTS} of ${total} — narrow your search to see more.`
      : `${total} icon${total === 1 ? '' : 's'}`;
}

function _applyIconPickerFilter() {
  if (!_mdiPaths) return;
  const query = ($('icon-picker-search').value || '').trim().toLowerCase();
  let pool = _iconPickerCategory
    ? (_mdiCategories[_iconPickerCategory] || [])
    : Object.keys(_mdiPaths);
  if (query) pool = pool.filter(name => name.includes(query));
  // Search-all-icons with no query yet would just be the first N of an
  // unsorted 7400-entry object — not useful. Category browsing has no
  // such problem (each category's own list is small enough to show
  // directly), so only gate the "All + no query" combination.
  if (!_iconPickerCategory && !query) {
    $('icon-picker-grid').innerHTML = '';
    $('icon-picker-status').textContent = 'Search or pick a category to browse icons.';
    return;
  }
  _renderIconPickerGrid(pool.sort());
}

function _renderIconPickerCategories() {
  const wrap = $('icon-picker-categories');
  if (!wrap || !_mdiCategories) return;
  const chips = ['All', ...Object.keys(_mdiCategories).sort()];
  wrap.innerHTML = chips.map(cat => {
    const isAll = cat === 'All';
    const active = isAll ? !_iconPickerCategory : _iconPickerCategory === cat;
    return `<span class="icon-picker-chip${active ? ' active' : ''}" ` +
           `data-cat="${isAll ? '' : esc(cat)}">${esc(cat)}</span>`;
  }).join('');
}

function _openIconPicker() {
  $('icon-picker-modal').hidden = false;
  $('icon-picker-search').value = '';
  _iconPickerCategory = null;
  _ensureMdiCatalog().then(() => {
    _renderIconPickerCategories();
    _applyIconPickerFilter();
  });
  if (_mdiCategories) _renderIconPickerCategories();
  $('icon-picker-status').textContent = _mdiPaths ? '' : 'Loading icon catalog…';
}

if ($('btn-choose-icon')) {
  $('btn-choose-icon').addEventListener('click', _openIconPicker);
  $('btn-icon-picker-cancel').addEventListener('click', () => { $('icon-picker-modal').hidden = true; });
  $('icon-picker-search').addEventListener('input', _applyIconPickerFilter);
  $('icon-picker-categories').addEventListener('click', e => {
    const chip = e.target.closest('.icon-picker-chip');
    if (!chip) return;
    _iconPickerCategory = chip.dataset.cat || null;
    _renderIconPickerCategories();
    _applyIconPickerFilter();
  });
  $('icon-picker-grid').addEventListener('click', e => {
    const item = e.target.closest('.icon-picker-item');
    if (!item) return;
    $('icon-glyph').value = item.dataset.name;
    _renderIconEditorPreview();
    $('icon-picker-modal').hidden = true;
  });
}

// ---------------------------------------------------------------------------
// Announce — with live countdown
// ---------------------------------------------------------------------------
let _announceTimerId = null;
let _announceSentAt  = 0;   // client-side timestamp (ms) of last successful announce

function _stopAnnounceTimer() {
  if (_announceTimerId !== null) {
    clearInterval(_announceTimerId);
    _announceTimerId = null;
  }
}

function _fmtCountdown(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h > 0
    ? `${h}h ${m}m ${s}s`
    : `${m}m ${String(s).padStart(2, '0')}s`;
}

function _updateAnnounceStatus() {
  const nameEl   = $('announce-identity-name');
  const statusEl = $('announce-status');
  const btn      = $('btn-announce');
  if (!statusEl || !btn) return;

  if (!_myIdentity) {
    if (nameEl) nameEl.textContent = '';
    statusEl.textContent = '';
    statusEl.style.color = '';
    btn.disabled = true;
    _stopAnnounceTimer();
    return;
  }

  if (nameEl) nameEl.textContent = _myIdentity.name;

  const addrEl = $('announce-lxmf-addr');
  if (addrEl) {
    const addr = _myIdentity.lxmf_address || '';
    addrEl.textContent = addr ? addr : '';
    addrEl.hidden = !addr;
  }

  const last      = _myIdentity.last_announced || 0;
  const remaining = Math.ceil((last + 3 * 3600) - Date.now() / 1000);
  const justSent  = _announceSentAt > 0 && (Date.now() - _announceSentAt) < 8000;

  if (remaining > 0) {
    const countdown = _fmtCountdown(remaining);
    if (justSent) {
      statusEl.textContent = `✓ Sent  ·  next in ${countdown}`;
      statusEl.style.color = 'var(--accent2)';
    } else {
      statusEl.textContent = `Next announce in ${countdown}`;
      statusEl.style.color = 'var(--text-dim)';
    }
    btn.disabled = true;
    if (_announceTimerId === null) {
      _announceTimerId = setInterval(_updateAnnounceStatus, 1000);
    }
  } else {
    _stopAnnounceTimer();
    if (justSent) {
      statusEl.textContent = '✓ Sent';
      statusEl.style.color = 'var(--accent2)';
    } else if (last) {
      statusEl.textContent = `Last announced ${formatAge(last)}`;
      statusEl.style.color = 'var(--text-dim)';
    } else {
      statusEl.textContent = '';
      statusEl.style.color = '';
    }
    btn.disabled = false;
  }
}

$('announce-lxmf-addr').addEventListener('click', () => {
  const addr = $('announce-lxmf-addr')?.textContent?.trim();
  if (!addr) return;
  navigator.clipboard.writeText(addr).then(() => {
    const el = $('announce-lxmf-addr');
    const prev = el.textContent;
    el.textContent = 'Copied!';
    setTimeout(() => { el.textContent = prev; }, 1200);
  }).catch(() => {});
});

$('btn-rename-identity').addEventListener('click', () => {
  if (!_myIdentity) return;
  const nameEl = $('announce-identity-name');
  const input  = document.createElement('input');
  input.type   = 'text';
  input.value  = _myIdentity.name;
  input.style.cssText = 'font-size:12px;padding:1px 4px;background:var(--bg);' +
    'border:1px solid var(--accent);color:var(--text);font-family:var(--font-mono);' +
    'border-radius:2px;outline:none;width:100%;min-width:0;';
  nameEl.replaceWith(input);
  input.focus();
  input.select();
  const commit = async () => {
    const newName = input.value.trim();
    if (newName && newName !== _myIdentity.name) {
      try {
        await apiFetch(`/api/identities/${_myIdentity.id}/rename`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: newName }),
        });
        _myIdentity.name = newName;
      } catch (err) {
        setStatus(`Could not rename: ${err.message}`, 'error');
      }
    }
    const span = $('announce-identity-name') || document.createElement('span');
    span.id = 'announce-identity-name';
    span.style.cssText = 'flex:1;font-size:12px;color:var(--text-dim);overflow:hidden;' +
      'text-overflow:ellipsis;white-space:nowrap;';
    span.textContent = _myIdentity.name;
    input.replaceWith(span);
  };
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { input.value = _myIdentity.name; input.blur(); }
  });
});

$('btn-announce').addEventListener('click', async () => {
  if (!_myIdentity) return;
  const btn      = $('btn-announce');
  const statusEl = $('announce-status');
  btn.disabled    = true;
  btn.textContent = 'Sending…';
  statusEl.textContent = '';
  statusEl.style.color = '';
  try {
    const data = await apiFetch(`/api/identities/${_myIdentity.id}/announce`, { method: 'POST' });
    if (data.ok) {
      _myIdentity.last_announced = Date.now() / 1000;
      _announceSentAt = Date.now();
      _updateAnnounceStatus();
      // Clear the "✓ Sent" prefix after 8 s so it fades into plain countdown
      setTimeout(() => { _announceSentAt = 0; _updateAnnounceStatus(); }, 8000);
    } else {
      if (data.next_allowed && data.next_allowed > Date.now() / 1000) {
        _myIdentity.last_announced = data.next_allowed - 3 * 3600;
      }
      statusEl.textContent = data.message || 'Failed';
      statusEl.style.color = 'var(--error)';
      _updateAnnounceStatus();
    }
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
    statusEl.style.color = 'var(--error)';
    btn.disabled = false;
  } finally {
    btn.textContent = 'Announce';
  }
});

// ---------------------------------------------------------------------------
// Tab switching (Chats | Users)
// ---------------------------------------------------------------------------
function switchMsgTab(tabName) {
  document.querySelectorAll('.msg-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabName);
  });
  document.querySelectorAll('.msg-tab-panel').forEach(panel => {
    panel.hidden = panel.id !== `tab-${tabName}`;
  });
}

$('msg-tabs').addEventListener('click', e => {
  const tab = e.target.closest('.msg-tab');
  if (!tab) return;
  switchMsgTab(tab.dataset.tab);
  if (tab.dataset.tab === 'users') refreshLxmfPeers();
});

// ---------------------------------------------------------------------------
// Chat / Conversations
// ---------------------------------------------------------------------------
let _allConversations = [];
let _currentConvHash  = null;

function buildConversations(sent, received) {
  const map = {};

  for (const m of sent) {
    const hash = m.dest;
    if (!hash) continue;
    if (!map[hash]) map[hash] = { hash, messages: [], unread: 0, lastTime: 0 };
    map[hash].messages.push({ ...m, direction: 'sent', time: m.sent_at });
    map[hash].lastTime = Math.max(map[hash].lastTime, m.sent_at || 0);
  }

  for (const m of received) {
    const hash = m.source;
    if (!hash) continue;
    if (!map[hash]) map[hash] = { hash, messages: [], unread: 0, lastTime: 0 };
    map[hash].messages.push({ ...m, direction: 'received', time: m.received_at });
    map[hash].lastTime = Math.max(map[hash].lastTime, m.received_at || 0);
    if (!m.read) map[hash].unread++;
  }

  for (const c of Object.values(map)) {
    c.messages.sort((a, b) => a.time - b.time);
  }

  return Object.values(map).sort((a, b) => b.lastTime - a.lastTime);
}

function _updateUnreadBadges(total) {
  ['inbox-badge', 'sidebar-msg-badge'].forEach(id => {
    const el = $(id);
    if (!el) return;
    el.textContent = total > 0 ? total : '';
    el.hidden = total === 0;
  });
}

async function refreshChats() {
  if (!_authState.logged_in) return;
  try {
    const [sentData, recvData] = await Promise.all([
      apiFetch('/api/messages/sent'),
      apiFetch('/api/messages/received'),
      loadContacts(),
    ]);
    _allConversations = buildConversations(
      sentData.messages || [],
      recvData.messages || [],
    );
    const totalUnread = _allConversations.reduce((n, c) => n + c.unread, 0);
    _updateUnreadBadges(totalUnread);

    if (_currentConvHash) {
      const conv = _allConversations.find(c => c.hash === _currentConvHash);
      renderChatLog(conv ? conv.messages : []);
    } else {
      renderConversationList(_allConversations);
    }
  } catch (_) {}
}

function renderConversationList(conversations) {
  const inner = $('chat-list-inner');
  if (!inner) return;
  if (!conversations.length) {
    inner.innerHTML =
      '<div class="msg-empty">' +
      'No conversations yet.<br>' +
      '<span style="color:var(--text-dim);font-size:11px;">' +
      'Click <strong>+ New</strong> to start one, or click an <strong>LXMF address</strong> on a NomadNet page.' +
      '</span></div>';
    return;
  }
  inner.innerHTML = '';
  for (const conv of conversations) {
    const contact = _contacts.find(c => c.hash === conv.hash);
    const name    = contact ? contact.name : conv.hash.slice(0, 16) + '…';
    const lastMsg = conv.messages[conv.messages.length - 1];
    const preview = lastMsg
      ? (lastMsg.content || lastMsg.preview || '').slice(0, 45)
      : '';

    const el = document.createElement('div');
    el.className = 'conv-item' + (conv.unread ? ' conv-unread' : '');
    el.style.display = 'flex';
    el.style.alignItems = 'center';
    el.style.gap = '8px';
    const badge = conv.unread
      ? `<span class="inbox-badge">${conv.unread}</span>`
      : '';
    const convIcon = _contactIcon(contact, 36);
    el.innerHTML =
      (convIcon ? `<div style="width:36px;height:36px;flex-shrink:0;">${convIcon}</div>` : '') +
      `<div style="flex:1;min-width:0;">` +
        `<div class="conv-header">` +
          `<span class="conv-name">${esc(name)}${badge}</span>` +
          `<span class="conv-time">${formatAge(conv.lastTime)}</span>` +
        `</div>` +
        `<div class="conv-preview">` +
          (lastMsg?.direction === 'sent' ? '<span class="conv-you">You: </span>' : '') +
          esc(preview) + (preview.length === 45 ? '…' : '') +
        `</div>` +
      `</div>`;
    el.addEventListener('click', () => openConversation(conv.hash));
    inner.appendChild(el);
  }
}

function openConversation(hash) {
  _currentConvHash = hash;
  const conv    = _allConversations.find(c => c.hash === hash);
  const contact = _contacts.find(c => c.hash === hash);
  const name    = contact ? contact.name : hash.slice(0, 16) + '…';

  $('chat-contact-name').textContent = name;
  $('chat-contact-hash').textContent = hash;
  const iconSlot = $('chat-header-icon');
  if (iconSlot) {
    const hdrIcon = _contactIcon(contact, 32);
    iconSlot.innerHTML = hdrIcon;
    iconSlot.style.display = hdrIcon ? 'block' : 'none';
  }
  $('chat-list-view').hidden = true;
  $('chat-view').hidden      = false;
  // On mobile, the "Announce identity" block and Chats/Users tab bar sit
  // above the conversation and never shrink (flex-shrink: 0) — with the
  // keyboard open there often wasn't enough height left over for
  // #chat-log to show more than a sliver of the actual conversation.
  // Hide both while a conversation is open; restored on back/delete.
  $('sidebar-panel-messages').classList.add('chat-open');
  $('chat-dest-hidden').value = hash;
  renderChatLog(conv ? conv.messages : [], { forceScroll: true });

  // Mark all unread in this conversation as read
  if (conv && conv.unread > 0) {
    for (const m of conv.messages) {
      if (m.direction === 'received' && !m.read) {
        apiFetch(`/api/messages/received/${m.id}/read`, { method: 'POST' }).catch(() => {});
        m.read = true;
      }
    }
    conv.unread = 0;
    const totalUnread = _allConversations.reduce((n, c) => n + c.unread, 0);
    _updateUnreadBadges(totalUnread);
  }
}

// Robust "scroll to latest message". A single scrollTop = scrollHeight
// right after a render or focus can land short of the true bottom on
// mobile: if the on-screen keyboard is still animating open (the
// visualViewport / 100dvh height is still settling) or the DOM just
// changed, scrollHeight read in that same tick can be stale — so the
// scroll lands wherever the bottom was a frame ago, e.g. the last
// *received* message, one frame short of a just-sent one below it.
// Re-applying across a couple of animation frames catches the settled
// value instead of whatever layout looked like mid-transition.
function _scrollChatToBottom() {
  const log = $('chat-log');
  if (!log) return;
  log.scrollTop = log.scrollHeight;
  requestAnimationFrame(() => {
    log.scrollTop = log.scrollHeight;
    requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  });
}

// Human-readable byte size for attachment labels ("148 KB", "1.2 MB").
// Reuses the same rounding as _fmtBytes elsewhere in the file but
// scoped locally so this rendering doesn't couple to it.
function _fmtAttachmentSize(bytes) {
  if (!bytes || bytes <= 0) return '';
  if (bytes < 1024)                return `${bytes} B`;
  if (bytes < 1024 * 1024)         return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Render a single attachment as HTML for inclusion in a chat bubble.
// Kind-specific rendering (image inline, audio player, file link) —
// see docs/design/chat-uploads.md. All three kinds resolve to the
// same auth-gated endpoint (GET /api/messages/<id>/attachments/<idx>)
// which serves the blob with the correct Content-Type stored on the
// message metadata.
function _renderAttachment(msgId, att) {
  const url  = `/api/messages/${encodeURIComponent(msgId)}/attachments/${att.idx}`;
  const alt  = esc(att.filename || 'attachment');
  const size = _fmtAttachmentSize(att.size);
  const sizeLabel = size ? ` <span style="color:var(--text-dim);">${esc(size)}</span>` : '';

  if (att.kind === 'image') {
    // ``loading="lazy"`` so a long chat history doesn't hammer the
    // server with parallel image requests at scroll-into-view time.
    // Click opens full-size in a new tab.
    return (
      `<div class="chat-attachment chat-attachment-image">` +
        `<a href="${url}" target="_blank" rel="noopener noreferrer">` +
          `<img src="${url}" alt="${alt}" loading="lazy" ` +
               `style="max-width:100%;max-height:300px;` +
               `border-radius:4px;display:block;">` +
        `</a>` +
      `</div>`
    );
  }
  if (att.kind === 'audio') {
    // Native <audio controls> — the browser handles play/pause/seek/
    // volume for free. ``preload="none"`` avoids fetching the blob
    // until the user actually hits play, which matters for long
    // chats with many audio clips.
    const mime = esc(att.mime || 'audio/mpeg');
    return (
      `<div class="chat-attachment chat-attachment-audio">` +
        `<div style="font-size:11px;color:var(--text-dim);margin-bottom:2px;">` +
          `🎧 ${alt}${sizeLabel}` +
        `</div>` +
        `<audio controls preload="none" style="width:100%;max-width:320px;">` +
          `<source src="${url}" type="${mime}">` +
          `Your browser can't play this audio format.` +
        `</audio>` +
      `</div>`
    );
  }
  // Generic file — download link with filename + size.
  return (
    `<div class="chat-attachment chat-attachment-file">` +
      `<a href="${url}" target="_blank" rel="noopener noreferrer" ` +
         `download="${alt}" ` +
         `style="color:var(--accent);text-decoration:underline;">` +
        `📎 ${alt}${sizeLabel}` +
      `</a>` +
    `</div>`
  );
}

// Signature of the last conversation actually painted into #chat-log —
// lets renderChatLog skip the teardown/rebuild when polling brings back
// the exact same messages (the common case: nothing new arrived). A full
// rebuild re-creates every bubble, including inline <img>/<audio>
// attachments, which is expensive and — because it always ran through
// _scrollChatToBottom() unconditionally — also yanked the reader back to
// the bottom every ~15s (more often right after sending) even if they
// had scrolled up to read history. Both together read as "the messages
// tab is laggy". Reset to null on conversation switch so the new
// conversation always paints on first open.
let _chatLogSignature = null;

function renderChatLog(messages, { forceScroll = false } = {}) {
  const log = $('chat-log');
  if (!log) return;
  if (!messages.length) {
    log.innerHTML = '<div class="msg-empty" style="text-align:center;padding:20px 10px;">No messages yet — say hello!</div>';
    _chatLogSignature = '';
    return;
  }

  const signature = messages.map(m => `${m.id}:${m.state || ''}:${m.read}`).join('|');
  if (!forceScroll && signature === _chatLogSignature) return;
  _chatLogSignature = signature;

  // Only follow new messages down to the bottom if the reader was
  // already there (or this is a fresh conversation open / a message
  // they just sent) — otherwise a background poll refresh must not
  // move their scroll position while they're reading up-thread.
  const wasNearBottom = forceScroll ||
    (log.scrollHeight - log.scrollTop - log.clientHeight) < 80;

  log.innerHTML = '';
  for (const m of messages) {
    const bubble = document.createElement('div');
    bubble.className = `chat-bubble chat-bubble-${m.direction}`;

    const content = m.content || m.preview || '';
    let inner = '';
    if (m.title) inner += `<div class="chat-title">${esc(m.title)}</div>`;
    inner += `<div class="chat-content">${esc(content)}</div>`;
    // Inline any attachments below the content. Only received-message
    // attachments render today (step 2); sent-message attachments
    // land in step 4 when the send path exists.
    if (Array.isArray(m.attachments) && m.attachments.length) {
      for (const att of m.attachments) {
        inner += _renderAttachment(m.id, att);
      }
    }
    inner += `<div class="chat-meta"><span class="chat-time">${formatAge(m.time)}</span>`;
    if (m.direction === 'sent') {
      const cls = m.state === 'delivered' ? 'ok' : m.state === 'failed' ? 'fail' : 'pend';
      inner += ` <span class="chat-state-${cls}">${esc(m.state)}</span>`;
    }
    inner += '</div>';
    bubble.innerHTML = inner;
    log.appendChild(bubble);
  }
  if (wasNearBottom) _scrollChatToBottom();
}

$('btn-rename-chat').addEventListener('click', () => {
  if (!_currentConvHash) return;
  const contact  = _contacts.find(c => c.hash === _currentConvHash);
  const nameEl   = $('chat-contact-name');
  const current  = nameEl.textContent;
  const input    = document.createElement('input');
  input.type     = 'text';
  input.value    = current;
  input.style.cssText = 'flex:1;font-size:12px;padding:1px 4px;background:var(--bg);' +
    'border:1px solid var(--accent);color:var(--text);font-family:var(--font-mono);' +
    'border-radius:2px;outline:none;min-width:0;';
  nameEl.replaceWith(input);
  input.focus();
  input.select();
  const commit = async () => {
    const newName = input.value.trim() || current;
    if (newName !== current) {
      try {
        const d = await apiFetch('/api/contacts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ hash: _currentConvHash, name: newName }),
        });
        if (d.ok) {
          if (contact) contact.name = newName;
          else _contacts = [..._contacts, d.contact];
          renderConversationList(_allConversations);
          loadContacts();
        }
      } catch (err) {
        setStatus(`Could not rename: ${err.message}`, 'error');
      }
    }
    const span = document.createElement('span');
    span.id = 'chat-contact-name';
    span.style.cssText = 'flex:1;font-size:12px;color:var(--accent2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
    span.textContent = newName;
    input.replaceWith(span);
  };
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { input.value = current; input.blur(); }
  });
});

$('btn-chat-back').addEventListener('click', () => {
  _currentConvHash = null;
  $('chat-view').hidden      = true;
  $('chat-list-view').hidden = false;
  $('sidebar-panel-messages').classList.remove('chat-open');
  renderConversationList(_allConversations);
});

$('btn-delete-chat').addEventListener('click', async () => {
  if (!_currentConvHash) return;
  if (!confirm('Delete this entire conversation? This cannot be undone.')) return;
  const hash = _currentConvHash;
  try {
    await apiFetch(`/api/messages/conversation/${hash}`, { method: 'DELETE' });
    _allConversations = _allConversations.filter(c => c.hash !== hash);
    _currentConvHash = null;
    $('chat-view').hidden      = true;
    $('chat-list-view').hidden = false;
    $('sidebar-panel-messages').classList.remove('chat-open');
    renderConversationList(_allConversations);
    _updateUnreadBadges(_allConversations.reduce((n, c) => n + c.unread, 0));
    setStatus('Conversation deleted.', 'ok');
  } catch (e) {
    setStatus(`Delete failed: ${e.message}`, 'error');
  }
});

$('btn-chat-send').addEventListener('click', _sendChatMessage);

// Enter-to-send is wired through beforeinput, not keydown+preventDefault().
// keydown-based interception is a well-documented way to break mobile IME
// composition state: Gboard/Samsung Keyboard route ordinary typing through
// the same composition machinery Enter uses, and calling preventDefault()
// on that keydown stream desyncs the IME's internal cursor from the DOM's
// real one for the rest of the typing session — every following character
// then lands wherever the IME thinks the cursor still is (position 0),
// which is exactly "text types in backwards". beforeinput's
// 'insertLineBreak' only fires for a genuinely committed Enter — never
// mid-composition — so intercepting there instead never touches the IME's
// own event stream. Shift+Enter (newline, not send) isn't distinguishable
// from beforeinput's event alone, so a side-channel keydown/keyup pair
// tracks the modifier — those two listeners only ever set a flag, they
// never call preventDefault(), so they don't reintroduce the problem.
let _chatInputShiftHeld = false;
$('chat-input').addEventListener('keydown', e => {
  if (e.key === 'Shift') _chatInputShiftHeld = true;
});
$('chat-input').addEventListener('keyup', e => {
  if (e.key === 'Shift') _chatInputShiftHeld = false;
});
$('chat-input').addEventListener('beforeinput', e => {
  if (e.inputType !== 'insertLineBreak') return;
  if (_chatInputShiftHeld) return;   // Shift+Enter → let the newline through
  e.preventDefault();
  _sendChatMessage();
});

// Mirrors the 64 KB cap /api/messages enforces server-side
// (routes.py:api_message_send) so a message that's about to be rejected
// says so up front instead of silently failing after a round trip — what
// looked like "messages get truncated" was actually the sender's own copy
// being clipped to a 120-char preview in storage (fixed separately in
// messaging.py); this counter is for the one case where a real limit
// exists and is worth surfacing.
const CHAT_CONTENT_MAX     = 65536;
const CHAT_CONTENT_WARN_AT = Math.floor(CHAT_CONTENT_MAX * 0.9);
const CHAT_CONTENT_SHOW_AT = 500; // don't clutter the box for ordinary short replies

function _updateChatCharCount() {
  const el  = $('chat-char-count');
  if (!el) return;
  const len = $('chat-input').value.length;
  if (len <= CHAT_CONTENT_SHOW_AT) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = `${len.toLocaleString()} / ${CHAT_CONTENT_MAX.toLocaleString()}`;
  el.classList.toggle('chat-char-count-warn', len > CHAT_CONTENT_WARN_AT && len <= CHAT_CONTENT_MAX);
  el.classList.toggle('chat-char-count-over', len > CHAT_CONTENT_MAX);
}
$('chat-input').addEventListener('input', _updateChatCharCount);

// Scroll the conversation to the latest message the moment the reply box
// is focused (about to type) — otherwise, if the log had been scrolled up
// to read earlier history, opening the keyboard left that old scroll
// position in view instead of the message you're actually replying to.
$('chat-input').addEventListener('focus', _scrollChatToBottom);

// The focus-time scroll above fires as the keyboard *starts* opening, but
// on-screen keyboards animate in over a couple hundred ms, during which
// the visual viewport (and #chat-log's height along with it, since body
// is 100dvh) keeps changing. A scroll computed against any one frame of
// that transition can settle short of the real bottom once the keyboard
// finishes. Re-pinning on visualViewport's own resize event catches the
// end of that transition specifically, rather than guessing at a delay.
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', () => {
    const view = $('chat-view');
    if (view && !view.hidden) _scrollChatToBottom();
  });
}

// ---------------------------------------------------------------------------
// Chat attachments (v1.3.0 step 4 — paperclip UI)
// ---------------------------------------------------------------------------
// Staged before send; cleared after a successful POST /api/messages. Kept
// as a plain array (not a FormData) because FormData is write-only in
// browsers — we'd have no way to render chips / remove entries after
// staging. When the send fires, we build a fresh FormData from this list.
//
// Size caps mirror routes.py (_MAX_ATTACHMENT_COUNT, _attachment_max_bytes).
// The server re-checks defensively; the browser-side check exists to give
// the user a clear rejection ("this file is too big") instead of a 413.

const CHAT_ATTACH_MAX_BYTES = 500 * 1024;   // 500 KB per attachment
const CHAT_ATTACH_MAX_TOTAL = 500 * 1024;   // 500 KB total per message
const CHAT_ATTACH_MAX_COUNT = 10;
let _stagedAttachments = [];   // {file: File, size: number}

function _fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function _stagedTotalBytes() {
  return _stagedAttachments.reduce((sum, s) => sum + s.size, 0);
}

function _renderAttachChips() {
  const container = $('chat-attach-list');
  if (!container) return;
  if (!_stagedAttachments.length) {
    container.hidden = true;
    container.innerHTML = '';
    return;
  }
  container.hidden = false;
  const total = _stagedTotalBytes();
  const over  = total > CHAT_ATTACH_MAX_TOTAL;
  const chips = _stagedAttachments.map((s, i) => `
    <span class="attach-chip" style="display:inline-flex;align-items:center;
                                     gap:4px;background:var(--bg);
                                     border:1px solid var(--border);
                                     padding:2px 6px;margin:2px;
                                     border-radius:4px;font-size:11px;
                                     max-width:200px;">
      <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
            title="${esc(s.file.name)}">
        ${esc(s.file.name)}
      </span>
      <span style="color:var(--text-dim);">${_fmtBytes(s.size)}</span>
      <button type="button" data-attach-idx="${i}"
              style="background:none;border:none;color:var(--text-dim);
                     cursor:pointer;padding:0 2px;font-size:12px;line-height:1;"
              title="Remove">×</button>
    </span>
  `).join('');
  const counterColor = over ? 'var(--error, #d33)' : 'var(--text-dim)';
  container.innerHTML = `
    <div style="padding:2px 4px;">
      ${chips}
      <div style="font-size:10px;color:${counterColor};padding:2px 4px;">
        ${_stagedAttachments.length} file${_stagedAttachments.length === 1 ? '' : 's'},
        ${_fmtBytes(total)} / ${_fmtBytes(CHAT_ATTACH_MAX_TOTAL)}
      </div>
    </div>
  `;
  container.querySelectorAll('button[data-attach-idx]').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.getAttribute('data-attach-idx'), 10);
      _stagedAttachments.splice(idx, 1);
      _renderAttachChips();
    });
  });
}

function _clearStagedAttachments() {
  _stagedAttachments = [];
  const input = $('chat-attach-input');
  if (input) input.value = '';
  _renderAttachChips();
}

// Wire up the paperclip button + file input
{
  const attachBtn   = $('btn-chat-attach');
  const attachInput = $('chat-attach-input');
  if (attachBtn && attachInput) {
    attachBtn.addEventListener('click', () => attachInput.click());
    attachInput.addEventListener('change', () => {
      const picked = Array.from(attachInput.files || []);
      for (const file of picked) {
        if (_stagedAttachments.length >= CHAT_ATTACH_MAX_COUNT) {
          setStatus(
            `Too many attachments — max ${CHAT_ATTACH_MAX_COUNT} per message.`,
            'error',
          );
          break;
        }
        if (file.size > CHAT_ATTACH_MAX_BYTES) {
          setStatus(
            `"${file.name}" is ${_fmtBytes(file.size)} — cap is ` +
            `${_fmtBytes(CHAT_ATTACH_MAX_BYTES)}.`,
            'error',
          );
          continue;
        }
        _stagedAttachments.push({ file, size: file.size });
      }
      _renderAttachChips();
      // Reset the input so the same file can be re-picked after removal.
      attachInput.value = '';
    });
  }
}

async function _sendChatMessage() {
  const btn = $('btn-chat-send');
  // Re-entrancy guard. The Enter-key handler above calls this function
  // directly (not via a click on `btn`), so it doesn't get the browser's
  // built-in double-activation protection a disabled <button> gives clicks.
  // On a slow mobile connection a user hitting Enter twice before the first
  // request resolves — or a mobile keyboard delivering a duplicate Enter
  // keydown — re-entered this function while the field still held the same
  // unsent content, sending it twice.
  if (btn.disabled) return;

  const dest_hash = $('chat-dest-hidden').value;
  const content   = $('chat-input').value.trim();
  const hasAttach = _stagedAttachments.length > 0;

  // Empty send guard — either text OR attachments required.
  if (!dest_hash || (!content && !hasAttach)) return;
  if (content.length > CHAT_CONTENT_MAX) {
    setStatus(
      `Message is too long (${content.length.toLocaleString()} / ` +
      `${CHAT_CONTENT_MAX.toLocaleString()} characters) — trim it before sending.`,
      'error',
    );
    return;
  }
  if (hasAttach && _stagedTotalBytes() > CHAT_ATTACH_MAX_TOTAL) {
    setStatus(
      `Total attachment size ${_fmtBytes(_stagedTotalBytes())} exceeds ` +
      `${_fmtBytes(CHAT_ATTACH_MAX_TOTAL)} cap.`,
      'error',
    );
    return;
  }

  btn.disabled = true;
  try {
    if (hasAttach) {
      // Multipart branch — the server-side endpoint sniffs
      // request.content_type and switches parsers. Don't set
      // Content-Type manually: the browser must fill in the
      // multipart boundary parameter for us.
      const fd = new FormData();
      fd.append('dest_hash', dest_hash);
      fd.append('content', content);
      for (const s of _stagedAttachments) {
        fd.append('attachments', s.file, s.file.name);
      }
      await apiFetch('/api/messages', { method: 'POST', body: fd });
    } else {
      await apiFetch('/api/messages', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dest_hash, content }),
      });
    }
    $('chat-input').value = '';
    _clearStagedAttachments();
    _updateChatCharCount();
    setStatus('Message queued — delivery in progress.', 'ok');
    refreshChats();
    setTimeout(refreshChats, 8000);
    setTimeout(refreshChats, 25000);
    setTimeout(refreshChats, 60000);
    setTimeout(refreshChats, 95000);
  } catch (e) {
    setStatus(`Send failed: ${e.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Contacts (backing store — not directly shown in UI, used for name lookup)
// ---------------------------------------------------------------------------
let _contacts = [];

async function loadContacts() {
  if (!_authState.logged_in) return;
  try {
    const data = await apiFetch('/api/contacts');
    _contacts = data.contacts || [];
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// LXMF peer tracker (Users tab)
// ---------------------------------------------------------------------------
let _allPeers = [];
let _peerListWindow = { page: 1, frozen: null, resetKey: null };

async function refreshLxmfPeers() {
  if (!_authState.logged_in) return;
  try {
    const [data] = await Promise.all([apiFetch('/api/lxmf-peers'), loadContacts()]);
    _allPeers = data.peers || [];
    renderPeerList();
  } catch (_) {}
}

function renderPeerList() {
  const inner  = $('user-list-inner');
  const filter = ($('user-filter')?.value || '').trim().toLowerCase();
  if (!inner) return;
  const filtered = filter
    ? _allPeers.filter(p =>
        (p.name || '').toLowerCase().includes(filter) ||
        p.hash.toLowerCase().includes(filter))
    : _allPeers;

  if (!filtered.length) {
    inner.innerHTML = filter
      ? `<div class="msg-empty">No matches for &ldquo;${esc(filter)}&rdquo;.</div>`
      : '<div class="msg-empty">' +
        'No LXMF announces heard yet.<br>' +
        '<span style="color:var(--text-dim);font-size:11px;">' +
        'Users appear here as their announces propagate over the mesh.' +
        '</span></div>';
    return;
  }
  inner.innerHTML = '';
  // Windowed — see _windowList's own doc comment (also used by the node
  // list). A large mesh can hear thousands of announces; only the most
  // recent LIST_PAGE_SIZE render up front.
  const w = _windowList(filtered, _peerListWindow, filter);
  for (const peer of w.visible) {
    const contact = _contacts.find(c => c.hash === peer.hash);
    const name    = contact?.name || peer.name || peer.hash.slice(0, 16) + '…';
    const row = document.createElement('div');
    row.className = 'contact-item';
    row.style.cssText = 'cursor:pointer;display:flex;align-items:center;gap:8px;';
    // Falls back to {hash: peer.hash} so an announced peer who isn't a
    // saved contact yet still gets an identicon keyed to their real
    // hash, instead of no icon at all until the first click auto-adds
    // them as a contact below.
    const peerIcon = _contactIcon(contact || { hash: peer.hash }, 28);
    const hopsLabel = (peer.hops === null || peer.hops === undefined)
      ? '? hops'
      : peer.hops === 0
        ? 'local'
        : peer.hops === 1 ? '1 hop' : `${peer.hops} hops`;
    const hopsColor = (peer.hops === null || peer.hops === undefined)
      ? 'var(--text-dim)' : 'var(--accent2)';
    row.innerHTML =
      (peerIcon ? `<div style="width:28px;height:28px;flex-shrink:0;">${peerIcon}</div>` : '') +
      `<div style="flex:1;min-width:0;margin-left:${peerIcon ? 6 : 0}px;">` +
        `<div style="color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(name)}</div>` +
        `<div style="color:var(--text-dim);font-size:10px;">${peer.hash.slice(0, 20)}… · ${formatAge(peer.last_seen)}</div>` +
      `</div>` +
      `<span style="color:${hopsColor};font-size:10px;flex-shrink:0;margin-left:4px;font-variant-numeric:tabular-nums;" title="Hops away on the Reticulum network">${hopsLabel}</span>` +
      `<span style="color:var(--text-dim);font-size:10px;flex-shrink:0;margin-left:4px;" title="Announce count">×${peer.announce_count}</span>`;
    row.addEventListener('click', async () => {
      if (!_authState.logged_in) return;
      // Auto-create contact from peer name if not already known
      if (!contact && (peer.name || peer.hash)) {
        try {
          const d = await apiFetch('/api/contacts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hash: peer.hash, name: peer.name || '' }),
          });
          if (d.ok) _contacts = [..._contacts.filter(c => c.hash !== peer.hash), d.contact];
        } catch (_) {}
      }
      showSidebarPanel('messages');
      switchMsgTab('chats');
      openConversation(peer.hash);
    });
    inner.appendChild(row);
  }
  if (w.remaining > 0) {
    inner.appendChild(_makeLoadMoreRow(w.remaining, inner, () => {
      w.loadMore();
      renderPeerList();
    }, 'div'));
  }
}

$('user-filter').addEventListener('input', renderPeerList);

// ---------------------------------------------------------------------------
// Network tab — unified, filterable/searchable/sortable browser over
// every announce heard (sites, LXMF peers, mesh relays), modeled on the
// NomadPortal-Android sister project's own Network tab. Additive to the
// Nodes/Messages panels — those keep their own simple favorites/
// announces sections exactly as they already do; this is a second,
// unified view over the same underlying data (plus relays, which
// weren't surfaced anywhere in the UI before this).
// ---------------------------------------------------------------------------
let _allRelays = [];
let _networkTypeFilter = 'all'; // 'all' | 'site' | 'peer' | 'relay'
let _networkListWindow = { page: 1, frozen: null, resetKey: null };

async function refreshNetworkPanel() {
  // Sites/peers/relays are all public reads now (per explicit
  // direction, for "Network only" guest deployments — see
  // /api/lxmf-peers's own doc comment in routes.py) — fetch all
  // three regardless of auth state. loadContacts() already no-ops
  // for guests on its own (no account, no contacts), so calling it
  // unconditionally here is safe and simpler than gating it too.
  await Promise.all([
    loadContacts(),
    apiFetch('/api/lxmf-peers')
      .then(d => { _allPeers = d.peers || []; })
      .catch(() => {}),
    apiFetch('/api/relays')
      .then(d => { _allRelays = d.relays || []; })
      .catch(() => {}),
  ]);
  renderNetworkList();
}

// Signature of the last set of entries actually painted into
// #network-list — lets renderNetworkList skip the teardown/rebuild
// when a poll brings back the exact same data (the common case).
// Without this, wiring live-updating into pollStatus (see its own
// call site) would reintroduce the exact "messages tab is laggy"
// class of bug fixed earlier: a full rebuild every ~15s tears down
// every row and resets whatever scroll position the reader was at,
// even when nothing actually changed. Includes _networkListWindow's
// own page count so clicking "Load more" (same entries, one more
// page) isn't mistaken for a no-op.
let _networkListSignature = null;

function renderNetworkList() {
  const inner = $('network-list');
  if (!inner) return;
  const filterText = ($('network-filter')?.value || '').trim().toLowerCase();
  const sortKey    = ($('network-sort')?.value) || 'last_seen';
  const typeFilter = _networkTypeFilter;

  // Normalize all three announce kinds into one common row shape so
  // filter/search/sort/windowing only has to be written once.
  let entries = [];
  if (typeFilter === 'all' || typeFilter === 'site') {
    for (const n of _allNodes) {
      entries.push({
        kind: 'site', hash: n.hash, name: n.name,
        last_seen: n.last_seen, hops: n.hops,
        announce_count: n.announce_count,
        last_load_ok: n.last_load_ok, ever_load_ok: n.ever_load_ok,
      });
    }
  }
  if (typeFilter === 'all' || typeFilter === 'peer') {
    for (const p of _allPeers) {
      const contact = _contacts.find(c => c.hash === p.hash);
      entries.push({
        kind: 'peer', hash: p.hash, name: contact?.name || p.name,
        last_seen: p.last_seen, hops: p.hops,
        announce_count: p.announce_count,
      });
    }
  }
  if (typeFilter === 'all' || typeFilter === 'relay') {
    for (const r of _allRelays) {
      entries.push({
        kind: 'relay', hash: r.hash, name: null,
        last_seen: r.last_seen, hops: r.hops,
        announce_count: r.announce_count, picked: r.picked,
      });
    }
  }

  if (filterText) {
    entries = entries.filter(e =>
      (e.name || '').toLowerCase().includes(filterText) ||
      e.hash.toLowerCase().includes(filterText));
  }

  const signature = entries
    .map(e => `${e.kind}:${e.hash}:${e.last_seen}:${e.hops}:${e.announce_count}:${e.last_load_ok}:${e.picked}`)
    .join('|') + `#${filterText}|${typeFilter}|${sortKey}|${_networkListWindow.page}`;
  if (signature === _networkListSignature) return;
  _networkListSignature = signature;

  $('network-count').textContent =
    `${entries.length} announce${entries.length !== 1 ? 's' : ''}`;

  if (!entries.length) {
    inner.innerHTML = filterText
      ? `<li class="node-placeholder">No matches for &ldquo;${esc(filterText)}&rdquo;.</li>`
      : '<li class="node-placeholder">Waiting for announces…</li>';
    return;
  }

  if (sortKey === 'name') {
    entries.sort((a, b) =>
      (a.name || a.hash).toLowerCase().localeCompare((b.name || b.hash).toLowerCase()));
  } else if (sortKey === 'hops') {
    entries.sort((a, b) => {
      const ah = a.hops == null ? Infinity : a.hops;
      const bh = b.hops == null ? Infinity : b.hops;
      return ah - bh;
    });
  } else if (sortKey === 'announces') {
    entries.sort((a, b) => (b.announce_count || 0) - (a.announce_count || 0));
  } else {
    entries.sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
  }

  inner.innerHTML = '';
  // Windowed — see _windowList's own doc comment (shared with the node
  // list and the Users tab). resetKey covers every criterion this
  // render depends on; changing any of them snaps back to page 1.
  const w = _windowList(entries, _networkListWindow, `${filterText}|${typeFilter}|${sortKey}`);
  for (const entry of w.visible) inner.appendChild(makeNetworkItem(entry));
  if (w.remaining > 0) {
    inner.appendChild(_makeLoadMoreRow(w.remaining, inner, () => {
      w.loadMore();
      renderNetworkList();
    }, 'li'));
  }
}

function makeNetworkItem(entry) {
  const li = document.createElement('li');
  li.dataset.hash = entry.hash;

  const ringColor = entry.kind === 'site' ? _RING_SITE
    : entry.kind === 'peer' ? _RING_PEER : _RING_RELAY;
  const kindLabel = entry.kind === 'site' ? 'Site'
    : entry.kind === 'peer' ? 'Peer' : 'Relay';
  const name = entry.name || entry.hash.slice(0, 16) + '…';
  const hopsLabel = (entry.hops === null || entry.hops === undefined)
    ? '?'
    : entry.hops === 0
      ? 'local'
      : entry.hops === 1 ? '1 hop' : `${entry.hops} hops`;
  const hopsClass = (entry.hops === null || entry.hops === undefined)
    ? 'node-hops node-hops-unknown'
    : 'node-hops';

  const right = document.createElement('div');
  right.className = 'node-right';
  right.insertAdjacentHTML('beforeend',
    `<span class="${hopsClass}" title="Hops away on the Reticulum network">${hopsLabel}</span>` +
    // "picked" is the relay this app's own propagation sync is
    // currently using (PropagationSyncService's own doc comment) —
    // surfacing it here rather than leaving it a debug-only detail.
    `<span class="node-kind"${entry.picked ? ' title="Currently syncing through this relay"' : ''}>${kindLabel}${entry.picked ? ' ★' : ''}</span>`);
  li.appendChild(right);

  // Last-access status dot — same meaning as the Nodes panel's own
  // (page-fetch success/failure), so it only applies to sites: peers
  // and relays are announce-only, this app never "loads" from them.
  const dot = entry.kind === 'site' ? _nodeStatusDot(entry) : '';

  li.insertAdjacentHTML('beforeend',
    `<div class="node-icon-row">` +
      `<span class="node-identicon">${_identiconSvg(entry.hash, 22, ringColor)}</span>` +
      `<div class="node-text">` +
        `<span class="node-name">${dot}${esc(name)}</span>` +
        `<span class="node-hash">${entry.hash.slice(0, 24)}…</span>` +
        `<span class="node-age">${formatAge(entry.last_seen)}</span>` +
      `</div>` +
    `</div>`);

  if (entry.kind === 'site') {
    li.addEventListener('click', () => navigateTo(`hash://${entry.hash}/page/index.mu`));
  } else if (entry.kind === 'peer') {
    li.addEventListener('click', async () => {
      // Peers are guest-viewable now (see /api/lxmf-peers's own doc
      // comment), but messaging still genuinely needs a real identity
      // — give guests a real reason instead of the click doing
      // nothing with no explanation.
      if (!_authState.logged_in) {
        setStatus('Sign in to start a conversation.', 'error');
        return;
      }
      const contact = _contacts.find(c => c.hash === entry.hash);
      if (!contact) {
        try {
          const d = await apiFetch('/api/contacts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hash: entry.hash, name: entry.name || '' }),
          });
          if (d.ok) _contacts = [..._contacts.filter(c => c.hash !== entry.hash), d.contact];
        } catch (_) {}
      }
      showSidebarPanel('messages');
      switchMsgTab('chats');
      openConversation(entry.hash);
    });
  } else {
    // Relays are mesh infrastructure, not an addressable destination
    // for messaging or NomadNet browsing — no click action, matches
    // the node list's own .node-locked "inert row" cursor treatment.
    li.style.cursor = 'default';
  }
  return li;
}

$('network-filter').addEventListener('input', renderNetworkList);
$('network-sort')?.addEventListener('change', renderNetworkList);
$('btn-refresh-network').addEventListener('click', refreshNetworkPanel);
$('network-type-chips').addEventListener('click', e => {
  const chip = e.target.closest('.type-chip');
  if (!chip) return;
  _networkTypeFilter = chip.dataset.type;
  $('network-type-chips').querySelectorAll('.type-chip').forEach(c =>
    c.classList.toggle('active', c === chip));
  renderNetworkList();
});

// ---------------------------------------------------------------------------
// Compose modal (for new conversations)
// ---------------------------------------------------------------------------
function openComposeModal(destHash = '') {
  $('msg-dest').value        = destHash;
  $('msg-title').value       = '';
  $('msg-content').value     = '';
  $('msg-error').hidden      = true;
  $('msg-modal').hidden      = false;
}

// ---------------------------------------------------------------------------
// Modal event listeners
// ---------------------------------------------------------------------------
$('btn-msg-cancel').addEventListener('click', () => { $('msg-modal').hidden = true; });


$('welcome-msg-link').addEventListener('click', e => {
  e.preventDefault();
  openComposeModal(state.activeNodeHash || '');
});

$('btn-compose-new').addEventListener('click', () => openComposeModal(''));

$('btn-msg-send').addEventListener('click', async () => {
  const dest_hash = $('msg-dest').value.trim();
  const title     = $('msg-title').value.trim();
  const content   = $('msg-content').value.trim();
  const errEl     = $('msg-error');

  if (!dest_hash) { errEl.textContent = 'Enter a destination hash.'; errEl.hidden = false; return; }
  if (!content)   { errEl.textContent = 'Message cannot be empty.'; errEl.hidden = false; return; }

  $('btn-msg-send').disabled = true;
  $('btn-msg-send').textContent = 'Sending…';
  try {
    await apiFetch('/api/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dest_hash, title, content }),
    });
    $('msg-modal').hidden = true;
    setStatus('Message queued — delivery in progress.', 'ok');
    refreshChats();
    setTimeout(refreshChats, 8000);
    setTimeout(refreshChats, 25000);
    setTimeout(refreshChats, 60000);
    setTimeout(refreshChats, 95000);
  } catch (e) {
    errEl.textContent = e.message;
    errEl.hidden = false;
  } finally {
    $('btn-msg-send').disabled = false;
    $('btn-msg-send').textContent = 'Send ➤';
  }
});

// ---------------------------------------------------------------------------
// UI settings
// ---------------------------------------------------------------------------
function applyUISettings(s) {
  if (!s) return;

  const auth = _authState;

  // App title — app_title_html is Micron-rendered; app_title is plain fallback
  if (s.app_title_html || s.app_title) {
    document.querySelectorAll('.brand').forEach(el => {
      el.innerHTML = s.app_title_html || s.app_title;
    });
    document.title = s.app_title_plain || s.app_title || 'NomadNet';
  }

  if (s.allow_guest_external_browse) _allowGuestExternalBrowse = true;

  // Abuse contact in footer
  if (s.abuse_contact) {
    const ac = $('abuse-contact');
    if (ac) {
      ac.innerHTML = 'Report abuse: <a href="' +
        (s.abuse_contact.startsWith('http') ? '' : 'mailto:') +
        esc(s.abuse_contact) + '" style="color:var(--text-dim);">' +
        esc(s.abuse_contact) + '</a>';
      ac.hidden = false;
    }
  }

  // Per-audience access controls.
  //   super_admin → unrestricted (bypass all settings)
  //   admin       → reads admins_* fields
  //   user        → reads users_* fields
  //   guest       → reads guests_* fields
  const isSuper = !!auth.super_admin;
  const isAdmin = !!auth.is_admin && !isSuper;
  const isUser  = !!auth.logged_in && !auth.is_admin;
  const isGuest = !auth.logged_in;

  function pick(forGuests, forUsers, forAdmins) {
    if (isSuper) return null;        // super admin: no restrictions
    if (isAdmin) return forAdmins;
    if (isUser)  return forUsers;
    if (isGuest) return forGuests;
    return null;
  }

  // Address bar — enabled / disabled / hidden, per audience.
  // The refresh-page button is exempt from both restrictions: a visitor who
  // can see the page but not navigate elsewhere still needs to be able to
  // reload it.
  const nb = $('nav-bar');
  if (nb) {
    // Fail CLOSED: pick() returning a real state ('enabled'/'disabled'/
    // 'hidden') means settings loaded fine — use it as-is. `undefined`
    // only happens when the settings fetch failed; treat that as 'hidden'
    // rather than leaving the address bar enabled by default. `null` is
    // pick()'s explicit "no restriction" signal for super admins and must
    // stay untouched.
    const rawAb = pick(s.guests_address_bar, s.users_address_bar, s.admins_address_bar);
    const ab = rawAb === null ? null : (rawAb || 'hidden');
    if (ab === 'hidden') {
      nb.querySelectorAll('input, button').forEach(el => {
        if (el.id !== 'btn-refresh-page') el.hidden = true;
      });
    } else if (ab === 'disabled') {
      nb.querySelectorAll('input, button').forEach(el => {
        if (el.id === 'btn-refresh-page') return;
        el.disabled = true;
        el.style.opacity = '0.4';
        el.style.cursor  = 'not-allowed';
      });
      const bar = $('address-bar');
      if (bar) bar.style.color = 'var(--text-dim)';
    }
  }

  // Sidebar panels — bool per audience. Super admin always sees.
  // Fail CLOSED on `undefined` (settings fetch failed) — require an
  // explicit `true` rather than merely "not `false`".
  const showNodes    = isSuper || pick(s.guests_nodes_panel,    s.users_nodes_panel,    s.admins_nodes_panel)    === true;
  const showMessages = isSuper || pick(s.guests_messages_panel, s.users_messages_panel, s.admins_messages_panel) === true;
  const showNetwork  = isSuper || pick(s.guests_network_panel,  s.users_network_panel,  s.admins_network_panel)  === true;

  if (!showNodes) {
    const tab = $('sidebar-tab-nodes');
    if (tab) tab.hidden = true;
    const panel = $('sidebar-panel-nodes');
    if (panel) panel.hidden = true;
  }
  if (!showMessages) {
    const tab = $('sidebar-tab-messages');
    if (tab) tab.hidden = true;
    const panel = $('sidebar-panel-messages');
    if (panel) panel.hidden = true;
  }
  if (!showNetwork) {
    const tab = $('sidebar-tab-network');
    if (tab) tab.hidden = true;
    const panel = $('sidebar-panel-network');
    if (panel) panel.hidden = true;
  }

  // Activate the first visible panel
  if (!showNodes && showMessages)  showSidebarPanel('messages');
  if (!showNodes && !showMessages && showNetwork) {
    showSidebarPanel('network');
    // Real bug this fixes: showSidebarPanel() only makes the panel
    // visible, it doesn't fetch anything — the tab-click handler is
    // what normally calls refreshNetworkPanel() too, but this boot-time
    // auto-activation bypasses that handler entirely. Without this,
    // the panel sat on its static "Waiting for announces…" placeholder
    // until the first pollStatus tick happened to fire (setInterval
    // doesn't fire immediately — up to a full 15s after boot, on top
    // of however long the rest of boot itself took), even though sites
    // (_allNodes) were usually already populated by refreshNodes()
    // moments earlier in this same boot sequence.
    refreshNetworkPanel();
    // Network starts collapsed by default (for every audience it can
    // apply to — guests, regular users, or even an admin whose own
    // preset happens to land here too, not just guests) — per
    // explicit direction. Nodes/Messages don't get this treatment:
    // browsing those is the primary use case, so they stay expanded;
    // Network is more of an occasional reference tool, and this is
    // the one case where it's the ONLY tab available, with nothing
    // else to switch to for reclaiming the content area otherwise
    // (see #btn-sidebar-collapse's own comment).
    $('sidebar')?.classList.add('collapsed');
  }
  if (!showNodes && !showMessages && !showNetwork) {
    const tabs = $('sidebar-tabs');
    if (tabs) tabs.hidden = true;
    const sidebar = $('sidebar');
    if (sidebar) sidebar.hidden = true;
  }
}

// ---------------------------------------------------------------------------
// Desktop sidebar collapse — distinct from the mobile overlay
// toggle below. Matters most when only one sidebar tab is enabled
// for the current audience (e.g. guests with just the Network panel
// on): with nothing else to switch to, this is the only way to get
// the content area back to full width. No persistence — always
// starts expanded on a fresh load/reload, so an operator's kiosk
// display never comes up unexpectedly collapsed.
// ---------------------------------------------------------------------------
$('btn-sidebar-collapse')?.addEventListener('click', () => {
  $('sidebar')?.classList.toggle('collapsed');
});

// ---------------------------------------------------------------------------
// Mobile sidebar toggle
// ---------------------------------------------------------------------------
// Shared with navigateTo() so picking a node/favorite/link closes the
// full-page mobile overlay and reveals the page that was just navigated to,
// instead of leaving the list covering it. Safe to call unconditionally
// (including on desktop, where the sidebar is never given 'mobile-open' in
// the first place and #sidebar-backdrop doesn't exist) — both lookups
// no-op harmlessly when there's nothing to close.
function _closeMobileSidebar() {
  const sidebar  = $('sidebar');
  if (sidebar) sidebar.classList.remove('mobile-open');
  const backdrop = $('sidebar-backdrop');
  if (backdrop) backdrop.classList.remove('visible');
}

function _initMobileSidebar() {
  if (window.innerWidth > 640) return;
  const sidebar  = $('sidebar');
  const topbar   = $('topbar');
  if (!sidebar || !topbar) return;

  const toggle   = document.createElement('button');
  toggle.id      = 'btn-sidebar-toggle';
  toggle.title   = 'Toggle sidebar';
  toggle.textContent = '☰';
  topbar.insertBefore(toggle, topbar.firstChild);

  const backdrop = document.createElement('div');
  backdrop.id    = 'sidebar-backdrop';
  document.body.insertBefore(backdrop, document.body.firstChild);

  function open() { sidebar.classList.add('mobile-open'); backdrop.classList.add('visible'); }

  toggle.addEventListener('click',   () => sidebar.classList.contains('mobile-open') ? _closeMobileSidebar() : open());
  backdrop.addEventListener('click', _closeMobileSidebar);
}

// ---------------------------------------------------------------------------
// Disclaimer banner (shown once per session for logged-in users)
// ---------------------------------------------------------------------------
function _initDisclaimer() {
  const banner  = $('disclaimer-banner');
  const dismiss = $('btn-dismiss-disclaimer');
  if (!banner) return;
  const KEY = 'disclaimer_dismissed';
  if (_authState.logged_in && !sessionStorage.getItem(KEY)) {
    banner.hidden = false;
  }
  if (dismiss) {
    dismiss.addEventListener('click', () => {
      banner.hidden = true;
      sessionStorage.setItem(KEY, '1');
    });
  }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
// Top-left brand → navigate back to the default node's home page on
// click. Especially load-bearing for guests / kiosk mode ("Locked" access
// preset): the address bar and node list are both hidden by per-audience
// access controls in that mode, leaving the brand as the only reliable
// "home" affordance at all — it has to actually work, not just usually
// work. Attached synchronously here, before init()'s async boot sequence
// (auth/settings/site-info fetches) even starts, rather than gated behind
// it: the old code only wired this up once those awaits resolved, so a
// tap on the logo before boot finished — plausible on any slow/mesh-
// adjacent connection, and kiosk touchscreens are exactly the case where
// an impatient tap on the one available affordance is likely — did
// nothing, silently, forever (the listener didn't exist yet, and nothing
// ever re-attached it later). Reads _defaultHash/_hostedHash live at
// click time instead of a value captured once during boot, so it
// self-heals the moment either populates, however long boot takes.
document.querySelectorAll('.brand').forEach(el => {
  el.style.cursor = 'pointer';
  el.title = 'Home';
  el.addEventListener('click', () => {
    const home = _defaultHash || _hostedHash;
    if (!home) {
      setStatus('No home page configured yet.', 'error');
      return;
    }
    navigateTo(`hash://${home}/page/index.mu`);
  });
});

(async function init() {
  setStatus('Connecting…', 'busy');
  // Auth/settings/site-info are fetched (and lockdown state fully resolved)
  // BEFORE refreshNodes() populates and renders the node list. Rendering the
  // list first and locking it down afterward left a window — worse on
  // mobile, where JS execution and network round-trips are slower relative
  // to how quickly a visitor can tap — during which every node appeared
  // unlocked and clickable regardless of the configured access mode.
  const [, uiSettings] = await Promise.all([
    loadAuthState(),
    apiFetch('/api/ui/settings').catch(() => null),
  ]);

  let _siteInfo = null;
  try { _siteInfo = await apiFetch('/api/site/info'); } catch (_) {}
  if (_siteInfo && _siteInfo.node_hash) _hostedHash = _siteInfo.node_hash;

  // Determine the effective default/locked node hash.
  const effectiveDefault = (uiSettings && uiSettings.default_node) ||
                           (_siteInfo && _siteInfo.node_hash) || null;

  // Surface the configured default to the trusted-set check in navigateTo
  // so navigation to / between default + built-in never triggers the
  // external-node warning. Stored separately from _hostedHash because the
  // two may differ (operator can point default_node at a third-party node).
  if (uiSettings && uiSettings.default_node) {
    _defaultHash = uiSettings.default_node;
  }

  // Activate lockdown BEFORE any navigation (and before the node list is
  // ever rendered — see refreshNodes() below) so navigateTo() and the node
  // list can both enforce it from their very first render.
  // Super admin: never locked. Otherwise pick the per-audience field.
  // Fail CLOSED: if /api/ui/settings couldn't be loaded at all (network
  // hiccup — more common on mobile), uiSettings is null and the field
  // lookup below is `undefined`, not `false` — treated as "locked" rather
  // than silently granting unrestricted browsing.
  const _lockField =
    _authState.super_admin ? null :
    _authState.is_admin    ? 'admins_default_lock' :
    _authState.logged_in   ? 'users_default_lock' :
                             'guests_default_lock';
  const _shouldLock = !!_lockField && (uiSettings ? uiSettings[_lockField] : true) !== false;
  if (_shouldLock && effectiveDefault) {
    _lockedHash = effectiveDefault;
  }

  try { applyUISettings(uiSettings || {}); } catch (_) {}
  await refreshNodes();
  _initMobileSidebar();
  _initDisclaimer();

  const params = new URLSearchParams(location.search);
  const startUrl = params.get('url');

  // Boot-time URL resolution. Priority:
  //   1. Explicit ``?url=`` query param — preserved for the legacy
  //      share-link format and for any callers that construct URLs
  //      programmatically. Winning here means we still redirect
  //      the browser to the clean pathname form after navigation
  //      so the URL bar reflects reality.
  //   2. ``window.location.pathname`` — the "user hit refresh on a
  //      bookmarked page" case. ``_pathnameToUrl`` translates back
  //      to the canonical ``hash://...`` form navigateTo expects.
  //      Requires ``_defaultHash`` to be known (for bare-path
  //      resolution); if it isn't, fall through to the default
  //      boot flow.
  //   3. Nothing — the default boot flow.
  let _bootTarget = null;
  if (startUrl) {
    _bootTarget = decodeURIComponent(startUrl);
    // Strip the legacy ``?url=`` from the URL bar before navigating,
    // so back-button after subsequent navigation doesn't cycle back
    // to a URL that would re-trigger the boot handler.
    try { window.history.replaceState({}, '', '/'); } catch (_) {}
  } else if (window.location.pathname !== '/') {
    _bootTarget = _pathnameToUrl(window.location.pathname);
  }
  if (_bootTarget) {
    navigateTo(_bootTarget);
  } else {
    await _bootDefaultNavigation(_siteInfo, effectiveDefault);
  }

  // Handle browser back/forward. popstate fires when the user hits
  // the browser's back/forward buttons on URLs we pushed via
  // ``_syncBrowserUrl``. Translate the new pathname to a canonical
  // URL and navigate without pushing new history (that's what
  // popstate is — history's already moved).
  window.addEventListener('popstate', (ev) => {
    const targetUrl = (ev.state && ev.state.url) ||
                       _pathnameToUrl(window.location.pathname);
    if (targetUrl) navigateTo(targetUrl, false);
  });

  setInterval(pollStatus, 15000);
})();

// Boot navigation with fallback: try the configured default first, then the
// built-in node, then surface a generic welcome state. Both nodes are in
// the trusted set, so lockdown doesn't block either — no special-casing
// needed here. Subsequent user clicks from the visitor are still gated by
// the same trusted-set check (only the trusted nodes are reachable under
// lockdown; everything else hits the lock alert).
async function _bootDefaultNavigation(siteInfo, primaryHash) {
  if (!primaryHash) {
    setStatus('Ready — select a node or enter an address', 'ok');
    return;
  }

  if (await navigateTo(`hash://${primaryHash}/page/index.mu`, false)) return;

  const builtIn = siteInfo && siteInfo.node_hash;
  if (builtIn && builtIn.toLowerCase() !== primaryHash.toLowerCase()) {
    if (await navigateTo(`hash://${builtIn}/page/index.mu`, false)) return;
  }

  pageError.hidden = true;
  pageContent.innerHTML = '';
  addrBar.value = '';
  setStatus('Ready — select a node or enter an address', 'ok');
}

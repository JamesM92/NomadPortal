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
// Contact icon — real image from FIELD_ICON_APPEARANCE, or nothing
// ---------------------------------------------------------------------------
function _contactIcon(contact, size = 24) {
  if (!contact?.icon) return '';
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

  // Main list omits auto-favorites since they're already shown above.
  const byLastSeen = [...visible]
    .filter(n => !autoFavHashes.has(n.hash))
    .sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
  for (const node of byLastSeen) nodeList.appendChild(makeNodeItem(node));
}

function makeNodeItem(node) {
  const li = document.createElement('li');
  li.dataset.hash = node.hash;
  if (node.hash === state.activeNodeHash) li.classList.add('active');
  const age = formatAge(node.last_seen);
  const dot = node.last_load_ok === true
    ? '<span class="node-dot node-dot-ok"       title="Last access succeeded">●</span>'
    : node.last_load_ok === false && node.ever_load_ok
    ? '<span class="node-dot node-dot-degraded" title="Last access failed — has worked before">◑</span>'
    : node.last_load_ok === false
    ? '<span class="node-dot node-dot-err"      title="Last access failed — never successfully loaded">✕</span>'
    : '<span class="node-dot node-dot-none"     title="Never accessed">○</span>';

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
    `<span class="node-name">${dot}${esc(node.name)}</span>` +
    `<span class="node-hash">${node.hash.slice(0, 24)}…</span>` +
    `<span class="node-age">${age}</span>`);

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

  li.insertAdjacentHTML('beforeend',
    `<span class="node-name">${esc(fav.name)}</span>` +
    `<span class="node-hash">${fav.hash.slice(0, 12)}…${esc(fav.path)}</span>`);

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
  const m = url.match(/hash:\/\/([0-9a-f]+)\//i) ||
            url.match(/hash:\/([0-9a-f]+)\//i) ||
            url.match(/nomadnetwork:\/\/([0-9a-f]+)\//i);
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

  addrBar.value = _displayAddress(url);

  if (pushHistory) {
    state.history = state.history.slice(0, state.historyIndex + 1);
    state.history.push(url);
    state.historyIndex = state.history.length - 1;
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

// ---------------------------------------------------------------------------
// Raw toggle
// ---------------------------------------------------------------------------
toggleRaw.addEventListener('change', () => {
  // Swap displays from the cached fetch — never re-call the node.
  if (_lastPage.url) renderPageContent();
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
  $('sidebar-tab-nodes').classList.toggle('active',    name === 'nodes');
  $('sidebar-tab-messages').classList.toggle('active', name === 'messages');
}

$('sidebar-tabs').addEventListener('click', e => {
  const tab = e.target.closest('.sidebar-tab');
  if (!tab) return;
  const panel = tab.dataset.panel;
  showSidebarPanel(panel);
  if (panel === 'messages') {
    loadIdentities();
    refreshChats();
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

function _iconSvg(glyph, fg, bg, size = 28) {
  const g = (glyph || '?').slice(0, 2).toUpperCase();
  const fontSize = size * 0.55;
  const safeFg = _safeHexColor(fg, '#ffffff');
  const safeBg = _safeHexColor(bg, '#5ba3c9');
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" ' +
    `width="${size}" height="${size}">` +
    `<circle cx="16" cy="16" r="16" fill="${safeBg}"/>` +
    `<text x="16" y="22" text-anchor="middle" font-size="${fontSize * 32 / size}" ` +
    `font-family="sans-serif" font-weight="bold" fill="${safeFg}">${esc(g)}</text>` +
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
  slot.innerHTML = _iconSvg(
    $('icon-glyph').value,
    $('icon-fg').value,
    $('icon-bg').value,
    36,
  );
}

function _openIconEditor() {
  const ic = (_myIdentity && _myIdentity.icon) || _myIconDefaults();
  $('icon-glyph').value = ic.glyph;
  $('icon-fg').value    = ic.fg;
  $('icon-bg').value    = ic.bg;
  _renderIconEditorPreview();
  $('icon-editor').hidden = false;
}

if ($('btn-edit-icon')) {
  $('btn-edit-icon').addEventListener('click', _openIconEditor);
  $('btn-icon-cancel').addEventListener('click', () => { $('icon-editor').hidden = true; });
  ['icon-glyph', 'icon-fg', 'icon-bg'].forEach(id => {
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
  $('chat-dest-hidden').value = hash;
  renderChatLog(conv ? conv.messages : []);

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

function renderChatLog(messages) {
  const log = $('chat-log');
  if (!log) return;
  if (!messages.length) {
    log.innerHTML = '<div class="msg-empty" style="text-align:center;padding:20px 10px;">No messages yet — say hello!</div>';
    return;
  }
  log.innerHTML = '';
  for (const m of messages) {
    const bubble = document.createElement('div');
    bubble.className = `chat-bubble chat-bubble-${m.direction}`;

    const content = m.content || m.preview || '';
    let inner = '';
    if (m.title) inner += `<div class="chat-title">${esc(m.title)}</div>`;
    inner += `<div class="chat-content">${esc(content)}</div>`;
    inner += `<div class="chat-meta"><span class="chat-time">${formatAge(m.time)}</span>`;
    if (m.direction === 'sent') {
      const cls = m.state === 'delivered' ? 'ok' : m.state === 'failed' ? 'fail' : 'pend';
      inner += ` <span class="chat-state-${cls}">${esc(m.state)}</span>`;
    }
    inner += '</div>';
    bubble.innerHTML = inner;
    log.appendChild(bubble);
  }
  log.scrollTop = log.scrollHeight;
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
    renderConversationList(_allConversations);
    _updateUnreadBadges(_allConversations.reduce((n, c) => n + c.unread, 0));
    setStatus('Conversation deleted.', 'ok');
  } catch (e) {
    setStatus(`Delete failed: ${e.message}`, 'error');
  }
});

$('btn-chat-send').addEventListener('click', _sendChatMessage);
$('chat-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    _sendChatMessage();
  }
});

async function _sendChatMessage() {
  const dest_hash = $('chat-dest-hidden').value;
  const content   = $('chat-input').value.trim();

  if (!dest_hash || !content) return;

  const btn = $('btn-chat-send');
  btn.disabled = true;
  try {
    await apiFetch('/api/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dest_hash, content }),
    });
    $('chat-input').value = '';
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
  const visible = filter
    ? _allPeers.filter(p =>
        (p.name || '').toLowerCase().includes(filter) ||
        p.hash.toLowerCase().includes(filter))
    : _allPeers;

  if (!visible.length) {
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
  for (const peer of visible) {
    const contact = _contacts.find(c => c.hash === peer.hash);
    const name    = contact?.name || peer.name || peer.hash.slice(0, 16) + '…';
    const row = document.createElement('div');
    row.className = 'contact-item';
    row.style.cssText = 'cursor:pointer;display:flex;align-items:center;gap:8px;';
    const peerIcon = _contactIcon(contact, 28);
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
}

$('user-filter').addEventListener('input', renderPeerList);

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
    const ab = pick(s.guests_address_bar, s.users_address_bar, s.admins_address_bar);
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
  const showNodes    = isSuper || pick(s.guests_nodes_panel,    s.users_nodes_panel,    s.admins_nodes_panel)    !== false;
  const showMessages = isSuper || pick(s.guests_messages_panel, s.users_messages_panel, s.admins_messages_panel) !== false;

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

  // Activate the first visible panel
  if (!showNodes && showMessages)  showSidebarPanel('messages');
  if (!showNodes && !showMessages) {
    const tabs = $('sidebar-tabs');
    if (tabs) tabs.hidden = true;
    const sidebar = $('sidebar');
    if (sidebar) sidebar.hidden = true;
  }
}

// ---------------------------------------------------------------------------
// Mobile sidebar toggle
// ---------------------------------------------------------------------------
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

  function open()  { sidebar.classList.add('mobile-open');    backdrop.classList.add('visible'); }
  function close() { sidebar.classList.remove('mobile-open'); backdrop.classList.remove('visible'); }

  toggle.addEventListener('click',   () => sidebar.classList.contains('mobile-open') ? close() : open());
  backdrop.addEventListener('click', close);
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
(async function init() {
  setStatus('Connecting…', 'busy');
  // Fetch settings in parallel with auth/nodes, but apply after auth resolves
  // so visibility rules (_authState.logged_in / is_admin) are correct.
  const [, , uiSettings] = await Promise.all([
    refreshNodes(),
    loadAuthState(),
    apiFetch('/api/ui/settings').catch(() => ({})),
  ]);
  try { applyUISettings(uiSettings); } catch (_) {}
  _initMobileSidebar();
  _initDisclaimer();

  const params = new URLSearchParams(location.search);
  const startUrl = params.get('url');

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

  // Activate lockdown BEFORE any navigation so navigateTo() can enforce it.
  // Super admin: never locked. Otherwise pick the per-audience field.
  const _lockField =
    _authState.super_admin ? null :
    _authState.is_admin    ? 'admins_default_lock' :
    _authState.logged_in   ? 'users_default_lock' :
                             'guests_default_lock';
  const _shouldLock = _lockField && !!(uiSettings && uiSettings[_lockField]);
  if (_shouldLock && effectiveDefault) {
    _lockedHash = effectiveDefault;
  }

  if (startUrl) {
    navigateTo(decodeURIComponent(startUrl));
  } else {
    await _bootDefaultNavigation(_siteInfo, effectiveDefault);
  }

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

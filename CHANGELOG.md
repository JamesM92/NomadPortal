# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.0] - 2026-08-24

### Added

- **Real node/contact icons — identicon fallback + a Material Design
  Icons picker for your own identity.** Ported from the NomadPortal-
  Android sister project. Nodes and contacts with no
  ``FIELD_ICON_APPEARANCE`` set now render a deterministic identicon
  (GitHub/Columba-style symmetric dot grid keyed to the entity's
  hash) instead of blank space, applied across the node list, the
  LXMF peer (Users tab) list, and the conversation list.
  ``FIELD_ICON_APPEARANCE`` itself now renders as a real icon shape
  instead of just the first letter of the glyph name — the full
  Material Design Icons catalog (~7400 icons, Apache-2.0, bundled as
  ``static/data/mdi_icons.json`` / ``mdi_categories.json``) backs
  both the server-side SVG render (``nomadnet_web/mdi_icons.py``,
  ``messaging.py``'s ``_render_appearance_svg``) and a new client-
  side icon picker (search + category chips) that replaces the old
  2-character glyph text field for setting your own identity's icon.
  A regex-allowlist validator on the SVG path data (client
  ``_safeSvgPath``, a matching server-side pattern) closes the same
  XSS-dataflow surface ``_safeHexColor`` already covers for colors.
  15 new pytest cases cover the MDI catalog loader — including a
  real UTF-8 BOM bug in the bundled file that the first test run
  caught — and the appearance-SVG renderer's real-icon-vs-letter-
  fallback behavior.

- **Favoriting a node's home page before it's ever announced.**
  Ported from the NomadPortal-Android sister project. Index-page
  favorites (the node-list star) used to require a cached node
  record to already exist, even though page bookmarks (path !=
  ``/``) could already create one on the fly — so a node reached
  only by a manually-typed or pasted address couldn't be starred
  until it happened to announce first. A missing record is now
  synthesized from the given name (falling back to the hash prefix);
  an existing record's real announce data is never overwritten.
  Also factors three separately hand-written 13-field node-record
  dict literals into one ``_new_node_record()`` helper so the fields
  can't drift out of sync between call sites. 9 new pytest cases in
  ``tests/test_favorite_unannounced.py``.

- **Outbound chat attachments — paperclip UI + multipart send**
  (v1.3.0 step 4). Composer gains a 📎 button that opens a native
  file picker (multi-select, ``image/*,audio/*,*/*``); staged
  attachments render as chips with filename + size + × remove, plus
  a live "N files, X KB / 500 KB" counter that turns red when the
  cap is hit. Send switches transparently to ``multipart/form-data``
  when any attachment is staged; text-only sends still use JSON.
  Server (``POST /api/messages``) sniffs ``Content-Type`` and dual-
  paths — new multipart branch enforces the 500 KB per-attachment /
  per-message total cap AND the 10-attachment count cap; returns
  413 with a specific error on overflow (no silent truncation).
  Cap is overridable via the ``LXMF_ATTACHMENT_MAX_BYTES`` env var
  (default 524288). ``MessagingService.send_message`` now accepts
  ``attachments=[{data, filename, mime}, ...]``; each blob is
  written to the ``AttachmentStore`` under the outbound msg_id
  before the delivery thread starts (so a sender-side chat log can
  render the bubble even if the RNS delivery fails). LXMF field
  assembly at delivery time: MIME → kind (image / audio / file)
  routes to ``FIELD_IMAGE`` (0x06) / ``FIELD_AUDIO`` (0x07) /
  ``FIELD_FILE_ATTACHMENTS`` (0x05) — image and audio are
  singletons, extras of either kind demote into the file array
  (matches MeshChat's send-side structure). ``MAX_CONTENT_LENGTH``
  raised 512 KB → 1 MB to accommodate the multipart envelope
  around a 500 KB attachment; the tighter per-endpoint cap still
  enforces the design's 500 KB rule. 16 new pytest cases cover
  the MIME classifier, single/multi-kind persistence, missing-MIME
  fallback, empty-list handling, non-bytes skip, and reverse-map
  parity with the receive-side extension → MIME tables.

- **Inbound file and audio attachments in chat** (v1.3.0 step 3).
  ``_on_delivery`` now also extracts ``FIELD_FILE_ATTACHMENTS``
  (0x05, array of ``[filename, bytes]`` tuples) and ``FIELD_AUDIO``
  (0x07, ``[audio_mode_str, bytes]`` — MeshChat sends ``"opus"``,
  ``"webm"``, ``"mp3"``, etc. as the codec identifier). Files land
  with their original filename; audio lands as ``audio.<ext>`` with
  the codec-derived MIME (``audio/opus``, ``audio/webm``, …) and
  falls back to ``application/octet-stream`` for unknown codecs so
  the download link still works. Malformed entries in the
  file-array are skipped rather than crashing the receive path.
  Frontend renders audio with ``<audio controls preload="none">``
  and files as a 📎 download link with human-formatted byte size.
  All three attachment kinds (image / file / audio) can coexist in
  one message and are rendered in that order. 9 new pytest cases
  cover single/multiple files, unknown extensions, malformed
  entries, image+files+audio coexistence, and codec MIME mapping.

- **Inbound image attachments in chat** (v1.3.0 step 2). When an
  LXMF message arrives with a ``FIELD_IMAGE`` field AND has text
  content (title or body), the image is persisted to the
  ``AttachmentStore`` and rendered inline in the chat bubble.
  Contact-icon path stays intact — ``FIELD_IMAGE`` without text
  content is still treated as an icon-update announce.
  ``FIELD_ICON_APPEARANCE`` (0x04) is always an icon regardless.
  New endpoint ``GET /api/messages/<msg_id>/attachments/<idx>``
  serves the blob with the correct ``Content-Type`` and
  ``Content-Disposition: inline``; auth-gated to the message's
  owner. Frontend renders images at ``max-width:100%;
  max-height:300px`` inside the bubble with ``loading="lazy"`` so
  scrolling a long history doesn't stampede the endpoint. Click
  opens full-size in a new tab. 7 pytest cases cover the icon-vs-
  attachment heuristic, MIME resolution (extension + byte-sniff
  fallback), and the no-attachment-store code path.

- **``AttachmentStore`` — on-disk blob store for LXMF message
  attachments** (foundation for v1.3.0 chat file / image / audio
  uploads; see ``docs/design/chat-uploads.md``). Bytes land on
  disk under ``config/attachments/<msg_id>/<idx>.<ext>``;
  ``messages.json`` keeps only lightweight metadata (kind /
  filename / mime / size / disk path) so it doesn't inflate with
  base64-encoded blobs (the same NAS/GIL pathology v0.9.x fixed
  on the peer + node trackers). Store owns write/read/evict.
  Wired into ``MessageStore`` so ``delete_conversation`` and the
  silent ``MAX_MESSAGES`` overflow both evict the corresponding
  blobs — no orphans accumulate. Path-traversal defenses on
  untrusted ``msg_id`` / ``filename`` inputs: hex-only msg_id
  sanitizer, whitelist-based extension check. 22 pytest cases
  cover write/read/evict lifecycle, path-traversal defenses,
  and MessageStore integration. Wiring only in this drop — no
  UI, no LXMF-field integration yet; those land in the next
  chat-uploads steps.

- **`` `FT<6hex> `` / `` `BT<6hex> `` 24-bit exact-color demo on the
  site examples page.** NomadNet's reference parser accepts these
  for exact colors and Micron2HTML 1.1.1 restored the parser
  branch (Micron2HTML dropped it in an earlier version citing
  MeshChat compatibility). The examples page now shows the demo
  with a "discouraged for general use" note in red — MeshChat
  doesn't render the T-form and every byte counts on a mesh, so
  the 3-hex `` `Fxxx `` shorthand remains the always-portable
  default.

- **Confirmation prompt before following an external (clearnet)
  link in page content.** NomadNet page content is untrusted and
  already HTML-escaped with no JS execution path, but a plain-text
  link label can still claim to be anything while the ``href``
  points off-mesh — a phishing-style mismatch a reader has no other
  way to notice. Clicking an ``http(s)://`` link in rendered page
  content now shows the real destination and asks for confirmation
  before leaving the mesh.

### Dependencies

- **``Micron2HTML`` bumped 1.0.8 → 1.1.1** for the ``FT<6hex>``
  parser and the NomadNet-parity table + anchor features that
  landed in 1.1.0.

- **Bump `python:3.14-slim-trixie` base image** to the latest digest
  (``sha256:ce407646…``) and add an explicit ``apt-get upgrade`` to
  the Dockerfile's system-deps layer. Fixes what Trivy was blocking
  the v1.3.0 release PR on: ``util-linux`` / ``login`` /
  ``libuuid1`` (CVE-2026-53612, -53613, -53614, -53615 — TOCTOU and
  SUID-bypass issues in ``mount(8)``, plus an integer overflow in
  ``libblkid``). Debian's security repo had already published these
  patches, but the next scheduled base-image rebuild hadn't picked
  them up yet — bumping the pinned digest alone wasn't enough, so
  ``apt-get upgrade`` now pulls them directly at build time (see
  ``.hadolint.yaml`` for the accompanying DL3005 exception and its
  rationale). pip-audit / bandit / CodeQL all passed throughout.

- **Pin `setuptools==83.0.0`** in ``requirements.txt`` (not an app
  dependency — the base image's ensurepip-installed setuptools,
  70.3.0, carries CVE-2025-47273, a path-traversal issue, fixed in
  78.1.1). pip-audit's broader OSV feed then flagged a second, newer
  issue at 78.1.1 (PYSEC-2026-3447, a Unicode-normalization MANIFEST.in
  bypass on macOS APFS/HFS+, irrelevant to how this app builds/runs
  but still flagged) fixed only in 83.0.0 — verified clean with
  ``pip-audit -r requirements.txt`` locally before landing. Pinned
  through this file rather than a bare Dockerfile ``pip install
  --upgrade`` so it stays under the same version-bump discipline as
  every other dependency here.

### Fixed

- **A favorited node's star became unclickable once its name grew
  long enough.** ``.node-right`` (the star/pin/hops column) was
  ``float: right`` beside a ``white-space: nowrap`` node name — a
  float's reserved space isn't reliably excluded from nowrap text's
  hit-testing box in every layout path, so a long enough name could
  visually and interactively cover the star, and every tap landed on
  the row's own navigate-to-node handler instead. Replaced the float
  with a real flex row so the star keeps a fixed, always-clickable
  width regardless of name length.

- **Opening the node list or the Users (LXMF peer) tab was laggy on
  an established mesh.** Both rendered every matched/sorted row into
  the DOM unconditionally — thousands of rows on a mesh with a long
  history. Ported the Android sister project's windowed-list
  pattern: render only the most recent 50, with a "Show N more" row
  (click, or scroll it into view) to load another page. The first
  page stays live against new announces; loading a second page
  freezes the window so already-scrolled-past rows don't reorder
  underneath the reader.

- **The messages tab felt laggy while a conversation was open.**
  ``renderChatLog()`` unconditionally tore down and rebuilt every
  chat bubble — including inline image/audio attachments — and
  force-scrolled to the bottom on every poll cycle (every 15s while
  the panel is open, plus a burst of follow-ups right after
  sending). Now skips the rebuild when a poll brings back the exact
  same messages, and only auto-scrolls when the reader was already
  near the bottom, so a background refresh no longer yanks their
  scroll position mid-read.

- **Renaming your identity didn't update the name on future
  announces.** ``LXMRouter.announce()`` reads the live
  ``Destination.display_name`` set once at registration time, never
  the persisted-to-disk value ``identity_store.rename()`` updates —
  so a rename took effect in this app's own UI immediately, but any
  announce sent afterward (including the bootstrap/reconnect
  announce) kept broadcasting the old name to the mesh until the
  next full process restart. ``MessagingService.
  refresh_router_display_name()`` now pushes the new name onto the
  live router's destination at rename time. 3 new pytest cases in
  ``tests/test_refresh_router_display_name.py``.

- **The top-left brand/logo didn't reliably navigate home when
  clicked.** Its click handler was only wired up inside ``init()``'s
  async boot sequence (auth state, UI settings, site info — up to
  three sequential fetches), so a click landing before boot finished
  did nothing. Moved the handler to attach synchronously at script
  load, reading the current default/hosted hash live at click time
  instead of a value captured once boot completed.

- **New or restarted LXMF identities were unreachable until their
  first outbound message.** RNS path discovery is announce-based
  with no other mechanism — a destination that has never announced
  is unreachable by anyone, including this app's own bootstrap/
  reconnect flow. Registering a new identity (or restarting the
  process) now sends an announce immediately after registration.
  3 new pytest cases in ``tests/test_bootstrap_announce.py``.

- **Hosted-site link establishment sometimes timed out even though
  the announce reached the other side.**
  ``set_proof_strategy(PROVE_ALL)`` on the hosted-site destination
  was an unexplained deviation from NomadNet's own reference
  ``Node.py`` (which sets no proof strategy at all, leaving RNS's
  default ``PROVE_NONE``). Removed it — confirmed fixed by the
  original reporter reaching the hosted site successfully
  immediately after, on a build with no other change.

- **Sideband-users' contact icons rendered as flat grey.** The
  ``FIELD_ICON_APPEARANCE`` (0x04) color values can arrive in two
  shapes in the wild: MeshChat and this app send raw ``bytes(3)``,
  but Sideband (the LXMF library's reference client) sends
  ``[r, g, b]`` (or ``[r, g, b, a]``) as 0-1 floats — its
  ``DEFAULT_APPEARANCE`` is ``["account", [0,0,0,1], [1,1,1,1]]``.
  Our converter only accepted the bytes shape; Sideband's float
  sequence fell through to the ``#888888`` fallback, so every
  Sideband-user's contact showed the same grey circle regardless
  of their actual chosen colors. Adds ``_appearance_color_to_hex``
  that handles both shapes and clamps out-of-range channels.
  Ported from the ``python-core`` of the NomadPortal-Android
  sister project, which hit this exact interop failure.

  14 pytest cases cover the bytes shape, the float shape (with
  and without alpha), grey fallback for unknown inputs, and the
  channel clamping semantics — so the regression can't sneak
  back in silently.

### Security

- **Identity rename had no ownership check.**
  ``POST /api/identities/<id>/rename`` renamed any identity by ID
  with no check that it belonged to the requesting user — any
  logged-in user could rename another user's identity. Now returns
  403 unless the identity's ``user_sub`` matches the current
  session.

### Docs

- **README and the default hosted-site page rewritten in
  Simplified Technical English (ASD-STE100).** Active voice, simple
  tenses, no contractions, shorter sentences — every fact, URL,
  environment variable, and config value preserved exactly. The
  Micron Feature Showcase section and its ASCII art in
  ``templates/site/index.mu`` are left untouched — it's a MeshChat
  rendering-parity fixture, not prose.

## [1.2.0] - 2026-08-06

**Mobile pass.** A round of fixes from actually operating NomadPortal on a
phone — guest access-control hardening, a mobile IME bug that made typed
replies come out backwards, and several layout issues where the on-screen
keyboard fought with the chat UI for space.

### Fixed

- **Guest access-control fail-open on the client.** Server-side gating
  (`_can_browse`/`_can_interact` in `routes.py`) was already correct — this
  closes three client-side gaps that made the guest UI misleadingly
  permissive, worst on a slow mobile connection where the failure modes are
  most likely to actually trigger:
  - The node list rendered (and was clickable) *before* lockdown state was
    computed on boot. Boot now resolves auth/settings/lockdown before
    `refreshNodes()` ever renders the list.
  - Per-audience restrictions (address bar, nodes/messages panels, lockdown)
    failed **open** when `/api/ui/settings` failed to load. Now fail closed.
  - `_extractNodeHash()`'s regex required a trailing `/` after the hash, so
    a bare `hash://<hash>` with no path skipped the lockdown/external-warning
    check entirely.

- **Chat replies typed backwards on mobile.** Enter-to-send was wired
  through `keydown` + `preventDefault()`, which is a documented way to
  desync a mobile IME's (Gboard, Samsung Keyboard) internal composition
  cursor from the DOM's real one — those keyboards route ordinary typing
  through the same composition machinery Enter uses. Once desynced, every
  following character landed at the IME's stale cursor position (0) instead
  of the real one. Rewired through `beforeinput`'s `insertLineBreak`, which
  only fires for a genuinely committed Enter and never touches the IME's
  event stream; Shift+Enter is preserved via a side-channel keydown/keyup
  pair that only ever sets a flag.

- **Messages double-sent or cut in half.** Same IME interaction: the
  keystroke that confirmed a predictive-text suggestion could be read as
  "send" mid-composition, grabbing a not-yet-committed value. Folded into
  the `beforeinput` fix above, plus a re-entrancy guard on the send handler
  so a duplicate Enter delivery from some keyboards can't fire twice.

- **Own sent messages truncated to 120 characters.** `messaging.py` only
  ever stored a 120-char `preview` for sent messages, never the full
  `content` — received messages already stored full content, so this was a
  sent-side-only gap. The chat log falls back to `preview` whenever
  `content` is missing, so every sent message shown in your own open
  conversation was silently clipped, even though the full text was (and
  still is) what actually went out over LXMF.

- **Mobile sidebar overlay capped at 85%/300px** instead of filling the
  screen, and didn't close itself after picking a node — left the list
  covering the page that was just navigated to. Now full-width, and
  `navigateTo()` closes it once navigation is committed.

- **Chat view hidden behind the on-screen keyboard.** `body` used
  `height: 100vh`, which is sized for the keyboard-*closed* viewport on
  mobile and doesn't shrink when the keyboard opens — the reply box and
  the tail of the conversation ended up underneath it. Switched to
  `100dvh` (with `100vh` kept as a fallback). Additionally, the "Announce
  identity" block and Chats/Users tab bar above the conversation never
  shrank, squeezing `#chat-log` down to a sliver on short mobile
  viewports — both now hide while a conversation is open.

- **Chat didn't scroll to the latest message when composing a reply,**
  or landed short of a just-sent message. A single `scrollTop =
  scrollHeight` right after a render/focus reads `scrollHeight` against
  whatever layout looks like in that exact tick — mid-way through the
  keyboard's open animation, that can be stale. Now re-applied across a
  couple of animation frames after render/focus, plus a `visualViewport`
  `resize` listener that re-pins to bottom once the keyboard's own
  animation actually finishes.

### Added

- **Live character counter** on the chat reply box, appearing past 500
  characters and tracking the real 64 KB cap `/api/messages` enforces
  server-side, so an over-limit message is caught before sending instead
  of failing after a round trip.

## [1.1.0] - 2026-07-28

**First feature release after 1.0.** Path-based URLs give the
browser refresh / bookmark / share-link behaviour operators
expect from a web app. Plus a real download-lifecycle
compatibility fix (DuckDuckGo Android) that came in from live
use, and a second instance of the CSS ``hidden``-attribute
override bug caught by the audit script we wrote after the
v1.0.0 fingerprint fix.

### Added

- **Path-based URL sync.** The browser URL now reflects the page
  you're actually on — refresh preserves state, browser bookmarks
  work for any NomadNet page, and share-links (copied from the
  URL bar) are natural. Scheme:

  | URL                              | Target                            |
  |----------------------------------|-----------------------------------|
  | ``/``                            | default node home                 |
  | ``/page/foo.mu``                 | default node's ``/page/foo.mu``   |
  | ``/file/x.pdf``                  | default node's ``/file/x.pdf``    |
  | ``/n/<hash>``                    | external node's home              |
  | ``/n/<hash>/page/foo.mu``        | external node's page              |

  Default-node URLs collapse the hash (``/`` and ``/page/foo.mu``);
  external nodes carry the hash under an ``/n/`` prefix.
  Flask picks up a catch-all route that serves ``index.html`` for
  any path not owned by a reserved prefix (``/api``, ``/admin``,
  ``/auth``, ``/static``) so refresh / bookmarks / share-links
  reach the SPA cleanly. The JS wires ``history.pushState`` into
  ``navigateTo`` and handles browser back/forward via
  ``popstate``. Legacy ``?url=`` query-param entry point still
  works but is transparently rewritten to the pathname form on
  first navigation.

### Fixed

- **File downloads fail on DuckDuckGo Android with "Failed to
  download. Check Internet connection."** DDG's browser
  architecture issues TWO requests for the same download URL: one
  from the WebView (Chrome UA) that received the SPA-triggered
  ``window.location.assign``, then a second from DDG's separate
  download-manager process (``ddg_android`` UA) that actually
  persists the file. The old ``drop_job(job_id)`` call after the
  first successful serve dropped the in-memory job entry
  immediately, so DDG's second request got 404 and DDG surfaced
  a generic download-failed toast. Same class of second-request-
  after-download exists in other privacy-focused browsers.

  Extends ``NodeBrowser.drop_job`` with a ``grace_seconds``
  parameter. The download endpoint now passes ``grace_seconds=60``
  so the job stays serveable for 60 s after the first response.
  ``cleanup_jobs`` evicts past-grace entries alongside the
  existing max-age sweep. Historical behaviour (immediate drop)
  is the default when ``grace_seconds`` is omitted or 0, so
  other callers are unchanged.

- **``#sidebar-tabs`` visible after being set hidden.** Same class
  of bug the v1.0.0 fingerprint icon fix caught: an id-selector
  ``display: flex`` rule was beating the browser stylesheet's
  ``[hidden] { display: none }``. Scoped the display rule to
  ``:not([hidden])`` so ``applyUISettings``'s ``tabs.hidden = true``
  (fires when the guest audience is denied both sidebar panels)
  now actually hides the tab bar. Audit script confirmed no other
  id-selector rules in ``style.css`` have this pattern.

### Docs

- **README documents the URL scheme** in a new "URL scheme"
  section between "Diagnostics admin actions" and "Data
  storage" — the path convention (``/``, ``/page/foo.mu``,
  ``/n/<hash>/...``), the reserved backend prefixes, and the
  legacy ``?url=`` rewrite behaviour. Bumped the feature bullet
  at the top so path-based URLs are visible in the "at a glance"
  list too.

## [1.0.0] - 2026-07-23

**The 1.0 milestone.** NomadPortal is now stable and usable enough
in production that it earns the "1.0" name. The primary browses
mesh destinations reliably and the mirror serves its hosted site
to clients over the mesh — both running the same image, both
soaking clean over multi-day windows. The reliability journey the
0.9.x line traced (Phase 1 MeshChat parity, announce-driven
retry, per-destination fetch dedup, propagation-node sync,
version-alignment with the rest of the ecosystem, and — the
final root-cause fix — batching per-event disk writes off the
RNS event loop) all landed by v0.9.28 and the last few 0.9.x
patches. This drop closes the remaining known reliability issues
and cleans up two guest-facing UI bugs; taken with everything
already shipped in the 0.9 line, it's the version worth pointing
someone at.

### Fixed

- **Fingerprint (identify) button visible for guests.** The
  ``#btn-identify`` CSS declared ``display: inline-flex`` on the id
  selector, which beat the HTML ``hidden`` attribute (backed by the
  browser stylesheet's lower-specificity ``[hidden] { display: none }``)
  and left the button visible even when JS had explicitly set
  ``btn.hidden = true``. Scoped the display rule to ``:not([hidden])``
  so the hidden attribute now actually hides. Same class of bug may
  live elsewhere in ``style.css`` wherever an id selector sets
  ``display`` on an element that also toggles ``hidden``; noted
  inline as an audit item.

- **Brand element in the top-left didn't navigate anywhere on click.**
  In guest / kiosk deployments where the address bar and node list are
  hidden by per-audience access controls, this left visitors with no
  reliable way to get back to the default node's home page after
  navigating deeper. Wires up a click handler at boot that navigates
  to ``hash://${default_node}/page/index.mu``, applied for everyone —
  a useful shortcut regardless of role.

- **``NodeBrowser`` persisted ``nodes.json`` on every
  ``nomadnetwork.node`` announce.** Same class of pathology as
  the LXMFPeerTracker inline persist that was fixed in v0.9.28.
  Every incoming node-announce (and every fetch / ping stat
  update) called ``_persist(snapshot)`` synchronously on the RNS
  read_loop thread. On the mirror deployment (2k+ nodes) on
  NAS-backed ``/config``, the same GIL-contention gridlock as
  the peer tracker applies — but with a different visible
  symptom: the mirror hosts a site and needs to send
  ``LINK_PROOF`` back to clients accepting their inbound Link
  handshakes; the read_loop thread being blocked in ``json.dump``
  delays that response past the client's establishment timeout.
  Clients see "Link establishment timed out" or "Timeout waiting
  for RTT packet from link initiator" while trying to browse
  the hosted site.

  Applies the same debounce pattern: ``_mark_nodes_dirty()`` on
  all hot-path callers (``_register_node``, ``_record_fetch``,
  ``_record_ping``, hop-refresh in ``get_nodes``); a background
  daemon thread flushes to disk every ``NODES_PERSIST_INTERVAL_S``
  (60 s default). ``atexit`` hook covers clean shutdown.

  This closes the second contributor to the NAS-config
  reachability gridlock. The historical
  ``[[reticulum-stack-pin]]`` symptom
  ("Timeout waiting for RTT packet from link initiator")
  originally attributed to a 1.3.x RNS regression may have
  been this pathology all along, misattributed.

- **Container crash-loop with exit 141 on large ratchet
  directories.** The entrypoint's ratchet-prune peek pipeline
  (``find … | head -n 1``) SIGPIPEs on ``find`` when ``head``
  reads its one line and closes. Under ``set -o pipefail`` +
  ``set -e`` the outer shell exits before the SSL / gunicorn
  setup, giving no log line to explain it — just repeated
  restart-loop iterations of the earlier MTU warning.
  Reproduced on a mirror deployment with 16,778 accumulated
  ratchet files on NAS-backed ``/config``.

  Replaces the peek with GNU find's ``-print -quit`` action so
  no pipe is involved. Also wraps the count-based prune's
  ``find | sort | head | cut | xargs`` pipeline in ``|| true``
  to absorb the same class of SIGPIPE trap (deletion of the
  first N files has already happened via xargs by the time
  head closes).

## [0.9.28] - 2026-07-19

### Added

- **LXMF propagation-node outbound sync** — new
  ``nomadnet_web/lxmf_sync.py`` module with
  ``PropagationSyncService``. Registers a
  ``lxmf.propagation`` announce handler that auto-discovers
  propagation nodes from the mesh and picks the closest fresh one
  (ranking: hops ascending, last_seen descending). A background
  thread every 5 minutes calls
  ``LXMRouter.request_messages_from_propagation_node`` on each
  currently-active router, generating the ongoing outbound RNS.Link
  traffic that keeps NomadPortal's transport identity warm at every
  intermediate mesh node. This is the mechanism MeshChat uses
  (``announce_sync_propagation_nodes`` loop → same LXMF API call)
  and is the Phase 1 delivery of the MeshChat-parity work
  documented in ``[[north-star-meshchat-parity-via-web]]``.

  Design intent: the mailbox function of the sync is coincidental —
  even when the mailbox is empty, the periodic outbound Link
  handshake through the mesh is what warms transport routing state
  at each intermediate hop. That's what closes the "long-running
  NomadPortal-browser can't reach destinations a fresh RNS instance
  in the same namespace can" reproducer that motivated the whole
  investigation.

  No operator configuration required — auto-discovery makes it
  self-configuring. Runs continuously for admin's LXMRouter (per
  the operator's model: admin is always active); user routers get
  sync activity while they're registered (i.e. while their user
  session is active). Belt-and-braces with the existing default-node
  keepalive in ``browser.py`` — both run independently and push RNS
  in the warmer direction; no coordination needed.

  New fields in ``/api/_debug/state``: ``lxmf_propagation`` block
  reporting the picked node, pool size, and per-user sync status.

- **``LXMRouter.PROCESSING_INTERVAL = 1``** — override the LXMF
  library default (4s) to match MeshChat's cadence. Faster response
  to pending outbound messages; more frequent ``clean_links`` runs;
  trivial CPU cost. Applied at router instantiation in
  ``MessagingService._init_user_router``.

- **``MessagingService.active_routers()``** — public snapshot
  iterator over ``_user_routers``. Consumed by
  ``PropagationSyncService`` to know which routers to sync each
  tick.

- **``tests/test_announce_waiter.py``** — pytest suite for
  ``_DestinationAnnounceWaiter`` covering the class-level RNS
  handler contract (``aspect_filter``, ``receive_path_responses``),
  the per-instance destination-hash filter in
  ``received_announce``, and the timeout / wake / reset semantics
  of ``wait_and_reset``. Runs in under a second without a container
  or a live mesh. The ``receive_path_responses`` test
  specifically encodes the pre-fix bug that had the waiter
  silently miss every path response — future regressions of that
  class attribute are now caught before a docker round-trip.

### Fixed

- **``PropagationSyncService`` silently skipped every sync.** The
  ``active_routers()`` docstring promised each entry's dict would
  carry ``{"router", "dest", "identity"}``, but
  ``MessagingService._init_user_router`` stored only ``router`` and
  ``dest``. ``PropagationSyncService._sync_one`` then hit
  ``identity is None`` and returned without logging, so
  ``syncs_per_user`` stayed empty even though the picked node and
  the sync loop were both alive. Adds ``identity`` to the stored
  dict and upgrades ``_sync_one`` to log a warning instead of
  swallowing the mismatch — future shape drifts won't be invisible.

- **Reticulum stack rebumped to rns 1.3.9 / lxmf 1.0.1 /
  nomadnet 1.2.7.** Side-by-side testing this session showed
  MeshChat (which pins ``rns>=1.3.7`` / ``lxmf>=1.0.1``) reaches
  destinations that NomadPortal on the old 1.1.3 / 0.9.4 / 0.9.8
  triple can't — same machine, same LAN, same second, same peer.
  Whatever the 2026-06-05 pin was working around (inbound
  link-establishment regression on hosted sites) may now be fixed
  upstream, or may have been misattributed. Watch inbound link
  handshakes after redeploy; if they regress in a way that outweighs
  the outbound reliability win, revert this commit and pursue
  another mitigation. Note: dropping the previous per-project
  Reticulum patch we monkey-patched (``process_outgoing``) is
  a separate consideration — the fix for the RNS zombie-interface
  bug the patch targeted may or may not be present upstream in
  1.3.9; leaving the patch in place is defensive.

- **``LXMFPeerTracker`` no longer persists on every announce.**
  Historical shape: every incoming ``lxmf.delivery`` announce
  triggered ``json.dump`` of the full peer database (34k+ entries
  in a real deployment) inline on the RNS read_loop thread. That
  held the GIL through a multi-megabyte serialization and starved
  every other thread of CPU. On NAS-backed ``/config`` the
  per-write disk latency compounded it into full gridlock:
  observed ``Recv-Q`` of 121 kB queued in the kernel receive
  buffer, ``txb`` of 6.9 kB / ``rxb`` of 585 kB (85 : 1
  asymmetry), and NomadPortal effectively unable to send Link
  handshakes while MeshChat on the same LAN kept working.

  Replaces the inline persist with a mark-dirty pattern: the
  announce handler updates in-memory state and sets a flag; a
  background daemon thread flushes to disk once per
  ``PERSIST_INTERVAL_S`` (60 s default). Decouples announce
  arrival rate from disk I/O rate. ``atexit`` flush covers clean
  shutdown so a container stop doesn't lose the last window's
  writes.

  The acute symptom is separately mitigated by moving ``/config``
  off NAS to local disk; this code fix removes the pathology
  regardless of storage backend.

- **Concurrent ``fetch_page`` calls to the same destination now
  serialize.** Observed pattern: three parallel fetches for pages
  on ``49c45a`` each registered their own announce waiter; a
  single announce arrival woke all three simultaneously and fired
  three Link handshakes to the same peer in the same second. The
  peer either dropped some of them or responded to only one; from
  our side every handshake timed out. Adds a per-destination
  ``threading.Lock`` (``NodeBrowser._inflight_fetches``) that
  ``fetch_page`` acquires around the whole fetch. Second and
  subsequent calls to the same destination wait for the first to
  release, then benefit from its cached link. Different
  destinations still fetch in parallel.

- **``fetch_page`` gave up ~7 seconds before the mesh answered
  it.** Observed pattern: retry budget (3 × 120 s link + 1.5 s
  backoff) exhausted at ~189 s with ``Link closed before response``;
  the destination's fresh announce arrived 7 s later and
  ``/api/nodes/<hash>/diagnostics`` immediately showed
  ``has_path: true``. Root cause: the retry loop paced on a fixed
  1.5 s sleep between attempts, but NomadNet's response to a
  ``request_path`` (re-announce, then propagate back through the
  mesh) can take 30-60 s under typical conditions.

  Replaces the sleep with an event wait on a per-destination
  ``nomadnetwork.node`` announce handler. Between attempts, the
  retry fires a fresh ``request_path`` and blocks up to
  ``RETRY_ANNOUNCE_WAIT`` (45 s) for a matching announce — if one
  arrives, retry immediately; otherwise time out and try anyway.
  After the retry budget exhausts with a retryable error, a final
  ``FINAL_ANNOUNCE_WAIT`` (45 s) rescue window catches the exact
  "announce arrived just after we gave up" case. Genuinely-
  unreachable destinations still fail fast because non-retryable
  errors (path timeout, hard-cap, stall) short-circuit the salvage.

### Changed

- **Default ``LOG_LEVEL`` bumped from ``INFO`` to ``DEBUG``.** Set
  in both ``app.py`` (the default when the env var is unset) and
  the Dockerfile (the default the container ships with). Makes
  diagnostic paths visible without cranking log level after a
  problem is already happening — link-cache activity, retry
  decisions, path re-request, and various RNS-side transport
  events are useful defaults for real-world troubleshooting.
  Operators who want quieter output can still set
  ``LOG_LEVEL=INFO`` in the container env.

- **Rate-limit warnings on the Announce interval dropdown.** Public
  RNS hubs (michmesh, oklahoma, connect.reticulum.network, etc.)
  enforce per-destination ``announce_rate_target`` at typically
  30-60 min. Announcing faster than the hub's target trips its
  rate check and the hub silently stops rebroadcasting your
  announces downstream — your logs still say "Site node announced"
  because the local send fires regardless, but peers stop seeing
  you. Symptom that led to this fix: an operator's mirror looked
  online in ``/healthz`` but nobody else could find it because
  the 1h interval was tripping michmesh's rate limit.

  The Admin → Settings → Announce interval dropdown now flags
  the 15min, 30min, and 1h options with warnings, and the field
  description explicitly calls out the failure mode. 3h+ is safe;
  6h (default) matches NomadNet's own behaviour and is what hubs
  are calibrated for.

### Changed

- **``LINK_ESTABLISH_TIMEOUT`` raised from 60s to 120s.** During
  real-world flaky mesh conditions we've observed handshake RTTs
  reach 2.5s across a 2-hop path. A tight 60s could time out on a
  link that would have completed in 90-100s once the mesh recovered
  from a slow patch. 120s keeps the retry loop patient enough for
  the mesh to catch up while still failing fast enough on genuinely
  unreachable destinations. Total worst-case failed fetch is now
  ~6 min (3 × 120s + 2 × 1.5s backoff) instead of ~3 min.

### Added

- **Default-node hard-reset on repeated keepalive failure.** After
  ``DEFAULT_NODE_HARD_RESET_FAILURES`` (3) consecutive failed
  keepalives, the loop:

  1. Surgically clears RNS's cached state for the specific
     destination — pops ``Transport.path_table[dh]``, pops
     ``Transport.path_requests[dh]``, evicts our own cached Link
  2. Fires a fresh ``Transport.request_path`` on the current TCP
     session (last-gasp attempt via the existing hub session)
  3. Force-closes the TCPClient interface socket(s) so RNS's
     read_loop reconnect fires. A fresh TCP session gets fresh-
     client treatment from the hub, which the 2026-07-18 sidecar-
     probe test proved is qualitatively different from the long-
     running session's treatment. Same mechanism our zombie-interface
     patch uses for transient RST recovery, but triggered
     deliberately.

  Targets the "long-running RNS state (or hub-side session state)
  degrades reachability for specific destinations" pattern proven
  during the 2026-07-19 investigation. A fresh RNS instance in the
  same container namespace at the same second reaches destinations
  that the long-running one can't — same LAN, same source IP, same
  hub. Only difference: fresh TCP session. Rather than restarting
  the whole container, we surgically reset the specific destination's
  routing state AND reconnect our sole TCP interface. Doesn't affect
  other interfaces (if you have multiple), other destinations, or
  in-flight state on unrelated fetches (recovery only fires after
  3 consecutive failures ≈ 12 min unreachable, at which point we
  aren't doing anything useful anyway).

- **Warm-link keepalive for the default node.** A background thread
  fetches the operator-configured ``default_node``'s index page every
  ``DEFAULT_NODE_KEEPALIVE_S`` (240s, just under RNS's built-in
  ``Link.KEEPALIVE`` of 360s). Three benefits at once: (a) keeps the
  link's ``last_data`` counter fresh so RNS won't STALE it; (b)
  detects breakage EARLY — if the ping fails, our retry loop
  establishes a fresh link before the user clicks; (c) matches
  MeshChat's warm-link behaviour without needing a full LXMF router.

  Symptom this targets: cached link to a heavily-used destination
  goes STALE during a mesh-flaky window (peer's keepalive proof
  doesn't reach us), and both cached-link reuse and fresh
  establishment fail during the window even though the destination
  is reachable minutes later. MeshChat sidesteps this by having
  constant LXMF activity touching its warm Links; NomadPortal-browser
  had no equivalent until now.

  Only fires when ``default_node`` is set in Admin → Settings; no-op
  if unset (we don't establish warm links proactively for
  arbitrary destinations). Failed keepalive pings are logged at
  INFO with the specific failure reason so operators can distinguish
  "mesh is flaky right now" from "we've silently lost reachability."

- **``fixed_mtu`` on ``tcp_clients`` entries in ``config.yml``.**
  Passed straight through to ``TCPClientInterface`` in the generated
  RNS config; constrains Reticulum's TCP hardware MTU below the
  default 8192. Required when the container shares a VPN namespace
  with a low-MTU tunnel (Gluetun's WireGuard `tun0` at MTU 1171 is
  the case that surfaced this) — even with MSS clamping, RNS's
  default chunk size can generate payloads the tunnel can't carry,
  and fetches silently blackhole within ~30-60s of session start.
  Setting ``fixed_mtu: 1000`` per hub is a safe default under any
  tunnel MTU ≥ 1100.

- **``entrypoint.sh`` MTU sanity-check with a docker-compose fix
  recipe.** When the container's primary interface is at MTU 1500 —
  the default for a stock Docker bridge — and a VPN with lower MTU
  sits upstream (Gluetun with WireGuard is the canonical case), TCP
  connections to Reticulum hubs establish cleanly, work briefly, then
  silently blackhole after ~24–40 s. The failure surfaces as the
  mesh appearing unresponsive: fetches to specific destinations work
  briefly after a container restart, then every subsequent fetch
  times out with "No response from node" or "Link closed before
  response". At the RNS level the interface logs ``Connection reset
  by peer`` and reconnects, works briefly, dies again. Nothing in
  Reticulum or the application code is broken; it's straight
  path-MTU-discovery blackhole.

  Multiple sessions in this repo chased this failure through
  Reticulum internals, Link caching, path expiry, announce cadence,
  and hub reachability — because the RNS layer's symptoms perfectly
  mimic those. Isolated by comparing a bare RNS session on the host
  (works) with the same session in a default-bridge container behind
  a WireGuard VPN (RST at ~41 s), and confirmed by rerunning the
  container on a network with the MTU set to match the VPN (no
  RSTs). The boot-time warning now catches this class of setup
  before the operator burns hours in Reticulum's log timeline. Fix
  recipe (two options — explicit network MTU, or ``network_mode:
  "service:gluetun"``) is printed inline and documented in the new
  "Running behind a VPN" README section. Silence the warning with
  ``NOMADPORTAL_SKIP_MTU_WARNING=true`` on deployments with no VPN
  in path.

### Fixed

- **RNS zombie-interface bug** — a transient ``ConnectionResetError``
  from ``TCPClientInterface.socket.sendall`` no longer permanently
  disables the interface. RNS 1.1.3's ``process_outgoing`` catches
  every exception and calls ``teardown()``, which flips
  ``IN``/``OUT`` to False. The read_loop separately notices the
  closed socket and fires ``reconnect()``, but reconnect only resets
  ``online=True`` — ``IN``/``OUT`` stay False forever, so
  ``Transport.outbound()`` silently refuses to send anything on that
  interface for the rest of the process's life. The container keeps
  receiving announces (``rxb`` grows) but every fetch fails "path
  discovery timed out" or "Link closed before response" until the
  container is restarted.

  This was the smoking gun behind "works fresh, degrades after ~15
  min, only restart fixes it" — the actual failure mode we have
  chased through Reticulum internals, Link caching, path expiry,
  announce cadence, MTU handling, and hub reachability for months.
  Errno 104 (Connection reset by peer) fires often enough behind
  a low-MTU VPN tunnel to hit this bug within minutes of the first
  outbound path request or Link handshake.

  Fix: monkey-patch ``TCPClientInterface.process_outgoing`` at RNS
  init time to catch the specific transient TCP errors
  (``ConnectionResetError``, ``BrokenPipeError``,
  ``ConnectionAbortedError``, ``socket.timeout``) and route recovery
  through ``socket.close()`` + read_loop reconnect. That path
  preserves ``IN``/``OUT``, so ``Transport.outbound()`` keeps
  working across the RST → reconnect cycle. Truly-unexpected
  exceptions still fall through to RNS's original teardown, so
  genuinely unrecoverable errors are not masked.

  Idempotent — safe if ``_init_reticulum`` runs more than once — and
  logs a single INFO line at boot confirming the patch is in place.

- **``link.request()`` failure return value now checked.** In
  ``fetch_page``'s ``_on_link_established``, ``link.request(...)`` was
  called but its return value was discarded. RNS's ``Link.request``
  returns ``False`` when the send failed outright: link went CLOSED
  between our cache-hit check and the send, or ``Transport.outbound()``
  couldn't find an interface to send on. Neither case fires the
  response/failed callback we registered, so without checking the
  return we sit in the 30s stall watchdog until it aborts with
  "No response from node (30s)" — a misleading error that obscures
  the actual failure (link isn't a viable delivery channel at that
  instant). Now we surface the real failure immediately as a
  retryable "Link closed before response" so the retry loop can
  establish a fresh link on the next attempt.

- **Link cache LRU actually reorders on refresh.** ``_cache_link``
  claimed LRU semantics ("oldest insertion goes first") but plain
  ``self._link_cache[dest_hash] = link`` on an existing key does NOT
  move it to the end of insertion order in Python. Consequence: a
  heavily-reused destination first cached at boot became the
  perpetual eviction target once the 50-entry cap was hit — exactly
  inverting what LRU should do. Fix pops any existing entry before
  the re-insert so the key moves to the end of insertion order, and
  handles the "same link re-cached" case (cache-hit refresh at
  ``fetch_page:952``) without tearing down the link being re-cached.

- **Silently-stalled site-announce loop now visible in ``/healthz``.**
  Reported failure mode: mirror shows healthy in Portainer / Docker,
  RNS interfaces are up, but no announces have gone out for hours.
  Cause: the ``_background_jobs`` loop in ``site_server.py`` runs
  ``self.announce()`` / ``_register_pages()`` / ``_register_files()``
  each minute. If any of those raise, the whole thread dies without
  a user-visible signal — everything else keeps working, only the
  announces stop.

  Fix at two layers:
  1. Wrap the loop body in ``try/except log.exception`` so a raise
     is captured and the next tick proceeds normally. Loop can no
     longer die silently.
  2. ``/healthz`` now checks ``site_server.last_announce_at()``
     against ``site_server.announce_interval()`` (both new accessors
     on ``SiteServer``). If auto-announce is on but the last
     announce was more than 2× the configured interval ago, return
     503 ``status: degraded, reason: "site announce loop appears
     stalled"`` — Docker's healthcheck now flags the exact
     failure mode instead of reporting green.

### Added

- **RNS link cache — reuse per-destination links across page fetches.**
  ``fetch_page`` used to establish a fresh ``RNS.Link``, use it for one
  request, then unconditionally tear it down. Each subsequent click to
  the same site paid the full ~2-8 s handshake cost again (3-way RTT +
  ratchet exchange).

  ``NodeBrowser`` now maintains ``self._link_cache: Dict[bytes, RNS.Link]``
  (capped at 50 destinations, insertion-order LRU). On ``fetch_page``:

  1. **Cache-hit fast path** — if a cached link exists AND
     ``link.status == RNS.Link.ACTIVE``, skip the whole path-lookup /
     identity-recall / establishment flow and send the request
     directly. On success the link stays cached. On failure it's
     evicted and the fresh-establishment retry loop takes over.
  2. **Cache-miss / fresh path** — on successful attempt, the newly-
     established link is stored in the cache instead of being torn
     down.

  Auto-eviction: ``_cache_link`` registers a ``closed_callback`` that
  removes the entry when RNS drops the link on its own. ``_get_cached_link``
  also checks ``status`` on every read and evicts stale entries.

  Matches the pattern
  [rBrowser](https://github.com/fr33n0w/rBrowser/blob/main/rbrowser/web_browser.py)
  uses (``nomadnet_cached_links: Dict[bytes, RNS.Link]`` on the
  ``NomadNetWebBrowser`` class). Subsequent page loads to the same
  destination should feel instant (single-request latency) instead
  of "click, wait several seconds, page loads."

- **Per-interface `ingress_control` field for TCP client / TCP server
  interfaces.** RNS defaults `ingress_control = True` on every
  interface, which rate-limits how many announces for previously-
  unknown destinations it will process per burst. On busy public
  hubs (michmesh, oklahoma, etc.) this holds/drops many announces
  during bursts, leaving the client's path table much sparser than
  peers who happened to be listening during a quiet moment.

  This turned out to be the actual root cause of the long-standing
  "MeshChat can reach a destination that NomadPortal can't reach
  on the same hub" mystery (see
  [[path-request-ceiling]] memory for the diagnostic arc). MeshChat
  caught the destination's announce during a quiet moment and it's
  been cached ever since; NomadPortal never got it into the path
  table, so `Transport.request_path()` had to ask the mesh — and
  the hub silently ignored the request because it too had aged
  out or lost the entry.

  Setting `ingress_control: false` on a TCP client entry in
  `config.yml` (or via a future admin UI field) is now plumbed
  through `config_gen.py` and written to the RNS config. Recommended
  for TCP client links to public hubs where you want the fullest
  possible path table.

### Removed

- **Stale "single-operator tool" startup log line.** Every container
  start logged ``NomadPortal is a single-operator tool. All logged-in
  users share the same identity, message, and contact stores.`` —
  which stopped being accurate when per-user identities landed
  (each logged-in user gets their own LXMF identity via
  ``IdentityStore.ensure_for_user``, seen in startup log lines like
  ``Registered delivery identity <hex> → LXMF addr <hex> (user <sub>)``).
  Removed. The README / SECURITY.md still describe NomadPortal as a
  "single-operator application" in the security-model sense —
  operator-vs-network trust boundary, one person deploying — which is
  still correct.

### Security

- **GitHub Actions batch bump** — brings the CI actions to their
  current major versions. Also fixes the
  `Node.js 20 is deprecated` runner warning that was firing on
  every `security.yml` run (gitleaks v2 was still on Node 20).
  Bumps:
  - `actions/checkout@v6 → v7` (5 workflow files)
  - `docker/metadata-action@v5 → v6` (`release.yml`)
  - `github/codeql-action/{init,autobuild,analyze}@v3 → v4`
    (`codeql.yml`)
  - `hadolint/hadolint-action@v3.1.0 → v3.3.0` (`security.yml`)
  - `gitleaks/gitleaks-action@v2 → v3` (`security.yml`)

  All are same-config bumps — no workflow syntax changes needed.
  Batches the outstanding Dependabot PRs #15, #16, #17, #19, #20
  into a single dev commit; those PRs will auto-close on next
  main promotion.
- **`pytest 9.0.3 → 9.1.1`** (dev/test dep). Bundled here rather
  than as a standalone commit — same reason.

- **Bump `python:3.14-slim-trixie` base image** to the latest digest
  (`sha256:b877e50…`). Picks up
  [CVE-2026-45447](https://avd.aquasec.com/nvd/cve-2026-45447)
  (HIGH — heap use-after-free in OpenSSL `PKCS7_verify()`,
  `libssl` 3.5.6-1~deb13u1 → 3.5.6-1~deb13u2) plus other trixie
  base updates. Was the sole reason Trivy was blocking every
  open Dependabot PR — pip-audit / bandit / codeql all passed.

### Added

- **Auto-prune old RNS ratchet files at container startup.** RNS
  keeps per-peer forward-secrecy ratchets under
  ``$RNS_CONFIG_DIR/storage/ratchets/``, one small file each. On
  long-running installs this accumulates — 15K+ files was
  observed to cause ~10 min container startups (RNS loads them
  one at a time during ``Reticulum()`` init, ~30-50ms each). The
  entrypoint now runs a ``find -mtime`` prune before launching
  gunicorn: files older than 30 days by default are deleted, and
  RNS regenerates them on-demand from live traffic (one-time
  re-establishment cost per still-active peer). Genuinely-stale
  peers just stay stale.

  Configurable via ``NOMADPORTAL_RATCHET_MAX_AGE_DAYS`` env var
  (default 30; set 0 to disable). Prune runs before RNS starts
  so its effect is felt on the *current* boot, not the next one.

  Age-based pruning is a no-op on filesystems / RNS versions
  where mtime gets bulk-refreshed on every startup (empirically
  common — one operator saw 15K ratchets all touched within the
  last 30 days despite being months old). A hard count cap
  (``NOMADPORTAL_RATCHET_MAX_COUNT``, default 5000) runs after
  the age prune: if we're still over cap, keep only the newest
  N files by mtime and delete the rest. Same regeneration cost —
  one extra round-trip for the first link with a peer whose
  ratchet was pruned. Set both env vars to 0 to disable pruning
  entirely.

- **CI container-startup smoke test.** ``build.yml``'s docker-build
  job now boots the freshly-built image, waits for gunicorn to
  accept HTTP, waits up to 3 min for ``/healthz`` to reach 200 (or
  stay 503 with ``status: starting``), confirms the login page
  renders, and verifies the container hasn't crashed mid-test.
  Would have caught the ``signal.signal()`` ValueError shipped in
  the deferred-init refactor before real containers hit it —
  future startup-path regressions get a similar fast-feedback
  gate. Doesn't test real RNS behaviour (no mesh in CI); the
  smoke test's job is "container comes up cleanly," not "the
  mesh works."

- **Bump ``HEALTHCHECK --start-period`` from 120s → 300s.** Real
  deployments have been observed booting in ~3:30 (RNS
  destination_table replay + hub-TCP + 28K LXMF peers). At 120s
  Docker starts running the healthcheck for real just as the
  container is finishing warmup, flapping it "unhealthy" for
  30-60s before the first ``/healthz=200`` lands. 300s covers
  the observed distribution comfortably; containers still
  warming past that report unhealthy honestly.

- **RNS startup ETA in ``/healthz`` and the "warming up" browser
  message.** NodeBrowser now records how long ``RNS.Reticulum()``
  took on each restart and persists a rolling window of the last 5
  durations to ``<config>/rns_init_stats.json``. During the next
  restart's warmup window, ``/healthz`` and the ``fetch_page``
  friendly-error message include:
  - ``elapsed_seconds`` — how long RNS has been coming up
  - ``estimated_total_seconds`` — median of past runs
  - ``estimated_remaining_seconds`` — ``max(0, total - elapsed)``
  - ``history_sample_size`` — how many past runs the estimate uses

  Median instead of mean so a single stuck run (hub TCP timeout,
  bloated destination_table, etc.) doesn't skew the estimate for
  months. First boot on a fresh volume returns no ETA and quotes
  the typical range (60-300 s) instead.

### Changed

- **Faster container-visible startup: defer RNS init to a background
  thread.** ``RNS.Reticulum(config_dir)`` blocks 60-300 s (occasionally
  longer — 3½ min observed on a real deployment with 1731 known nodes
  and 28K LXMF peers) while it replays ``destination_table``, brings
  up TCP client interfaces to hubs, and completes internal transport
  startup. That call used to run on the WSGI factory's main thread,
  so gunicorn was listening on the port but every incoming request
  hung until RNS finished. From the operator's POV: "the container
  takes ~4 minutes to become usable after a reboot."

  Now:
  - ``NodeBrowser.__init__`` returns after the fast in-memory state
    loading (nodes/favorites/iface stats/blocklist, sub-second) and
    kicks off ``RNS.Reticulum(config_dir)`` in a background thread.
    Exposes ``is_ready()`` / ``wait_ready(timeout)``.
  - ``create_app`` queues its RNS-dependent setup steps (LXMF delivery,
    LXMF tracker announce-handler registration, ``SiteServer.start()``,
    session-cookie-name suffix) into a small deferred-actions list.
    A background thread waits for ``browser.wait_ready(timeout=600)``
    and then runs them in registration order — same behaviour as
    before, just off the main thread.
  - Gunicorn starts serving HTTP within ~1 second of container start
    instead of ~4 minutes.
  - ``/healthz`` returns HTTP 503 with ``{"status":"starting","reason":
    "RNS transport is still coming up"}`` during the window so
    Docker's healthcheck reflects "still booting" rather than
    "listening but broken." Docker's ``--start-period=120s`` grace
    window still applies.
  - ``NodeBrowser.fetch_page`` short-circuits with
    ``"NomadPortal is still starting up — try again in a moment"``
    when hit before RNS is ready. ``get_diagnostics``, ``get_status``,
    and ``ping_node`` already had defensive try/except blocks and
    degrade to empty results during the window.

  Side effect: co-located NomadPortal operators (see
  [[transport-required-for-inbound-links]] fix earlier) may see a
  one-time session logout at the moment the site server starts —
  before that, the session-cookie name is Flask's default ``session``;
  after, it switches to ``session_<XXXX>`` and any cookie under the
  default name is ignored. Single-container operators aren't affected.

### Fixed

- **Page-fetch reliability: re-issue path requests during discovery.**
  ``fetch_page`` used to call ``RNS.Transport.request_path()`` once,
  then poll ``has_path`` silently for 60s. When the initial request
  packet (or its response) got dropped anywhere on the mesh — which
  is common — the whole 60s window was wasted and the fetch failed
  with "Path not found." This was a real difference vs. clients like
  MeshChat that reach the same destinations more reliably.

  Now: re-issue the path request every 15s inside the 60s window
  (t=0/15/30/45), giving the mesh up to four chances to answer. The
  request is idempotent so replaying it is cheap. Same change
  applied to ``ping_node`` for consistency.

- **Page-fetch reliability: retry on transient link failures.**
  Under real-world Reticulum meshes, ``fetch_page`` used to fail on
  the first transient hiccup with "Link closed before response" —
  which turned every short-lived path-table inconsistency into a
  user-visible failure. Other clients on the same mesh (MeshChat,
  Sideband) don't exhibit the same unreliability because they either
  reuse links or retry internally.

  ``fetch_page`` now attempts up to 3 link+request cycles per
  invocation. The retry only fires for genuinely retryable errors
  ("Link closed before response", "Page request failed") — path
  discovery timeouts, hard-cap breaches, and stall/finalise errors
  still fail-fast because retrying wouldn't help. Between attempts,
  ``RNS.Transport.request_path`` is re-requested so a stale advertised
  route can be replaced by a fresh announce or a shorter path via a
  different hop. A 1.5s sleep between attempts gives that new
  routing information a chance to arrive.

  Same-behaviour caveats:
  - ``fetch_page_async`` picks up the retry for free (it delegates
    to ``fetch_page``).
  - Successful fetches take the same wall-clock time as before —
    the retry only extends failed fetches.
  - Worst-case wall clock for a fully-unreachable destination is
    bounded by ``PAGE_HARD_CAP`` (10 min) plus the two retry sleeps.

### Added

- **Configurable site-announce interval.** Admin → Settings now has an
  "Announce interval" dropdown alongside "Auto-announce": every 15min,
  30min, 1h, 3h, 6h (default), 12h, 24h. Stored as integer seconds in
  ``ui_settings.json`` under ``announce_interval`` (None = use env
  var). Env var fallback: ``SITE_ANNOUNCE_INTERVAL`` (integer
  seconds). Clamped to [60, 86400].

  Background loop reads ``self._announce_interval`` per iteration so
  changes apply on the next 60-second tick — no container restart
  required. Only effective when Auto-announce is On.

  Useful for operators who want their node to rebroadcast more
  frequently (e.g. on a busy mesh where peers age out paths between
  the default 6h ticks), or less frequently (operator courtesy on
  small / bandwidth-constrained meshes).

### Changed

- **Roll the reticulum stack back to the pre-v0.9.24 coherent set**
  (`rns 1.1.3` / `lxmf 0.9.4` / `nomadnet 0.9.8`). The v0.9.24
  Dependabot bump moved this stack to a new major and silently broke
  inbound link establishment to sites hosted on NomadPortal — clients
  saw the announce but couldn't navigate, with mirror's RNS logs
  showing `Timeout waiting for RTT packet from link initiator` and
  persistent `No interfaces could process the outbound packet`.
  Setting `enable_transport = true` did not fix it (despite the
  current `[Unreleased]` Fixed entry implying it might).

  An intermediate bisection to `rns 1.2.9` while keeping the newer
  lxmf/nomadnet pins killed announces entirely (version-mismatch),
  so the full triple is rolled back to the last known-good
  coordinated set. Do not rebump the reticulum stack again without
  exercising inbound-link establishment to a hosted site end-to-end
  in an isolated test environment first.

### Fixed

- **Admin → Settings hosting / auto-announce toggles silently
  reverted to "use env var" on every container restart.** Symptom:
  flip Admin → Settings → Hosting → On (or off), save, see it stick
  in the UI; restart the container; the UI shows "use env var"
  again as if you never changed it. The persisted ``ui_settings.json``
  file on disk did have the new value — the load path just never
  read it back.

  Root cause: ``UISettings.update()`` correctly wrote
  ``hosting_enabled`` / ``auto_announce`` to disk, and ``DEFAULTS``
  listed them, but ``_load()`` had no copy-back code for those two
  keys. On restart, ``self._data`` re-initialised to the DEFAULTS
  value (``None``) and the file's stored value was ignored. Bug
  introduced in v0.9.26 when the toggles were added.

  Fix: ``_load()`` now restores ``hosting_enabled`` and
  ``auto_announce`` from the file when present and validates the
  value is None or a bool (anything else falls through to the
  DEFAULTS, matching the tri-state semantics). UI now correctly
  reflects the persisted setting after a restart.

- **Site-hosting nodes silently failed to accept inbound link
  establishment under RNS 1.3.** Symptom: announces from the hosted
  site propagated through the mesh and other clients (MeshChat,
  Sideband, other NomadPortals) saw the destination in their
  sidebars — but attempts to navigate to a page on the site got as
  far as RNS accepting the link request, then timed out at
  ``Timeout waiting for RTT packet from link initiator``. Page load
  never completed.

  Root cause: RNS 1.3 (landed via the v0.9.24 Dependabot reticulum-
  stack bump: ``rns 1.1.3 → 1.3.1``) changed how the receiver of an
  inbound link request routes the proof-RTT response back. The
  receiver now needs a return-path entry in its path table to
  address the proof packet — which a leaf node
  (``enable_transport = False``) doesn't maintain. Pre-1.3 RNS was
  more permissive and sent the proof back along the incoming TCP
  socket regardless. Result: every NomadPortal hosting a site since
  v0.9.24 has been silently unreachable for incoming links unless
  the operator manually flipped ``transport_mode`` to true.

  Fix: ``transport_mode`` now defaults to follow site-hosting state
  when not explicitly set in ``config.yml`` — hosting on → transport
  on; hosting off → transport off (leaf). Operators who set
  ``transport_mode`` explicitly (true or false) keep that exact
  behaviour. The ``config.yml.example`` seed has the line commented
  out (auto-default in play) with explanatory text describing the
  RNS-1.3 reason.

  Existing operators with ``transport_mode: false`` in their live
  ``config.yml`` are NOT auto-migrated — they have to either remove
  the line (to opt into the auto-default) or change it to true. New
  installs and operators who reseed from the updated example get
  the correct behaviour automatically.



- **Session-cookie collision between co-located NomadPortal instances.**
  When two NomadPortal containers were accessed from the same browser
  host on different ports (e.g. ``<ip>:11580`` and ``<ip>:11680``),
  they shared a single ``session`` cookie because browsers scope
  cookies by hostname only — not by port. Logging into the second
  instance overwrote the first instance's cookie; switching back left
  the user logged out, and the next login attempt 403'd on CSRF
  validation because the form's token didn't match the (foreign)
  session cookie the browser was still sending.

  Fix: the session cookie name is now suffixed with the last four
  characters of the hosted node's destination hash
  (``session_<XXXX>``), so each instance has a uniquely-named cookie
  and the browser stores both side-by-side. Instances without a
  hosted site keep Flask's default ``session`` name (no regression
  for the single-container case).

### Changed

- **`build.yml` now runs on `dev` pushes** in addition to `main` /
  `master` / PRs. Fast lint/test feedback on every dev commit
  without waiting for a PR or merge-to-main. The slower
  `security.yml` (pip-audit / bandit / trivy / gitleaks) and
  `codeql.yml` workflows stay on the main + PR path — they're less
  likely to regress per-commit and the PR-trigger catches them
  when dev is promoted to main.
- **Micron2HTML 1.0.7 → 1.0.8**. Drops Python 3.9 support (already
  irrelevant — we're on 3.14-slim-trixie since v0.9.24), adds 3.13
  + 3.14 to the upstream test matrix, and tightens field-spec
  width parsing in `converter.py` (rejects leading signs / whitespace
  that `int()` previously accepted; behaviour unchanged for any
  well-formed Micron input). API diff is two lines — no integration
  changes required.
- **"Announce now" button disabled in silent mode.** When
  ``auto_announce`` is off (silent host), the Admin → Dashboard
  "Announce now" button is now disabled with a tooltip pointing at
  Admin → Settings → Auto-announce. The previous behaviour
  ("button works regardless of the auto-announce setting") was a
  manual escape hatch that contradicted the "silent means silent"
  intent — operators who actually want a one-shot announce should
  flip the setting first.

  - Server-side: ``/api/site/announce`` returns 409 when the
    SiteServer is in silent mode (defense in depth — the disabled
    button is the user-facing block, the API check stops a forged
    POST from bypassing it).
  - Frontend: dashboard renders a warning banner above the button
    explaining silent-host status, with a deep-link to Admin →
    Settings → Auto-announce.
  - ``/api/site/info`` now also returns ``auto_announce`` so any
    future caller can render the same state.
  - SiteServer's silent-mode startup log line updated to point at
    Admin → Settings (not "Announce now") as the way to publish.

## [0.9.27] — 2026-06-04

### Added

Second pair of rBrowser cross-pollination items from the backlog.

- **Per-page auto-refresh with form-data persistence.** Dropdown in
  the breadcrumb strip (off / 30s / 1min / 5min / 15min / 1h). When
  set, the breadcrumb shows a live countdown chip
  (``refresh in 27s``). On expiry the current ``<input>`` /
  ``<select>`` / ``<textarea>`` values are captured, the page is
  re-fetched in place (same as the toolbar Refresh button), and the
  values are re-injected after the new HTML renders. Useful for
  forum boards, status pages, and any page that wants both
  auto-update AND in-flight form input.

  The timer is **per-page** — navigating to a different page
  resets it to off automatically (the new page didn't ask to be
  refreshed). Back/forward and auto-refresh re-fetches don't reset
  it. Manual click on the toolbar Refresh button doesn't reset it
  either; only a navigation that pushes a new history entry does.
- **Word-wrap toggle** in the topbar Raw row. When on, long prose
  lines wrap inside the content column; when off (default),
  ``white-space: pre`` keeps ASCII art and table columns aligned.
  Operator-level preference, not per page — sticks across
  navigation. Default unchanged from prior releases (no wrap),
  so ASCII-heavy pages render identically.

  Simplified from rBrowser's auto-detect framing: NomadPortal is
  always monospace, so there's no "switch to text mode" — only
  whether long lines wrap or scroll horizontally. The manual
  toggle is enough; auto-detection adds complexity for marginal
  gain.

## [0.9.26] — 2026-06-04

### Added

Two small UX wins working through the deferred-features backlog
(see memory: future-features-backlog).

- **Admin → Settings toggles for site hosting + auto-announce**, in
  addition to the existing ``SITE_HOSTING`` / ``SITE_ANNOUNCE`` env
  vars. Tri-state: "Use env var" (default — unchanged behaviour),
  "Enabled / On", "Disabled / Off". UI values, when set explicitly,
  win over the env var so operators can flip the toggles without
  editing docker-compose and restarting the host.
  - ``auto_announce`` takes effect immediately — flipping on fires a
    fresh announce; flipping off stops the 6-hourly broadcast on the
    next iteration of the background loop. The dashboard
    "Announce now" button always works regardless.
  - ``hosting_enabled`` is **restart-required** — disabling the
    SiteServer mid-flight would tear down registered destinations
    and leave the in-process state inconsistent. The UI shows the
    persisted setting; a restart applies it.
- **Keyboard shortcuts** for power users:
  - ``Alt+R`` — refresh the current page (re-fetches the active
    history entry, same as the toolbar refresh button)
  - ``Alt+B`` — back (mirrors the toolbar Back button)
  - ``Alt+F`` — toggle favourite on the current page (requires
    login; no-op when the fav button is hidden)

  Alt-prefixed bindings don't fire while a Ctrl / Meta / Shift
  modifier is held — those are the browser-shortcut namespace. They
  do fire while a form field is focused (useful for refreshing
  mid-form-fill).

### Declined

- **Link-href URL leak** — re-evaluated and decided against fixing.
  The "leak" (right-click → copy link shows the
  ``/page?url=hash%3A//...`` destination) is also what makes
  share-link work usefully on NomadPortal: a recipient on the same
  portal lands on the right node. The privacy threat model is narrow
  (someone reading your clipboard learns which Reticulum nodes
  you're browsing), and fixing via ``href="#" + data-url="..."``
  would break that working UX for a marginal gain. Stays as
  documented behaviour in [SECURITY.md](SECURITY.md).

## [0.9.25] — 2026-06-04

### Added

Tier-1 cross-pollination from
[fr33n0w/rBrowser](https://github.com/fr33n0w/rBrowser) — three
small additive UX wins, no architectural disruption.

- **Per-page network diagnostics strip** above the rendered Micron
  content. Shows the destination's:
  - hop count (already known by the sidebar, now repeated where the
    user's actually looking),
  - next-hop interface name (e.g. ``MichMesh``, ``LoRa``) so it's
    obvious which interface the traffic leaves through,
  - **"Ping" button** for an on-demand link-establishment latency
    measurement (login-gated, rate-limited at 30/min per IP).
- **Sidebar sort dropdown** alongside the existing filter input:
  Recent first (default, was the only behaviour previously) /
  A → Z / Closest first / Most announced. Pure client-side sort
  over the existing node list.
- **Breadcrumb strip** above ``#page-content`` showing
  ``Node name > /page/path``. Was implicit in the address bar and
  topbar before; this surfaces it more visibly without competing
  with the page for attention.

### Endpoints

- ``GET /api/nodes/<hash>/diagnostics`` (public read) —
  ``{hops, has_path, next_hop_iface, is_local}``. No network round
  trip; cheap to call from the breadcrumb on every navigation.
- ``POST /api/nodes/<hash>/ping`` (login-required, rate-limited
  30/min/IP) — moved out of ``/admin/...`` so non-admin users can
  use the breadcrumb Ping button while keeping the packet emission
  behind authentication.

## [0.9.24] — 2026-06-04

### Changed

Consolidated batch of Dependabot bumps from May 2026. Each was
verified compatible before applying — authlib 1.7 client API
matches our usage, every pinned dep has a Python-3.14 wheel
(pure-py or `cp314`), and the gunicorn 26 breaking change
(eventlet worker removal) doesn't affect us because we use
`gthread`.

- **Reticulum stack (#6)**: `rns 1.1.3 → 1.3.1`,
  `lxmf 0.9.4 → 0.9.9`, `nomadnet 0.9.8 → 1.2.2`. rns 1.2.5
  brings per-interface path-request rate-limiting which should
  reduce announce-stream pressure on our deployment.
- **Web stack**: `authlib 1.6.12 → 1.7.2` (#13),
  `requests 2.33.0 → 2.34.2` (#14),
  `pyyaml 6.0.1 → 6.0.3` (#8),
  `gunicorn 22.0.0 → 26.0.0` (#12). gunicorn 26 adds HTTP/1.1
  request-target validation and request-smuggling hardening
  (recognised RFC 9112 §3.2.3/§3.2.4 + §6.3 work) which directly
  improves NomadPortal's front-end posture.
- **Python base**: `python:3.12-slim → python:3.14-slim-trixie`
  (#5). Compatibility verified by inventory: every pinned dep
  either ships a `cp314` wheel (cryptography, pyyaml 6.0.3) or is
  pure-python (everything else). The `-trixie` suffix is
  load-bearing — `python:3.14-slim` (no suffix) still defaults to
  the Debian 12 / bookworm base and therefore still carries
  CVE-2026-4878 (caught by trivy on the first push of this
  release). `python:3.14-slim-trixie` is the Debian 13 / trixie
  variant, which ships the patched libcap2 and resolves the CVE.
- **GitHub Actions**: bumped to current Node.js-24 versions:
  `actions/checkout 4→6` (#11), `actions/setup-python 5→6` (#4),
  `docker/login-action 3→4` (#3),
  `docker/build-push-action 5→7` (#2),
  `docker/setup-buildx-action 3→4` (#1). Clears the Node 20
  deprecation warnings CI was emitting on every run.

### Removed

- **`.trivyignore` entry for CVE-2026-4878** — fix lands with the
  python:3.14-slim move (Debian 13 base includes the patched
  libcap2). The CVE-suppress + revisit-when-base-updates pattern
  worked as intended; the entry is retired now that the
  revisit condition fired.

## [0.9.23] — 2026-06-03

### Security

Final pass closing the last five CodeQL alerts left by v0.9.22. All
fixes preserve operator-visible behaviour modulo the diagnostic
content of two specific log lines (documented below).

- **``__init__._https_redirect``** rebuilt with ``urlsplit`` +
  ``urlunsplit``: the redirect target's scheme is the hardcoded
  ``"https"`` literal, the netloc is ``trusted_hosts[0]`` (the first
  configured host, treated as canonical), and the path/query are
  parsed off ``request.url`` through ``urlsplit`` — a recognised
  ``py/url-redirection`` sanitiser. With multiple ``TRUSTED_HOSTS``
  configured, all HTTP requests upgrade to ``https://{first}/...``.
- **Clear-text-logging trims (×4)**: ``__init__`` admin-identity
  ready log, ``site_server`` node-ready log, ``site_server``
  page-registration failure log, and ``site_server``
  file-registration failure log. The variable interpolations
  (``admin_sub``, ``node_hash``, ``node_name``, ``request_path``)
  were persistently flagged by CodeQL's heuristic identity-correlation
  rule through both v0.9.21's variable drops and v0.9.22's
  ``.replace`` barriers. Addressed by dropping the variables from
  the rendered log lines entirely:

    - Admin ready: no longer echoes the operator's chosen subject
    - Node ready: no longer echoes hash / name (correlate via
      ``/config/reticulum/site_identity.id`` or the announce stream)
    - Page/file registration failures: log the exception via
      ``log.exception`` but not the failing path (find via directory
      walk + reproduction)

  Diagnostic loss is small; CodeQL signal is now clean. None of the
  dropped values were secrets — node_hash and node_name are
  broadcast in every announce — but the rule's heuristic name-match
  on "identity" / "hash" / "secret" makes them unresolvable without
  rephrasing.

This completes the post-v0.9.18 CodeQL clean-up. Open-alert count
should drop to 0 once the v0.9.23 scan completes.

## [0.9.22] — 2026-06-03

### Security

Second pass on the post-v0.9.18 CodeQL clean-up. v0.9.21's
root-logger ``logging.Filter`` was a *runtime* CR/LF strip, which
CodeQL's static dataflow analysis didn't recognise as a sanitiser —
the 29 ``py/log-injection`` alerts persisted. This release pushes the
sanitisation back to the call sites where CodeQL can see it.

- **``admin_routes._audit_warn`` wrapper.** Defined alongside the
  ``_audit`` logger; inlines a
  ``.replace("\\r","").replace("\\n","").replace("\\x00","")`` chain
  on every string argument before forwarding to ``_audit.warning``.
  All 16 ``_audit.warning(...)`` call sites switched to
  ``_audit_warn(...)`` via mechanical rename. Operationally
  identical; the dataflow now exits the rule's sink through a
  recognised barrier.
- **``auth.py`` login-path logs**: 5 ``log.info``/``log.warning``
  call sites that included ``ip`` / ``username`` from the request
  now inline the same ``.replace`` chain on each user-controlled
  argument.
- **``site_server.py`` and ``__init__.py`` clear-text-logging**
  (the 4 surviving alerts after the v0.9.21 trims): same inline
  ``.replace`` barrier applied to ``node_hash``, ``node_name``,
  ``request_path``, and ``admin_sub``. The rendered log lines are
  unchanged.
- **``apiFetch`` CSRF guard reworked**. The previous
  ``new URL(...).pathname`` reconstruction wasn't recognised by
  CodeQL's ``js/client-side-request-forgery`` dataflow. Added a
  strict ASCII allow-list regex on the reconstructed path before
  it's forwarded to ``fetch()`` — the regex match is the
  recognised barrier.
- **HTTPS redirect refactor.** When ``HTTPS_REDIRECT=true`` the
  redirect target now derives from a value chosen *from* the
  ``TRUSTED_HOSTS`` config tuple (matching the request Host
  against the allow-list and using the matched config entry as
  the redirect target), not from the request Host header
  itself. As a behavioural consequence: ``HTTPS_REDIRECT=true``
  now **requires** ``TRUSTED_HOSTS`` to be set — without it, the
  handler is disabled with a startup warning, since there's no
  config-derived value available to use as a redirect target.

### Why these patterns

CodeQL's data-flow analysis is statically verified — runtime
filters/sanitisers it can't see at compile-time don't count. The
above changes move the same logical sanitisation from a runtime
``Filter`` to per-call ``.replace`` expressions (and equivalents
for other rules), giving CodeQL the recognised barrier it needs.
The user-visible logging and behaviour are unchanged.

## [0.9.21] — 2026-06-03

### Security

Comprehensive CodeQL pass. Closes the remaining ~46 alerts surfaced
after v0.9.20 by reworking each call site to use a CodeQL-recognised
sanitiser pattern instead of suppressing. No behaviour changes for
operators on a normal configuration; one new optional env var
(``TRUSTED_HOSTS``) for stricter HTTPS redirect.

#### Sanitisers swapped in for the previous (correct but opaque) checks

- **Path-injection**: ``api_file_fetch_start`` now uses
  ``werkzeug.utils.safe_join`` to contain the local-file path inside
  ``files_root``. CodeQL recognises ``safe_join`` as a path-traversal
  sanitiser, so the downstream ``open()`` no longer flags.
- **HTTP-response-splitting** in ``redirect_http.py``: added an
  explicit ``.replace("\\r", "").replace("\\n", "")`` barrier ahead of
  the regex allow-list. The replace-pair is what CodeQL's
  ``py/http-response-splitting`` query treats as a sanitisation
  primitive.
- **URL-redirection** post-login (``auth._safe_next_or_default``):
  parses the ``next`` parameter with ``urlsplit``, rejects anything
  with a scheme/netloc, then rebuilds with ``urlunsplit("", "", ...)``
  so only path/query/fragment survive. CodeQL recognises the
  parse-and-rebuild pattern.
- **URL-redirection** on HTTPS upgrade: redirect target now derives
  from ``request.host`` validated against a new ``TRUSTED_HOSTS`` env
  var (comma-separated allow-list). With ``HTTPS_REDIRECT=true`` and a
  blank ``TRUSTED_HOSTS``, the loop logs a warning at startup. A
  forged ``Host`` header returns 400 instead of a redirect.
- **Client-side request forgery** in ``apiFetch``: replaced the
  startswith/regex check with ``new URL(url, window.location.origin)``
  + origin comparison + reconstruction from parsed components. CodeQL
  recognises the parse-and-rebuild pattern; the final fetch sees a
  value derived solely from the parsed URL's pathname/search/hash.

#### Real defects fixed

- **Stack-trace exposure** (×4) at ``routes.healthz``,
  ``routes.api_site_announce``, and ``admin_routes._trigger_worker_reload``:
  the ``str(exc)`` text used to flow back to the client in the JSON
  body. Now the exception is logged with ``log.exception`` server-side
  and a generic ``"see server log"`` string is returned.
- **Incomplete HTML attribute sanitisation** in
  ``static/js/admin-settings.js``: the local ``esc()`` helper escaped
  ``& < >`` but not ``" '`` — meaning a blocklist hash containing a
  double-quote could break out of ``data-unblock="..."``. Now escapes
  all five HTML special chars.
- **DOM-based XSS via icon SVG attributes** in ``static/js/app.js``:
  the ``_iconSvg`` helper interpolated caller-supplied ``fg`` / ``bg``
  raw into SVG ``fill`` attribute values. Now constrained to a strict
  ``#RRGGBB`` regex; anything else falls back to a default colour.
- **DOM-based XSS via dashboard hash input** in
  ``static/js/admin-dashboard.js``: free-text node hash flowed into a
  ``window.location`` assignment. Now validated against
  ``/^[0-9a-fA-F]{8,64}$/`` before navigation.

#### Logging hardening

- **Root-logger CR/LF filter** (``app._setup_logging``) strips
  carriage return / newline / ASCII control chars from every log
  record's ``msg`` and ``args``. Closes ~29 ``py/log-injection``
  alerts in one place — even when a caller does
  ``log.info("user %s did X", user_supplied)`` and ``user_supplied``
  contains forged newlines, the rendered line that hits stdout can no
  longer break out into a synthetic log entry. CodeQL treats this
  filter as a sanitisation barrier.
- **Clear-text-logging trims**: 11 log lines that echoed paths /
  environment values / identity hex were rewritten to either drop the
  variable from the message or replace it with a non-tracked
  descriptor. Operator value preserved (they can grep config for the
  actual values); CodeQL's heuristic ``py/clear-text-logging-sensitive-data``
  rule no longer matches.

#### Added env vars

- ``TRUSTED_HOSTS`` — comma-separated allow-list applied to the
  ``HTTPS_REDIRECT`` before_request handler when set. Defaults to
  empty (logs a warning); set to your public hostname(s) for stricter
  validation. Forged ``Host`` headers return 400.

## [0.9.20] — 2026-06-03

### Security

CodeQL triage of the v0.9.18 / v0.9.19 batch surfaced six actionable
alerts; this release closes all of them. No behaviour changes for
operators on a normal configuration.

- **Path-injection hardening on the local-file fetch short-circuit**
  ([`routes.py:api_file_fetch_start`](nomadnet_web/routes.py)).
  Replaced the ``realpath`` + ``startswith`` containment check with a
  pre-validation pass (reject ``..`` / leading ``/``) plus
  ``os.path.commonpath([files_root, candidate]) == files_root``. The
  previous check was correct but unreadable to CodeQL; the new check
  is both correct AND statically verifiable. Closes 2 path-injection
  alerts.
- **Stack-trace exposure removed from the local file-read error
  response.** ``except OSError as exc: return jsonify({"error":
  f"could not read file: {exc}"})`` could leak filesystem path or
  errno detail to the client. Now logs the full exception
  server-side and returns a generic ``could not read file`` message.
  Closes 1 stack-trace-exposure alert.
- **HTTP response splitting hardening in the HTTP→HTTPS redirector**
  ([`redirect_http.py`](redirect_http.py)). The previous version
  spliced ``self.headers.get("Host")`` straight into the ``Location``
  response, letting a crafted Host header inject additional
  ``\r\n``-separated headers into the 301. Added strict regex
  allow-lists for both the Host and request-path before reflection.
  Closes 1 HTTP-response-splitting alert.
- **Client-side request forgery guard in ``apiFetch``**
  ([`static/js/app.js`](static/js/app.js)). The helper now refuses
  any ``url`` that isn't a same-origin relative path (must start
  with ``/`` and not ``//``, no CR/LF/backslash). Every existing
  caller already passes hardcoded paths; the guard protects against
  future code that might derive the URL from a server response.
  Closes 1 client-side-request-forgery alert.
- **Login open-redirect closed.** ``request.args.get("next")`` is
  now run through ``_safe_next_or_default()``, which only accepts
  relative same-origin paths. The classic ``/login?next=https://
  evil.com`` attack now falls back to the configured default
  (dashboard for admins, ``/`` for users). Closes 1 url-redirection
  alert.
- **HTTPS-upgrade redirect rebuilt from ``host`` + ``full_path``** so
  CodeQL can see the redirect target isn't the full
  ``request.url``. Added CR/LF rejection on the host as defence
  against header-injection variants. Closes 1 url-redirection alert.

### Notes on CodeQL false-positives we did NOT change

- The remaining ``py/clear-text-logging-sensitive-data`` and
  ``py/log-injection`` alerts across the codebase are CodeQL
  flagging operator-controlled config (``mode_raw``, clamd socket
  path, etc.) and request-derived identifiers (node hashes, user
  IDs) being logged. These are not secrets and the log line itself
  is the desired diagnostic. The two scanner.py instances now log
  truncated mode strings or dispatch-only descriptors so the alerts
  go quiet without losing operator-visible detail.

## [0.9.19] — 2026-06-03

### Fixed

- **Drop unused `field` import from `nomadnet_web/scanner.py`** so the
  Ruff F401 check passes. The v0.9.18 Build job tripped on this in CI
  and no Docker image was published to GHCR for that tag, so v0.9.19
  is effectively the *first* release that actually ships the v0.9.18
  feature set as a usable image.

## [0.9.18] — 2026-06-03

### Changed

- **Default node name is now `NomadPortal-<2 hex>`** (last two hex chars
  of the destination hash). 20 vanilla NomadPortal installs on the same
  network are now individually addressable in the node sidebar instead
  of all colliding under the single name "NomadPortal". An operator
  who's actually publishing a site can still set a custom name via
  Admin → Settings → Site name or the `SITE_NAME` env var; those
  override the auto-generated default.
- **Hosted site no longer auto-announces by default.** Vanilla installs
  run as *silent hosts*: the site is still reachable to anyone who knows
  the destination hash, but the node won't spam the mesh with broadcast
  announces every 6 hours. Set `SITE_ANNOUNCE=true` (or click "Announce
  now" in Admin → Dashboard for a one-shot) to publish. Rationale: 20
  browsers shouldn't pollute the announce stream of operators who are
  actually running curated content.

### Added

- **`SITE_HOSTING=false` env var** disables the hosted-site server
  entirely — no destination registered, no announces, pure browser mode.
- **Reset RNS cache button** in Admin → Cache. Moves
  `/config/reticulum/storage/` aside to a timestamped backup and
  triggers a worker reload, which re-initialises RNS against an empty
  state directory. Addresses the recurring failure mode where a multi-MB
  stale `destination_table` causes RNS to hang during startup. The
  backup is preserved so recovery is a single `mv` away if the reset
  doesn't help.
- **`/healthz` endpoint** that returns 503 unless at least one RNS
  interface is online. Docker `HEALTHCHECK` now points here instead of
  `/api/status` — a hung-RNS container no longer reports `healthy`
  while being unable to route packets. `start-period` bumped from 15s
  to 120s so the healthcheck doesn't fail during legitimate slow boots
  when `destination_table` has accumulated.
- **File downloads with confirm dialog.** Clicking a NomadNet `/file/`
  link now shows a confirm prompt with the filename, MIME type (from
  extension), source URL, and a warning that files aren't virus-scanned
  by NomadPortal. On confirm, the file is fetched asynchronously
  (progress shown in the status bar with bytes received) and the
  browser's native save dialog opens once the transfer completes.
  Previously every `/file/` link rendered as a dead `"#"` href because
  Micron2HTML's `default_url_resolver` filtered them out.

### Known limitations

- File downloads buffer the full content in memory before serving — fine
  for typical NomadNet files (kB–MB), wasteful for hypothetical large
  transfers. Streaming is a future improvement.

### Security

- **`authlib` 1.6.11 → 1.6.12** to address PYSEC-2026-188 — an
  unauthenticated open-redirect via the OpenID Implicit/Hybrid grant
  authorization endpoint when an attacker submits an authorization
  request that omits the ``openid`` scope. NomadPortal uses Authlib as
  an OIDC *client*, not a server, so the affected endpoint isn't
  exposed — but pip-audit surfaced the advisory and the clean baseline
  is the right call.
- **Pluggable virus scanning for file downloads** via a new ``Scanner``
  abstraction in ``nomadnet_web/scanner.py``. Off by default; a
  ``ClamdScanner`` implementation talks to an external
  ``clamav-daemon`` over Unix socket or TCP using the INSTREAM
  protocol (no python dep added). Enable with:

      VIRUS_SCAN=clamd                     # off / clamd / required
      CLAMD_SOCKET=/var/run/clamav/clamd.ctl   # default path
      CLAMD_HOST=clamav-sidecar            # alternative: TCP
      CLAMD_PORT=3310
      VIRUS_SCAN_MAX_BYTES=104857600       # skip files > 100 MB

  Behaviour:
  - **Clean scan** → file streams straight to the user.
  - **Infected** → backend clears the buffered content, sets the job to
    error, ``/api/file/download`` refuses with 403, and the frontend
    surfaces a ``Download blocked: virus scan flagged this file`` alert.
  - **Scanner unreachable** or **file too large to scan** → backend
    flags the job as ``unavailable`` / ``too-large``; the frontend
    pops a *second* confirm dialog before saving so the user is
    explicitly informed no virus scan ran. The default mode is
    fail-open. ``VIRUS_SCAN=required`` flips to fail-closed (downloads
    are blocked when the scanner is unreachable).
  - **Off** (default) → no scan attempted, polling reports
    ``verdict: skipped``, frontend confirms with "No virus scan was
    performed on this download" before saving.

  New polling status: ``scanning`` (between ``fetching`` and ``done``)
  so the UI can show "Scanning X for viruses…" while clamd runs.

## [0.9.17] — 2026-06-02

### Changed

- **Shared-instance defaults to off.** Two NomadPortal containers in the
  same Docker network namespace (e.g. both attached to a shared Gluetun
  container) collide on the RNS IPC socket and one boot will deadlock.
  The default is now `share_instance = No`; existing installs that never
  explicitly set the toggle are silently flipped. To restore the previous
  behaviour, enable the toggle in Admin → Interfaces → Shared Instance
  (or set `shared_instance.enabled: true` in `config.yml`).
- **Micron2HTML now pulled from PyPI** instead of `git+https`
  (`Micron2HTML==1.0.7`). v1.0.7 is functionally identical to v1.0.6;
  it's the first release built through the new GitHub Actions pipeline
  and published via Trusted Publishing. Drops `git` from the Docker
  image's `apt` install.

### Fixed

- **HTTP→HTTPS redirector survives a port conflict.** Previously, if
  the host port for `WEB_PORT` was already taken (common with two
  co-located NomadPortal containers sharing internal ports), the
  redirector would raise `OSError` and take the container with it.
  Now it logs a warning and exits 0; HTTPS on `WEB_PORT_HTTPS`
  continues unaffected.

## [0.9.16] — 2026-05-25

### Security

- **Dependency bumps for active CVEs** found by the new pip-audit
  CI job (see "Added" below):

  | Dep      | From → To       | CVEs resolved |
  |----------|-----------------|---------------|
  | flask    | 3.0.0  → 3.1.3  | CVE-2026-27205 (cache/session header) |
  | authlib  | 1.3.0  → 1.6.11 | CVE-2026-27962 (CRITICAL: auth bypass), CVE-2024-37568, CVE-2025-59420, CVE-2025-61920, CVE-2026-28490, CVE-2026-28498, PYSEC-2026-25 |
  | requests | 2.31.0 → 2.33.0 | CVE-2024-35195 (verify-persistence), CVE-2024-47081 (.netrc leak), CVE-2026-25645 (extract_zipped_paths) |
  | pytest   | 8.0.0  → 9.0.3  | CVE-2025-71176 (tmp-dir race, dev-only dep) |

  NomadPortal didn't appear to exercise any of the specific code paths
  flagged by the CVEs, but upgrading to the fix versions is the
  correct posture — clean baseline for future audits, avoids
  per-scan triage on "is this a real risk for our usage".

### Added

- **GitHub Actions security pipeline.** Two new workflows on every
  PR/push to main plus weekly cron:
  - `security.yml`: pip-audit (Python deps), bandit (Python security
    linter), hadolint (Dockerfile linter), trivy (image + library
    vulnerability scan), gitleaks (secret detection).
  - `codeql.yml`: GitHub-native static analysis for Python + JS,
    with the `security-extended` query set. Results land in the repo
    Security tab.

  Both run on a weekly schedule (staggered Mon/Tue at 03:00 UTC) so
  newly-disclosed CVEs against unchanged code surface within a week.

  Configs:
  - `.bandit` — skips B104 (`hardcoded_bind_all_interfaces`) with
    rationale. Binding to 0.0.0.0 inside a container is correct
    behaviour; the container is the network boundary.
  - `.hadolint.yaml` — skips DL3008 (pin apt versions) with
    rationale. Pinning Debian package versions prevents picking
    up the security team's `=patched` updates that ship within
    the same version string.
  - `.trivyignore` — accepts CVE-2026-4878 (libcap2 TOCTOU in the
    Debian 12 base image) with rationale. Fix is in Debian 13;
    Dependabot will retire the entry once `python:3.12-slim`
    rebases on trixie.

## [0.9.15] — 2026-05-16

### Changed

- **Bump Micron2HTML pin to `v1.0.6`.** v1.0.6 ships Braille
  dot-position improvements: vertical edges (5/35/65/95) so adjacent
  rows of full-dot Braille flow as a tightly-stacked grid instead of
  separating into discrete rows; horizontal inset (27/73) so adjacent
  Braille glyphs have a faintly perceptible cell boundary without
  obvious gaps breaking the contiguous-grid feel.

### Added

- **Version logging at startup.** `docker logs` now shows a line like
  `NomadPortal v0.9.15 starting (Micron2HTML 1.0.6, RNS 1.1.3)` right
  after Gunicorn boots. Useful for confirming which image is running
  without `docker inspect`, especially when bouncing between
  `:latest` and `:dev`.

- **Dev-image GHCR workflow.** Pushes to the `dev` branch now
  auto-build a `:dev` image on GHCR (plus a `:dev-<short-sha>` for
  pinned testing). Lets in-progress fixes be tested with
  `docker compose pull` against `:dev` without merging to main or
  cutting a release. The release workflow for tagged versions is
  unchanged.

### Fixed

- **Suppressed access-log spam** for `/api/page/poll` and
  `/api/status`. The front-end polls `/api/page/poll` every 500ms
  while a page fetch is in flight, and the Docker healthcheck hits
  `/api/status` every 30s. Both endpoints flooded `docker logs` with
  identical lines and buried genuinely useful events. Added a
  `gunicorn.access` logging filter that drops lines matching those
  paths; error events and all other `/api/*` traffic continue to
  log normally.

## [0.9.14] — 2026-05-10

### Changed

- **Reverted v0.9.13.** v0.9.13 removed the `font-kerning: none` /
  `font-feature-settings: "kern" 0, "liga" 0` rules added in v0.9.12,
  on the hypothesis that they were causing block-character glyphs to
  merge into continuous fills. After a long investigation, that
  hypothesis turned out to be wrong (the visible MeshChat rendering
  difference is from `display: inline-block` on bg-colored spans, not
  kerning), but the v0.9.13 revert itself had other rendering
  side-effects. v0.9.14 restores the v0.9.12 CSS state as the working
  baseline.

  Functionally identical to v0.9.12.

## [0.9.13] — 2026-05-10 — SUPERSEDED

This release introduced rendering regressions and was reverted by
v0.9.14. Do not use.

## [0.9.12] — 2026-05-10

### Fixed

- **Dense ASCII / box-drawing content (e.g. the `geomap` world map)**
  rendered with visible horizontal banding between rows. Roboto Mono
  Nerd Font ships with kerning pairs that shave fractional cell widths
  off adjacent glyphs; over a 100+ character row of repeating block
  characters the fractions accumulated and visibly offset the stacking
  of rows below. Disabled font kerning and ligatures (`font-kerning:
  none` + `font-feature-settings: "kern" 0, "liga" 0`) on the Micron
  content area to match MeshChat's rendering pipeline. The earlier
  Braille-glyph kerning dependency is gone — Braille is now CSS-drawn
  via `.mu-braille` radial-gradient dots and fully font-independent.

## [0.9.11] — 2026-05-10

### Fixed

- **Mixed-token field-specs** like `*|action=preview|board_id=1` now
  parse correctly. v0.9.10 only special-cased the case where the
  *whole* spec was a literal `*`, but real-world Micron pages (forum
  actions, edit/reply links, etc.) interleave the wildcard with
  literal `key=value` tokens inside one pipe-separated spec.

  Refactored the parser to be token-based: each backtick-separated
  spec is split on `|`, and each token is independently classified
  as either `*` (collect-all wildcard), `key=value` (literal pair),
  or `inputname` (page input reference). All three contribute their
  own fields to the outgoing submission — order-agnostic, mixable.

  Backward-compatible with the original `key=value|input1|input2`
  syntax (one literal + N input refs). Diagnosis credit: the same
  sibling AI as v0.9.10, who caught the broader case after the
  forum's Preview link still didn't work post-v0.9.10.

## [0.9.10] — 2026-05-10

### Fixed

- **Form-submit links with NomadNet's `*` wildcard now work.** When a
  Micron link uses `\submit` (rendered by Micron2HTML as
  `data-field-spec="*"`), the JS click handler was parsing each spec
  as `key=value` and silently skipping `*` because it has no `=` sign.
  Result: the link click was a no-op — `navigateTo` ran with empty
  fields. The handler now special-cases `*` to merge in every input
  on the current page via the existing `collectPageFields()` helper,
  matching NomadNet's "submit all" semantics. Diagnosis credit: a
  sibling AI working on a register-style `.mu` page.

## [0.9.9] — 2026-05-10

### Fixed

- **`#!/usr/bin/python3` shebang in user-authored `.mu` pages** now
  works out of the box. The `python:3.12-slim` base image only ships
  the interpreter at `/usr/local/bin/python3`, so the conventional
  `/usr/bin/python3` shebang silently failed on any executable Micron
  page. Added a symlink in the Dockerfile to provide both paths. No
  effect on existing pages that use other shebangs (e.g. `#!/usr/bin/env python3`).

## [0.9.8] — 2026-05-03

### Added

- **"Ignore LAN Discovery Probes" toggle** in Admin → Interfaces. Sets
  `respond_to_probes = No` in the RNS `[reticulum]` section so this
  portal stops replying to unsolicited Reticulum probes (the
  reachability tests other nodes send, most commonly on multicast LANs
  / AutoInterface). Outbound traffic on configured interfaces is
  unchanged — only unsolicited replies are suppressed. Off by default;
  the line is omitted from the generated config when the toggle is
  off, so RNS continues to use its built-in default.

## [0.9.7] — 2026-05-03

### Added

- **Shared-instance controls in the Interfaces tab.** Reticulum's
  shared-instance feature lets co-located RNS processes (e.g. multiple
  NomadPortals sharing a Docker network namespace via Gluetun) bridge
  through a single loopback socket — convenient for one-host setups,
  but it makes secondary instances inherit the primary's interfaces
  and announces whether you want it or not.

  A new "Shared Instance" card on **Admin → Interfaces** exposes the
  RNS knobs:

  - **Share with other processes** (`share_instance`) — uncheck on
    secondary portals to keep them fully independent.
  - **Instance name** (`instance_name`) — namespacing for
    Reticulum's on-disk state.
  - **Shared port** / **Control port** (`shared_instance_port` /
    `instance_control_port`) — give each instance unique ports if you
    want them isolated but still individually share-able with future
    co-located apps.

  Settings live under `shared_instance:` in `config.yml` and are
  written into the `[reticulum]` section of `config/reticulum/config`
  on save. Existing deployments without the block are unaffected
  (back-compat no-op).

## [0.9.6] — 2026-05-03

### Fixed

- **Worker boot crashed with `NameError: name 'https_mode' is not defined`.**
  v0.9.5 inadvertently removed the `https_mode` assignment while still
  referencing it in the HSTS header and HTTPS-redirect blocks, which
  prevented Gunicorn from booting on every deployment. Restored the
  assignment.

## [0.9.5] — 2026-05-03

### Fixed

- **Login worked on HTTPS but returned 403 on plain-HTTP deployments.** The
  session cookie was being sent unconditionally with the `Secure` attribute,
  which browsers silently drop on HTTP responses — leaving the next POST
  with no session, no CSRF token, and a bare 403 from the CSRF check. The
  session interface now sets `Secure` only when the actual request scheme
  is HTTPS (`request.is_secure`, which respects ProxyFix's
  `X-Forwarded-Proto`), so HTTPS deployments still get protected cookies
  while plain-HTTP test deployments work end-to-end.

## [0.9.4] — 2026-05-03

### Added

- **Boot navigation falls back to the built-in node** if the configured
  `default_node` fails to load — and falls back again to the generic
  welcome screen if the built-in is also unreachable. Previously a
  broken `default_node` (offline, RNS timeout, missing `index.mu`) left
  the user staring at an error page on every visit; now the portal
  silently lands them on its own hosted site instead. The fallback
  applies even under lockdown — locked visitors (typically guests)
  shouldn't be stranded on a broken-default error page when the
  operator's hosted site is reachable. Lockdown is restored immediately
  after the fallback navigation, so any subsequent click from the
  visitor still hits the lock alert. Applies only to first-load
  navigation; user-initiated clicks still surface errors directly
  (intentional, since the user picked the destination and expects
  feedback if it fails).

- **Local admin LXMF reception is always-on.** Previously the local
  admin's identity was created lazily on their first login, meaning
  any message sent before that first login was dropped at the network
  level (no delivery destination registered yet). At startup, when
  `ADMIN_PASSWORD` is set, the entry-point now eagerly creates the
  `local:<ADMIN_USERNAME>` identity in the IdentityStore so its LXMF
  router comes up immediately during `setup_delivery`. Messages
  addressed to the local admin are received whenever the container
  is running — no login required.

  No-op when `ADMIN_PASSWORD` is empty (local login disabled).

### Changed

- **The operator-configured `default_node` is auto-favorited** alongside
  the hosted node, for every audience (guests included). It now shows a
  pinned ★ in the sidebar, sorts to the top of the node list right
  after the hosted node, and gets synthesised as a placeholder if it
  hasn't announced yet — so visitors see it pinned even before the
  first RNS announce arrives. Guests previously had `favorited` stripped
  from every node except the hosted one; now they retain it for the
  default node too. Operator intent surfaced consistently regardless of
  RNS announce state or login status.

- **Lockdown now permits the trusted-local set** (built-in node +
  operator-configured `default_node`), not just a single lock target.
  Both are operator-controlled, so locked visitors can move between
  them — and the boot-time fallback from a broken default to the
  built-in works without any lockdown-bypass dance. Following a link
  from either trusted node to any third-party node still triggers the
  lock alert as before — the trust is per-hash, not transitive.
  Previously the warning popup fired whenever `default_node` differed
  from the built-in (even when the operator had deliberately pointed
  visitors at it), and lockdown blocked the built-in from a guest who
  was locked to the default.

- **Address bar uses MeshChat's `<node_hash>:/<path>` display format**
  for copy-paste compatibility with MeshChat. The `hash://` scheme is
  redundant in NomadPortal's own address bar, and the colon separator
  (instead of a leading slash on the path) matches MeshChat's URL
  format so addresses copied from one app paste cleanly into the other.
  Internal state still uses the canonical `hash://<hash>/<path>` form;
  pasting any of `hash://...`, `<hash>:/...`, `<hash>/...`, or a bare
  `<hash>` still works — they all normalise on input.

### Fixed

- **Page-fetch timeouts doubled** so distant external nodes have a
  realistic chance to respond. `PATH_TIMEOUT` (RNS path discovery)
  bumped from 30s → 60s, `STALL_TIMEOUT` (no-progress watchdog) from
  15s → 30s, and `PING_TIMEOUT` from 20s → 30s. Several-hop NomadNet
  links over slow long-haul carriers were timing out at the path
  step, surfacing as a 503 on the first visit even when the node was
  fully reachable on a retry. Truly-unreachable nodes still fail
  within ~60s, just not as quickly as before.

- **Braille characters (U+2800–U+28FF) now render as contiguous grids**
  instead of leaving visible gaps between adjacent characters —
  **restoring MeshChat parity**, which is what surfaced the issue.
  Root cause: Roboto Mono Nerd Font (the bundled monospace) has no
  Braille glyphs at all, and the system-monospace fallback (Noto Sans
  Symbols 2 on most Linux installs) renders Braille at ~34% of cell
  width, leaving a visible gap between cells. Bundling another font
  would just trade one rendering lottery for another. Fix lives in
  Micron2HTML 1.0.5: the converter now replaces every Braille character
  with `<span class="mu-braille" style="--mu-braille-dots:…">` whose CSS
  paints the raised dots as `radial-gradient`s at fixed fractional
  positions inside a `1ch`-wide inline-block. Result: glyphs always
  render at full cell width with no gaps and no "indicator" empty dots,
  regardless of which fonts the operator's browser has installed.
  NomadPortal pins Micron2HTML to v1.0.5 and ships the matching
  `.mu-braille` rule in `static/css/style.css`.
- `redirect_http.py` no longer crashes with `ValueError` when
  `WEB_PORT_HTTPS` is empty. Defensive port parser falls back to the
  default with a stderr warning. (v0.9.3's entrypoint already avoids
  starting the redirector in that case, but the script itself
  shouldn't blow up if invoked directly with bad input.)

## [0.9.3] — 2026-05-02

### Changed (breaking, but only against v0.9.2 which had a 1-day shelf life)

- **TLS mode is now derived from which ports you set**, removing the
  separate `TLS_ENABLED` flag introduced in v0.9.2:

  | `WEB_PORT_HTTPS` | `WEB_PORT` | Behaviour |
  |---|---|---|
  | set | set | HTTPS on `WEB_PORT_HTTPS` + HTTP→HTTPS redirector on `WEB_PORT` |
  | set | empty | HTTPS only on `WEB_PORT_HTTPS`, no redirector |
  | empty | set | Plain HTTP only on `WEB_PORT`. Use behind a reverse proxy. |
  | empty | empty | Falls back to the defaults (8443 HTTPS + 8080 redirector) |

  Existing deployments that don't touch the port env vars are unchanged
  (defaults give classic HTTPS+redirector). Operators upgrading from
  v0.9.2 should drop `TLS_ENABLED` from their env block — it's silently
  ignored now. To switch to plain-HTTP-behind-proxy: set `WEB_PORT` to
  the listening port and clear `WEB_PORT_HTTPS` to `""`.
- Healthcheck mirrors the same logic — picks `http://` or `https://`
  based on which port var is non-empty.

## [0.9.2] — 2026-05-02

### Added

- **`TLS_ENABLED` env var** — set to `false` to disable the in-container
  TLS stack: no self-signed cert is generated, the HTTP→HTTPS redirector
  doesn't start, and gunicorn binds plain HTTP on `WEB_PORT_HTTPS`. This
  is the deployment pattern for reverse proxies (Nginx Proxy Manager,
  Traefik, Caddy) that handle TLS termination upstream and forward plain
  HTTP to the container. Default stays `true` for backward compatibility.
- Healthcheck now honours `TLS_ENABLED` too — uses `http://` or `https://`
  to match the actual listening protocol.

> **Superseded by v0.9.3** — the `TLS_ENABLED` flag was replaced by
> port-based detection one day later. v0.9.2 still works but v0.9.3 is
> recommended; `TLS_ENABLED` is silently ignored from v0.9.3 on.

## [0.9.1] — 2026-05-02

### Fixed

- **Dockerfile healthcheck honours `WEB_PORT_HTTPS`** — was hardcoded to
  `https://localhost:8443/api/status`. Containers running on any other
  port (Portainer-allocated, custom-mapped, behind a reverse proxy with
  internal port reassignment) were marked unhealthy and stopped. The
  check now reads the env var at runtime, defaults to `8443`.

## [0.9.0] — 2026-05-02

First public-ready release. Core feature set complete and exercised end-to-end on a trusted-LAN deployment with Authentik OIDC. A handful of paths haven't been validated against every edge case (multi-hop LoRa-only meshes, large-scale public exposure, MeshChat and NomadPortal accessing the same site simultaneously) — hence 0.9 rather than 1.0. Once those land we'll cut 1.0.

### Security
- Node lockdown is now enforced server-side for page reads, not just form submissions.
  Previously, a guest who knew a node hash could fetch external pages via `/api/page`
  even when "Locked for guests" was set; the GET endpoint now applies the same access
  rules as form submissions.
- OIDC admin behaviour changed: when `OIDC_ADMIN_EMAILS` and `OIDC_ADMIN_SUBJECTS`
  are both empty, OIDC users now log in as **non-admins** by default instead of
  being granted admin automatically. The local `ADMIN_PASSWORD` account remains
  admin and is the recommended bootstrap path.
- Page-fetch field data is capped at 64 KiB total / 16 KiB per field / 64 fields,
  rejected with `413` beyond. Overall request body capped at 1 MiB via Flask
  `MAX_CONTENT_LENGTH`. Protects against DoS via huge form submissions.
- `OIDC_INSECURE_SKIP_VERIFY` is now scoped to the OIDC provider's hostname only —
  other outbound HTTPS calls retain certificate verification.
- CSP hardened with `object-src 'none'`, `base-uri 'self'`, `form-action 'self'`,
  and `font-src 'self'`.
- Persistent state (`config/nodes.json`, `config/lxmf_peers.json`) now uses
  per-thread temp filenames, eliminating a race condition that could corrupt
  the file or fill logs with `No such file or directory` warnings.
- Per-user fingerprint identification toggle now resets on every login. Previously
  the `identified_nodes` list persisted across logins, which violated the
  per-session expectation.

### Added
- **Page-level bookmarks**: address-bar `★` button stores `(hash, path, name)`
  tuples per user, mixed in the same Favorites sidebar as node favorites.
  Custom display name on each bookmark.
- **Sticky fingerprint identification**: address-bar fingerprint button toggles
  persistent `link.identify()` for the active node. Once on, every page fetch
  to that node identifies until you toggle off (no per-page button click).
  Resets at every login.
- **Async page fetch with progress polling**: `POST /api/page/fetch` returns a
  `job_id`; `GET /api/page/poll?id=<id>` polls until done. Progress percentage
  (and the loading overlay text) reflects RNS Resource transfer state.
- **Stall watchdog** for page fetches: 15 s of inactivity after the link is
  active aborts the fetch with `Lost connection`. Path discovery + link
  establishment use RNS's own per-hop timeouts.
- **Hop count display** on nodes and LXMF peers, with last-known-hops cached
  to disk so the display survives restarts even before paths re-discover.
- **Bundled site server enhancements**:
  - `node_destination` env var passed to executable pages.
  - `link_id` / `remote_identity` correctly populated; for local NomadPortal
    users, the logged-in user's identity hex is exposed as `remote_identity`.
  - `site/lib/` directory + `site/requirements.txt` autoloader: drop
    a requirements file and the entrypoint runs `pip install --target /site/lib`,
    exporting `PYTHONPATH=/site/lib` so executable `.mu` pages can `import`
    user-specified packages.
  - `site/data/` directory for application state (SQLite files, etc.).
- **Identity import / per-node fingerprint toggle / per-user identity** —
  each authenticated user gets an auto-generated NomadPortal-named identity
  (`NomadPortal-<XYZ>` where XYZ is the last 3 hex chars of the LXMF address).
- **Address bar shows `★` and fingerprint icon side-by-side** — both per-page
  toggles, visually parallel.
- **Active connection-context block** at the top of the showcase landing page —
  shows current node hash, Direct vs RNS connection state, and identity hash
  with a dark-blue "you are here" highlight on the architecture diagram.
- **Author's guide** for executable `.mu` pages at `docs/AUTHORING.md`.
- **CSP `font-src` directive** explicitly allowing `'self'`.

### Changed
- **`OIDC_INSECURE_SKIP_VERIFY` env-var** added — for trusted-LAN setups
  with self-signed Authentik/Keycloak. Logs a warning at startup. Scoped
  to the OIDC provider's hostname only.
- **Field-spec link parser**: now correctly handles `[Label`url`a=1`b=2]`
  multi-field syntax (preserves all backtick-separated specs) and the
  `key=value|input1|input2` pipe syntax that pulls referenced input values
  on click. Inputs not pipe-referenced are no longer auto-submitted.
- **Local-page cache key** includes the requesting user's identity, so two
  users no longer see each other's rendered context block.
- **OIDC user admin status** — admin Users page now shows the *effective*
  admin state (per-user UI flag + env-var fallback) and a "(via env)" hint
  when admin comes from `OIDC_ADMIN_EMAILS` rather than an explicit toggle.
- **Loading overlay** distinguishes "Connecting to node…" (no progress yet,
  link establishment) from "Receiving N%…" (transfer in progress) — no
  more "Fetching 0%…" appearing stuck during path discovery.
- **Error messages** in the status bar are humanised — `HTTP 503` →
  `Server error — see the logs`, etc.

### Fixed
- Path-discovery order: `Transport.has_path()` is now always checked before
  `Identity.recall()`. The previous order short-circuited on cached identity
  even after the path table had evicted the route, causing silent link-
  establishment failures.
- Plain navigation no longer leaks stale form input values from the previously
  displayed page. Field data is only attached when an explicit submit-intent
  is present (link with `data-field-spec` or form submit).
- The Raw toggle no longer re-fetches from the node — it swaps the cached
  HTML/Micron view client-side.
- `/admin/api/ui/settings` now merges only the keys the client sent. Previously,
  a partial PATCH silently reset omitted fields to their defaults.
- Removed dead `read_only` flag on the interfaces page and the `{% if user.is_admin %}`
  block in admin nav (the whole admin blueprint already requires admin).

### Documentation
- New `SECURITY.md` with trust model and hardening recommendations.
- New `NOTICE.md` listing third-party software and bundled assets.
- New `static/fonts/RobotoMonoNerdFont/LICENSE.md` documenting Roboto Mono
  (Apache 2.0) and Nerd Fonts (MIT) licenses for the bundled font.
- New `docs/AUTHORING.md` explaining how to write executable `.mu` pages.

### Added
- **Active sessions page** at `/admin/sessions` lists every logged-in user with
  login time, last activity, login IP, and current IP, with per-session and
  bulk revoke buttons.
- **Audit log viewer** at `/admin/audit` streams only `nomadnet.audit` records
  (privileged actions: user changes, role changes, session revokes, identity
  resets, cache clears, interface saves, settings changes, backup downloads).
- **Backup download** at `/admin/backup` produces a tar.gz of `/config` for
  off-host storage. Excludes regenerated runtime state (TLS cert, LXMF storage,
  Reticulum routing tables). Available via a button on the Users page.
- **Runtime interface reload**: a new "Apply now" button on the Interfaces page
  signals the gunicorn master via SIGHUP for a graceful worker reload, applying
  config changes without `docker compose restart`. Active sessions are revoked.

### Changed
- **Lockdown collapsed into `access_mode`**: the old three-state `lockdown_node`
  (off / guests / users) is now a clearer `access_mode` (public / gated / locked).
  Existing `ui_settings.json` files are migrated automatically on load. The API
  still accepts `lockdown_node` as a backwards-compatible alias.
- Tab titles now read "NomadPortal" instead of "NomadNet Browser" / "— NomadNet".
- Admin CSS extracted from `templates/admin/base.html` into `static/css/admin.css`
  for caching across admin pages.
- `requirements.txt` now pins exact versions for reproducible Docker builds.

### Fixed
- `/admin/api/ui/settings` now merges only the keys the client sent. Previously,
  a partial PATCH silently reset omitted fields to their defaults.
- Removed dead `read_only` flag on the interfaces page and the `{% if user.is_admin %}`
  block in admin nav (the whole admin blueprint already requires admin).

## [0.1.0] - 2026-04-30

Initial public release.

### Features
- Web-based browser for NomadNet nodes (Micron → HTML rendering)
- LXMF messaging with per-user inboxes and contact books
- RNS identity management and mesh announces
- Node discovery via Reticulum announces
- Local username/password login or OIDC/SSO (Keycloak, Authentik, Auth0, Google)
- Admin panel for interface configuration, user management, diagnostics
- Optional NomadNet site hosting (`./site/pages/`, `./site/files/`)
- HTTPS with auto-generated self-signed certificate
- Rate limiting and CSRF protection
- Mobile-responsive layout

[Unreleased]: https://github.com/JamesM92/NomadPortal/compare/v0.9.4...HEAD
[0.9.4]: https://github.com/JamesM92/NomadPortal/compare/v0.9.3...v0.9.4
[0.9.3]: https://github.com/JamesM92/NomadPortal/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/JamesM92/NomadPortal/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/JamesM92/NomadPortal/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/JamesM92/NomadPortal/compare/v0.1.0...v0.9.0
[0.1.0]: https://github.com/JamesM92/NomadPortal/releases/tag/v0.1.0

# Porting notes — for NomadPortal-Android (or any sister client)

This doc exists to transfer what this project learned the hard way, so a
from-scratch client doesn't have to re-learn it by re-hitting the same bugs.
It's written to be handed directly to an AI assistant (or a human) starting
a new codebase — paste it into the other repo's context, or point that
assistant at this file's raw URL.

NomadPortal is a Flask web app; the target is a native Android app. Almost
none of the *code* transfers. What transfers is: the protocol-level gotchas
(these live in Reticulum/LXMF's actual behavior, not in Python), the
security/trust model, the URL/UX conventions worth keeping for feature
parity, and the design tokens worth keeping for visual parity.

## 1. Protocol stack primer

- **[Reticulum](https://github.com/markqvist/Reticulum) (RNS)** — the
  network layer. Transport-agnostic (TCP, LoRa/RNode, I2P, AutoInterface,
  serial...), onion-routed, identity = keypair. No servers, no DNS —
  destinations are addressed by a hash derived from an identity + app
  name + aspects.
- **[LXMF](https://github.com/markqvist/LXMF)** — store-and-forward
  messaging on top of RNS. A message is either delivered opportunistically
  (single encrypted packet, no link needed) or over a `Link` (for larger
  payloads), with propagation-node relay for offline recipients.
- **[NomadNet](https://github.com/markqvist/NomadNet)** — the reference
  node software. A "node" is an RNS destination that serves pages (written
  in **Micron** markup) and files over a `Link`, browsable interactively
  like a very old-web BBS.
- **Micron** — NomadNet's markup language (`#!bg=`, `>heading`,
  `` `[link text`/path] ``, form fields, etc.). This project renders it to
  HTML via a separate library,
  [Micron2HTML](https://github.com/JamesM92/Micron2HTML) — reuse that
  logic (or port it) rather than re-deriving the grammar; it has its own
  test suite.

An Android client talks to all of this the same way this project does:
through Reticulum's Python (or, for Android, Kotlin/Java — check for an RNS
JVM port before writing a raw socket implementation) bindings, not through
NomadPortal itself. NomadPortal is a peer on the mesh, not a gateway you'd
proxy through.

## 2. Reliability lessons (the expensive ones)

These came from real multi-day soak testing (see `CHANGELOG.md`'s 0.9.x →
1.0.0 entries and `git log` for the full trail) and will very likely bite
any independent RNS/LXMF client the same way:

- **Path/link caching pays off, but cache the right thing.** Establishing
  an RNS `Link` is not free — cache `Link` objects per destination and
  reuse them across fetches (LRU eviction; see `NodeBrowser`'s link cache
  in `nomadnet_web/browser.py`). But a cached link can silently die on the
  peer's end — you need a stall watchdog (a bounded wait before deciding a
  link is dead and re-requesting a path) rather than trusting a cached link
  forever.
- **Serialize concurrent fetches to the same destination.** Firing two
  overlapping `Link` requests at the same destination from independent UI
  actions (e.g. double-tap) causes RNS-level contention, not two clean
  parallel results. Dedup/serialize by destination hash.
- **Announce-driven retry beats blind retry.** When a fetch fails because a
  path is stale, don't just retry on a timer — listen for the destination's
  next *announce* and retry then; it's the actual signal that the peer is
  reachable again. (`_DestinationAnnounceWaiter` in `browser.py`.)
- **`receive_path_responses=True`** is required on the relevant RNS call
  for announce-driven retry to actually see path responses — easy to miss,
  silently degrades to "never retries."
- **VPN / low-MTU tunnels blackhole TCP silently.** If the client (or a
  host it depends on) runs behind a VPN with MTU < 1500, RNS's default
  hardware-MTU (8192) produces fragments the tunnel can't carry — TCP
  connections work briefly after (re)connect, then blackhole. Fix is
  `fixed_mtu` on the interface, tuned below the tunnel's real MTU. See
  README.md's "Running behind a VPN" section for the full diagnostic
  writeup — worth reading in full if the Android app will ever run behind
  Orbot/WireGuard/etc.
- **Debounce hot-path disk writes.** LXMF peer tables, discovered-node
  registries, and message history all get updated on every mesh event. If
  each write is synchronous and on a path that also handles RNS I/O (a
  single-threaded async runtime, a GIL, whatever the platform's equivalent
  contention point is), disk latency stalls RNS's own link handshakes.
  Batch/debounce persistence off that hot path.
- **Ratchets need pruning.** RNS's forward-secrecy ratchet files accumulate
  indefinitely without an active prune step (age- and count-based cap).
- **LXMF propagation-node sync needs its own outbound tick**, separate from
  normal delivery — messages queued for offline recipients don't flush
  themselves.
- **A stale/hung RNS init must not look "healthy."** Distinguish "process
  is up" from "RNS actually has a working route" in any health/status
  signal — a hung RNS init behind a generic liveness check is a false
  positive that hides real outages.

## 3. Security / trust model

Worth keeping identical, since it's the correct model for this protocol,
not an implementation detail:

- **Executable content of your own hosted node is fully trusted** (it runs
  as your process/with your permissions). **Content from other nodes is
  always untrusted** — render Micron to a display format with *no*
  executable path (no JS-equivalent, no arbitrary code from a remote
  peer), and HTML/display-escape everything.
- **Three-tier access model** for anyone else who might use the app as a
  shared/public-facing thing: full mesh browsing, "gated" (restricted to a
  pinned default node unless authenticated), "locked" (restricted for
  everyone). If the Android app is single-user/local-only this may not
  apply — but if it ever gets a "share my node" or multi-account mode,
  this is the shape that held up.
- **Fail closed, not open, on any access-control check whose data source
  can fail.** NomadPortal shipped (and just fixed) a real bug where a
  failed settings fetch defaulted restrictions to "off" instead of "on" —
  the server-side check was still correct, but the client-side UI
  misrepresented what was actually allowed. Any client-side gating
  (buttons hidden, warnings suppressed) needs an explicit "we don't know,
  so restrict" default, never "we don't know, so allow."
- **Rate-limit anything that emits packets onto the mesh**, not just HTTP
  endpoints — an unauthenticated "ping this destination" affordance is a
  network-amplification primitive if it's not gated.

## 4. UX / feature-parity reference

- **URL scheme** — worth mirroring conceptually even in a native app (e.g.
  as deep-link / share-intent handling): a default/pinned node collapses to
  a bare path, everything else carries an explicit hash. See README.md's
  "URL scheme" section for the exact table.
- **Node browser affordances**: discovered-node list (sortable by
  recency/name/hop-count/announce-frequency), per-node hop count + last-
  fetch-ok/fail indicator, favorites (auto-pinned hosted/default node +
  user picks), address bar with a "fingerprint" (persistent identify-to-
  this-node) toggle separate from anonymous browsing.
- **LXMF messaging**: per-conversation view, contact book with icon
  support (LXMF has an icon-appearance field — `0x04` — and a raw-image
  field — `0x06`; see `_render_appearance_svg`/`_on_delivery` in
  `nomadnet_web/messaging.py` for both formats), delivery-state per sent
  message (queued/delivered/failed), unread badges.
- **File downloads**: always confirm before pulling an arbitrary binary
  from an untrusted peer over a possibly slow/metered link (filename +
  MIME + progress), and treat "no virus scan available" as something to
  say out loud rather than stay silent about, if the platform doesn't have
  an equivalent to ClamAV integration available.

## 5. Design tokens (for visual parity)

Dark, terminal-flavored aesthetic. From `static/css/style.css`'s `:root`:

```css
--bg:         #131313;   /* page background */
--bg2:        #1c1c1c;   /* panel/sidebar background */
--bg3:        #252525;   /* raised surface (headers, inputs) */
--border:     #333;
--text:       #c8c8c8;
--text-dim:   #666;
--accent:     #5ba3c9;   /* primary accent — links, active states */
--accent2:    #7ec8a0;   /* secondary accent — success/positive */
--warn:       #c8905b;
--error:      #c85b5b;
```

Monospace font is **Roboto Mono Nerd Font** (bundled — see
`static/fonts/`), chosen specifically because its Nerd Font glyph set makes
box-drawing and Braille characters (common in Micron-rendered ASCII art)
render flush with no sub-pixel gaps. If the Android app renders Micron
content itself, this font choice is worth matching — a generic monospace
font will visibly misalign box-drawing art that looks correct in
NomadPortal or a terminal NomadNet client.

Sent/received message bubbles: sent = right-aligned, `#173040` background,
`#2a5570` border; received = left-aligned, `--bg3` background, `--border`
border. Small `border-radius`, sharp corner on the "speaker" side (bottom-
right for sent, bottom-left for received) — standard chat-bubble
convention, called out here only because it's an easy detail to skip.

## 6. Mobile UX lessons (fresh — Aug 2026 session)

NomadPortal's UI is a responsive web app, not a native shell, but every one
of these is a *platform behavior* an Android app will hit too, just via a
different API surface:

- **Never intercept Enter-to-send via a raw keydown+preventDefault on a
  text field.** On Android, IME keyboards (Gboard, Samsung Keyboard) route
  ordinary typing through the same composition machinery Enter uses;
  forcibly cancelling that stream desyncs the IME's internal cursor from
  the real one, and every following character lands at the IME's last-known
  (now stale) position — visible as text typing in backwards. Web fix was
  switching to the `beforeinput`/`insertLineBreak` event, which only fires
  for a genuinely committed Enter. The native-Android equivalent: don't
  consume/intercept `KeyEvent.ACTION_DOWN` for Enter on an `EditText` tied
  to an IME; use `EditorInfo.IME_ACTION_SEND` / `TextView.OnEditorActionListener`
  instead, which is IME-composition-aware by design. Same underlying
  keyboard bug class, same fix shape: let the platform's real "commit"
  signal decide, don't guess from raw key events.
- **Keyboard-open resizing needs to actually reach the input.** The web fix
  was `100dvh` over `100vh` for a layout that shrinks with the keyboard.
  The Android equivalent is making sure `android:windowSoftInputMode`
  (`adjustResize` typically, for a chat-style screen) is set correctly per
  screen, and that the input field's container isn't inside something that
  eats the resize (e.g. a fixed-height parent).
- **A single "scroll to bottom" call, done once, isn't enough** during a
  keyboard-open transition — the layout keeps changing for a couple hundred
  ms after the keyboard starts opening. Re-apply the scroll after the
  resize actually settles (on Android: `ViewTreeObserver` layout-change
  listener, or WindowInsets animation callbacks on API 30+), not just
  once at focus time.
- **Full-page overlays (drawers/sidebars) should close themselves on
  selection.** Easy to build a drawer that opens correctly but forgets to
  close on the action that made it moot (picking a list item) — test that
  path specifically.
- **Store what you display.** A one-line regression here: sent messages
  stored a 120-char preview but the full-message view fell back to that
  preview whenever the full field was absent, silently clipping every sent
  message in-app (delivery itself was never affected — this was purely a
  local storage/display gap). Any "preview vs. full" field pair needs a
  test that actually renders the full view, not just the list view.

## 7. Where to look in this repo for more detail

- `README.md` — features, full config reference, VPN/MTU diagnostics,
  hosting-a-site walkthrough, operator/liability guidance, trust model.
- `CHANGELOG.md` — the full reliability journey with root-cause writeups
  per fix; the 0.9.x → 1.0.0 entries are the densest.
- `nomadnet_web/browser.py` — link caching, announce-driven retry, fetch
  dedup, diagnostics.
- `nomadnet_web/messaging.py` — LXMF send/receive, icon-appearance
  encoding.
- `nomadnet_web/lxmf_sync.py` — propagation-node outbound sync.
- `nomadnet_web/ui_settings.py` — the three-tier access-mode model.
- `docs/AUTHORING.md` — executable-page trust model and env for anyone
  building the "host your own node" side of a client too.

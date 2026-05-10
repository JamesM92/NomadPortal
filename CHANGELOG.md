# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.13] — 2026-05-10

### Fixed

- **Quarter-block characters lost their column gaps.** v0.9.12 added
  `font-kerning: none` and `font-feature-settings: "kern" 0, "liga" 0`
  to suppress what looked like horizontal banding in dense ASCII-art.
  Wrong hypothesis: those rules collapsed the *natural* sub-pixel
  separators that Roboto Mono Nerd Font generates between adjacent
  block / quadrant glyphs (▙ ▟ ▛ ▜ ░ ▒ etc.), making characters merge
  into continuous fills instead of distinct cells like MeshChat shows.

  Reverted both rules. The bundled font's default kerning/features now
  flow through unmodified, matching MeshChat's minimal CSS (which only
  sets `font-family` and `line-height: normal` on its `<pre>`
  container — no kerning overrides). Confirmed by reading MeshChat's
  upstream stylesheet at `src/frontend/components/nomadnetwork/NomadNetworkPage.vue`.

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

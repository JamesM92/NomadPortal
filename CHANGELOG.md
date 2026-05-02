# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/JamesM92/NomadPortal/compare/v0.9.2...HEAD
[0.9.2]: https://github.com/JamesM92/NomadPortal/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/JamesM92/NomadPortal/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/JamesM92/NomadPortal/compare/v0.1.0...v0.9.0
[0.1.0]: https://github.com/JamesM92/NomadPortal/releases/tag/v0.1.0

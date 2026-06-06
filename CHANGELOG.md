# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

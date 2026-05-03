# Security Policy

## Reporting a vulnerability

If you discover a security issue in NomadPortal — for example, anything that could allow unauthorized access, leak private keys, expose user messages, or be used to attack the host — **please do not open a public GitHub issue.**

Instead, please report it privately through one of the following channels:

- **Preferred:** [GitHub private vulnerability reporting](https://github.com/JamesM92/NomadPortal/security/advisories/new)
- Email: jamesmanley1992@gmail.com (subject line prefix: `[NomadPortal security]`)

Include:
- A description of the issue and its impact
- Steps to reproduce, or a proof-of-concept
- The version / commit hash you tested against
- Any suggested mitigation

You should expect an acknowledgement within 7 days.

## Scope

In scope:
- The Flask web application (`nomadnet_web/`)
- Docker image and entrypoint
- Default configuration

Out of scope:
- Issues in upstream Reticulum, LXMF, or NomadNet itself — please report those to their respective projects
- Issues in [Micron2HTML](https://github.com/JamesM92/Micron2HTML) (the Micron → HTML library) — report those to that repository's security policy
- Misconfiguration by the operator (e.g. running with a default password)
- Issues that require physical access to the host or root inside the container

## Supported versions

Only the latest tagged release receives security fixes. NomadPortal is a single-operator self-hosted application — the recommended posture is "stay current."

## Trust model

NomadPortal is a single-operator application. The security boundary is between **the operator** (you) and **the network** (everything else):

- **Logged-in users are partially trusted.** They can submit forms, send LXMF messages, and browse external nodes. They cannot upload content to your site or change configuration unless they are also admins.
- **Admin users are highly trusted.** They can edit configuration, change interface settings, and reach the admin panel. Treat the admin role like root on the host.
- **Executable pages in `site/pages/` and packages in `site/requirements.txt` are fully trusted.** They run as the NomadPortal process. Anything in those locations is effectively code you wrote — don't accept user-uploaded `.mu` pages or `pip` packages.
- **External NomadNet nodes are untrusted.** Their content is HTML-escaped and rendered through Micron2HTML, which has no JavaScript execution path. Field submissions to external nodes go through `_can_interact` gating.

## Known trade-offs (deferred)

These are conscious decisions where we picked function over hardening. Documented here so future operators know the boundary, and so the project's "as is" disclaimer covers them.

- **Rendered link `href`s contain the destination `hash://` URL.** Normal in-app navigation goes through `POST /api/page/fetch` (URL in body, not query string), so the destination is invisible to upstream proxies / Cloudflare during ordinary browsing. But if a user Ctrl-clicks "Open in new tab", right-clicks "Copy link", or shares a link, the URL ends up in the browser's address bar and any GET request that follows reveals it through the request line — visible to whatever's between the browser and the origin.

  *Future mitigation if the concern comes back up:* change Micron2HTML's link renderer to emit `<a href="#" data-url="hash://..."` and have `app.js`'s click handler read `data-url` instead of decoding `href`. Cost: copy-link / open-in-new-tab / share-link all break.

- **File downloads from external NomadNet nodes are blocked.** Micron2HTML's `default_url_resolver` rewrites any `/file/...` link to `href="#"` so users can't accidentally pull binaries through the portal. The downside: there's no way to fetch a file at all, even when the user actually wants it (e.g. clicking what looks like a page link only to discover it's a file).

  *Future mitigation if we want downloads back, with a confirmation prompt:* three coordinated changes —
  1. **Micron2HTML (`converter.py:42-67`)**: drop the `_is_blocked` gate, or replace it with a marker class (e.g. `class="mu-link mu-file-link"`) so the frontend can still distinguish file links from page links.
  2. **NomadPortal backend**: add a new `POST /api/file/fetch` route alongside `/api/page/fetch` ([routes.py:449](nomadnet_web/routes.py#L449)) and a `browser.fetch_file()` method alongside [browser.py:204 `fetch_page`](nomadnet_web/browser.py#L204). It establishes the RNS link the same way `fetch_page` does, calls `link.request("/file/<name>")`, and streams the bytes back (or returns base64 — page fetches are small; files may be larger, so stream).
  3. **Frontend ([app.js:577](static/js/app.js#L577) click handler)**: detect file links (by URL pattern `/file/` or by the marker class), show `confirm("Download <filename> from <node-name>?")` before fetching. On OK, call `/api/file/fetch`, wrap the response in a `Blob`, create an object URL, and trigger a `<a download>` click to save to disk.

  Trade-off: increases attack surface — a malicious node could try to push large files, exotic filenames, or content-type confusion. Mitigations to apply when implementing: cap file size server-side, sanitize the filename before passing to the browser, never auto-execute, and keep the confirm dialog mandatory (no "remember this choice" option).

## Hardening recommendations for production deployments

If you're exposing NomadPortal beyond a trusted LAN:

1. Set a strong, persistent `FLASK_SECRET_KEY` (`openssl rand -hex 32`).
2. Set `ADMIN_PASSWORD` to a long random value or leave it empty and rely on OIDC.
3. Put a real TLS certificate in front (Let's Encrypt via your reverse proxy is easiest); set `HTTPS_REDIRECT: "true"` and `TRUSTED_PROXIES: "1"`.
4. Set `OIDC_ALLOWED_EMAILS` (or `OIDC_ALLOWED_SUBJECTS`) to gate logins to known users — empty values mean any authenticated OIDC user can sign up.
5. Keep `CACHE_TTL` short (`60`) on public instances so abuse content gets evicted quickly.
6. Set `abuse_contact` in admin Settings so visitors can report content.
7. Use the node blocklist (admin → Settings) to proactively block known-bad nodes.
8. **Don't enable `OIDC_INSECURE_SKIP_VERIFY`** unless your OIDC provider is on the same trusted LAN — it disables TLS verification for OIDC flows.

## Cryptography

NomadPortal does not implement cryptography itself — it relies on:

- **Reticulum** for mesh-layer crypto (link establishment, identity, packet encryption)
- **LXMF** for messaging encryption, signing, and stamps
- **Authlib** for OIDC token validation, JWT verification, signature checks
- **OpenSSL** (via Python's `ssl`) for HTTPS

Please report cryptographic issues against the upstream library, with a CC to us if it affects how we use them.

## Disclaimer

This software is provided as-is. The maintainer accepts no liability for use, misuse, or consequences of operating this service. See the disclaimer in the project README.

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

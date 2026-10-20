# Validation suite

This document describes how NomadPortal is checked before a release. It lists what a computer checks on every push, and what a person must check on a real network.

A release tag is held until every item in **Release gate** passes.

## What is checked, and where

| Layer | What it covers | Runs | Where |
|-------|----------------|------|-------|
| Lint | Syntax errors and unused names (`ruff --select F,E9`) | Every push | CI job **Lint** |
| Python tests | Rules and logic with no network: access policy, caches, pre-warm, traffic counters, SEO helpers, health endpoints, route responses | Every push | CI job **Test** (`pytest tests/`) |
| Browser tests | The real front end in headless Chromium against a mock backend (see below) | Every push | CI job **Browser tests** (`tests/e2e`) |
| Container smoke test | The image builds, starts, and answers on its port | Every push | CI job **Docker build** |
| Security scans | Dependencies, image, code, secrets | Pull request to `main` | `security.yml`, `codeql.yml` |
| Network checks | Behaviour that needs real Reticulum peers | Before a release | By hand (see below) |

### Browser tests

The suites are in `tests/e2e/suites/`. Each one states the behaviour it protects.

| Suite | Protects |
|-------|----------|
| `01-refresh-indicator` | A saved copy shows at once and refreshes behind it. Spinner, green dot, red X. Guests see the age, admins also see the reason. Moving to another page cancels a stale refresh. |
| `02-lockdown` | The browser follows the server's access snapshot. Locked guests are stopped at off-node links. Other guests get the external-site warning. |
| `03-offnode-links` | A link the server marked off-limits becomes plain text with a tooltip. |
| `04-cold-miss` | With no saved copy, a failed load retries quietly, then shows a plain error page with a Retry button. A real answer (such as 401) is not retried. |
| `05-server-rendered` | With JavaScript off, a page URL returns the content, title and canonical link. With JavaScript on, start-up has no errors. |
| `06-cache-directive` | A saved copy inside its page's `#!c=N` window shows as up to date with no network request. A plain revisit does not force a fetch. The Refresh button does. A copy past its window refreshes behind itself. |
| `07-partials` | A partial loads in place and refreshes on a timer. A `p:` link reloads it by id. Links inside partial content work. Failures are shown plainly. Nesting and count are capped. Leaving the page stops the timers. |
| `08-inline-images` | Images load through `/api/media` as data URLs, two at a time. Alt text shows while loading and when an image fails. A busy server is retried. Leaving the page drops waiting images. Malformed targets are never requested. |

When you change front-end behaviour, add a check to the suite that owns it. When you find a bug in the browser, write the failing check first.

### What the automated tests do not prove

The mock backend returns scripted answers. These tests prove that the front end reacts correctly to the server. They do not prove that the server, Reticulum, or another program behaves as the mock says. The checks below cover that gap.

## Checks that need a real network

Run these on a deployed instance. Record the date, the version, and the result of each step.

### A. Chat attachments with MeshChat

Use one NomadPortal account and one MeshChat. Send in both directions.

1. Send a small image (under 100 KB) from NomadPortal. It must show as an image in MeshChat.
2. Send a small image from MeshChat. It must show inline in NomadPortal.
3. Repeat steps 1 and 2 with an audio clip.
4. Repeat steps 1 and 2 with a file (a PDF).
5. Send a file larger than 500 KB from NomadPortal. The message must be refused with a clear error. Nothing may be sent.
6. Send a message with two attachments of the same kind. Note how MeshChat shows them.

If an image or audio clip shows in one direction only, the fault is in the mapping tables `_MIME_TO_IMAGE_EXT` and `_MIME_TO_AUDIO_MODE` in `messaging.py`.

### B. Voice calls

Use two accounts on two browsers or devices.

1. NomadPortal to NomadPortal: place a call. Both sides must hear audio. End the call from each side in turn.
2. Place a call to an account that is offline. The caller must get a clear result and no stuck call screen.
3. NomadPortal to MeshChat and MeshChat to NomadPortal: note whether the call connects and whether audio is heard. MeshChat uses a different codec; record what happens.
4. Use a browser without WebCodecs. The call button must explain that the browser is not supported.

### C. Saved copies and pre-warm on the live mesh

Set at least one prescribed node and leave the portal running for one hour.

1. In **Admin > Cache**, confirm that saved copies exist for the landing page and its links. (`/healthz/web` shows the count.)
2. Open the site as a guest. Pages must show at once and the green dot must appear.
3. Disable the network interface that reaches the prescribed node. Reload a page. The saved copy must show with a red X, and the tooltip must show its age.
4. Open a page that was never visited and has no saved copy. After about 30 seconds the plain error page must appear.
5. Enable the interface again and press Retry. The page must load.
6. Watch `/healthz/web` through steps 3 to 5. It must stay 200. `/healthz` is allowed to return 503 while the mesh is down.

### E. Inline images with NomadNet

1. On a NomadNet node, host a page with an image line (`` `(Logo`:/media/logo.webp) ``) and a WebP file. Open that page in NomadPortal. The image must appear.
2. On NomadPortal, host a page with an image line and a WebP file in `site/pages/`. Open it from a NomadNet terminal client that supports images. The image must appear.
3. Put a `.allowed` file next to the image. A reader who is not on the list must see only the alt text.
4. Host an image larger than 2 MB. NomadPortal must show the alt text and no error page.

### D. Public site

1. Fetch a page URL with `curl` (no JavaScript). The content, `<title>` and canonical link must be in the response.
2. Fetch `/sitemap.xml` and `/robots.txt`. Check that every address uses your public address (set `PUBLIC_URL`).
3. Paste a page URL into a chat app or social site. The preview must show the page title and description.
4. As a guest, follow a link to a node that is not on the prescribed list. It must show as plain text with a tooltip.
5. Open **Admin > Traffic** after a few visits. The counts must rise. No visitor details may appear.

## Release gate

A version tag is made only when all of these are true:

1. CI is green on the commit that will be tagged: Lint, Test, Browser tests, Docker build.
2. The `security.yml` and `codeql.yml` checks are green on the pull request from `dev` to `main`, with no suppressions (see the release steps in the project instructions).
3. Checks A to D were run on the release candidate within the last seven days, and every step passed. A step that failed has a fix, or an entry in the changelog that explains the known limit.
4. The changelog has no entry under `[Unreleased]` that is missing from the list of checks above (new behaviour needs a check).

## Gaps and plans

- Checks A to D are manual. A script that drives two real Reticulum instances on one host would automate A and part of C. This needs a safe, isolated Reticulum configuration, so it has not been built.
- There are no tests for `auth.py`, `admin_routes.py`, `site_server.py` (request serving), `config_gen.py`, or `scanner.py`. The first two are security-relevant and are the next to add.
- Browser tests run in Chromium only.

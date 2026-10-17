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
| `09-conversation-extras` | Chats can be filtered All / Trusted / Untrusted (unknown counts as untrusted), with counts and unread badges. "All" is the default and the choice is remembered. Pinned conversations sort first. A peer with a note shows a marker. The peer dialog saves trust, pin and note, and Escape saves nothing. |
| `10-send-control` | A queued message offers Cancel, a failed or cancelled one offers Retry, a delivered one has no buttons. A refusal is explained and the button works again. Automatic re-sends are marked. |
| `11-voice-message` | The Ogg Opus muxer, checked with an independent parser and (when ffmpeg exists) a real decoder: the audio comes out bit-identical. The recorder in Chromium: timer, stop, cancel, Escape, a staged `audio/ogg` file that the browser's own decoder opens, the microphone always released, a refused microphone explained, no room in the message, no button without WebCodecs. **Microphone capture is stubbed** (see below). |
| `12-propagation` | Settings > Propagation: Automatic first and naming the nearest node, node distances, choosing a node, going back to automatic, typing a node in (Enter works), a bad hash explained, Sync now with progress then the result, the inbox refreshed afterwards, a refused sync shown, polling stopped when the dialog closes. |
| `13-lxmf-address` | A messaging address (`lxmf@...`, an `lxma://` link, in angle brackets) typed in the page address bar opens a new message and fetches no page. A node address still opens a page. Signed out, a message explains. The dialog sends the address as typed and shows the server's reason when it is wrong. |
| `14-network-map` | The map layout under random data from a seeded generator (every destination placed once, none overlapping, each inside its interface's slice, farther means further out, deterministic, fast, tolerant of messy input) and the map in Chromium (what is drawn, labels, offline interfaces, next-hop nodes, click and keyboard to details, kind filters, zoom, wheel and drag, Escape and Close, empty, interfaces-only and server-error states). |

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

### F. Stamp cost for incoming messages

Use an account on the portal and a second LXMF client (Sideband, MeshChat or NomadNet).

1. In **Admin > Settings**, set **Stamp cost** to 12 and leave "Refuse" off. Press **Announce** for the identity.
2. From the second client, send a message. The client must compute a stamp (a short delay) and the message must arrive.
3. Tick "Refuse messages that have no valid stamp" and save. Send from a client that does not support stamps. The message must not arrive.
4. Send from a client that does support stamps. The message must arrive.
5. Set the cost back to 0. Messages must arrive from every client again.

If step 2 never delays, the client has not seen the new cost. Announce again and retry.

### G. Cancel, retry and automatic re-send

Use an account on the portal and a second client that you can switch off (a phone with Sideband or MeshChat).

1. Switch the second client off. Send it a message. While the message shows "queued", press **Cancel**. The message must show "cancelled" and the client must never receive it, even after you switch it on.
2. Press **Retry** on the cancelled message. Switch the client on. It must arrive once.
3. Switch the client off again and send another message. Wait until it shows "failed" (several minutes).
4. Switch the client on and wait for it to announce. The failed message must arrive without you doing anything, and it must show the resend marker. Check that it arrived once, not twice.
5. In **Admin > Settings**, turn off "Send a failed message again". Repeat steps 3 and 4. The message must stay failed. **Retry** must still work by hand.

### H. Voice messages

Use a real microphone and a second client that plays voice messages (MeshChat is the one to check).

1. In the portal, open a chat and press the microphone button. Allow the microphone. Speak for about five seconds and press **Stop**. A `voice-message-...ogg` chip must appear. Press send.
2. In MeshChat, the message must show an audio player, and it must play your voice. (MeshChat shows "Unsupported Audio" if the field is wrong.)
3. Send a voice message from MeshChat to the portal. It must play inline in the portal. If MeshChat's quality menu has Codec2 options, send one: the portal must show a downloadable file, not a dead player.
4. Record, then press **Cancel**. The browser's recording indicator (the red dot on the tab) must go out at once.
5. Start a recording and close the tab. The microphone indicator must go out.
6. Open the portal in Firefox and Safari. The microphone button should appear only where the browser can encode Opus; if it does not appear, the paperclip must still attach an audio file.

The automated browser test cannot run the real microphone path. The headless Chromium used in CI and in development has no audio hardware, and without an audio device the browser never starts the AudioWorklet thread (its `addModule` waits forever). The test therefore replaces only the capture step with a generated tone, and runs everything after it for real. The worklet file (`call-audio-worklet.js`, shared with voice calls) and the real microphone are what this check covers.

### I. Propagation nodes

You need at least one propagation node on the network (a MeshChat or NomadNet instance running one, or a public one), and a second client you can switch off.

1. Open **Settings > Propagation**. After a few minutes of the portal running, a node must be listed, and "Automatic" must name the nearest one.
2. Choose a different node. The status must say "your choice". Choose "Automatic". It must go back.
3. Type in the hash of a node you know. It must be used even though the portal has not heard it announce.
4. From the second client, send a message to the portal user while the portal is **not** the destination reached directly (switch the portal's identity off, or send via propagation). Press **Sync now**. The progress line must show "receiving messages", then "Last collection: 1 new message". The message must appear in the chat without a reload.
5. Switch the second client off. Send it a message from the portal and wait. After the delivery attempts the message must change to **stored on a relay** (not "failed", and never "delivered").
6. Switch the second client on and make it collect from the same node (or a node peered with it). The message must arrive.
7. In **Admin > Settings**, untick "leave it with a propagation node". Repeat step 5. The message must show "failed".
8. Cancel a message while it is still "queued" and the client is off. It must stop and never arrive.

Step 6 depends on the two clients using the same node, or nodes that peer with each other. If the message never arrives, check that first.

### J. Messaging someone you have not heard from

Use a second client whose QR code (or `lxma://` link) you can copy, on a network where the portal has **not** seen that client announce (restart the portal, or use a new identity on the client and do not announce).

1. From the second client, copy the QR link, or read the address it shows.
2. In the portal, press **New Message**, paste the `lxma://` link and send a short message. It must be delivered without waiting for an announce.
3. Repeat with only the plain address (or `lxmf@...`) of an identity the portal has never heard. It must wait for the path, then fail or go to the propagation node. It must not claim success.
4. Edit one character in the middle of the key in an `lxma://` link and send. The portal must refuse it with "does not belong to this address".
5. Paste `lxmf@...` into the page address bar. A new message must open, not a page error.

### K. Network map

Run the portal on a network with at least two interfaces (for example a TCP hub and a LoRa radio) for an hour, so it has heard a mix of sites, peers and relays.

1. Open **Network** and press the map button. Every interface in **Admin > Interfaces** must appear, and an interface that is down must be drawn in red.
2. On the host, run `rnpath -t` (or `rnstatus -a`) and pick three destinations. On the map, each must sit on the same interface, at the same hop count, as `rnpath` reports.
3. Destinations that share a next hop must hang from the same diamond. Hover the diamond: the next-hop id must match `rnpath`'s "via".
4. A destination the portal has heard announce but has no path for (path expired) must appear in the grey "No route known" slice.
5. Click a destination. Its details must open on top of the map. Close them: the map must still be there.
6. Zoom, drag and tick or untick the kinds. Nothing may jump or disappear that was not asked for.
7. With 500 or more destinations, the map must open in about a second and stay smooth while zooming.

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

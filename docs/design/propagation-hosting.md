# Hosting an LXMF propagation node

Status: **in progress** (host module built; routes, panel and picker to come). Covers MeshChat parity item 8 and the
deferred NomadNet item "host a propagation node" (they are the same feature:
NomadNet and MeshChat both just switch on LXMF's `enable_propagation()`).

## What it is, and why it is worth having

A propagation node is a store-and-forward mailbox for the mesh. Senders hand
it messages for people who are offline; recipients collect them when they next
sync. Today the portal is only a *client* of other people's nodes (picker,
Sync now, fallback when delivery fails). Hosting one means:

- the portal's own users can use their own node and do not depend on a stranger's;
- the operator gives back to the mesh (the network only works because people do this);
- a kiosk deployment on a well-connected server is exactly where a node belongs.

## What LXMF gives us (verified against the installed source)

`LXMF.LXMRouter(..., propagation_limit, sync_limit, static_peers, max_peers,
from_static_only, autopeer, autopeer_maxdepth, propagation_cost,
peering_cost, name)` then `enable_propagation()`. After that the router:

- announces `lxmf.propagation` (app data carries the per-transfer limit, sync
  limit, stamp costs, node name) after a 20 s delay, and `disable_propagation()`
  announces again to say it has stopped;
- stores messages in `<storagepath>/messagestore`, indexes them at start-up,
  expires them after 30 days, and trims to `set_message_storage_limit(...)`;
- peers with other nodes (auto-discovered up to `autopeer_maxdepth` hops and
  `max_peers`, or only the static list) and offers/gets messages with them;
- exposes a control destination that only its own identity may call
  (`compile_stats()` gives peers, message counts, byte counters, uptime).

So the library does the protocol. Our work is lifecycle, settings, safety,
visibility and tests.

## Decisions

**D1. A dedicated router with its own identity, not a user's.** Users already
each have an `LXMRouter` (one per identity, `messaging.py`). A node's address is
its identity, and it must be stable and independent of whichever account
happens to be active. So: one extra router, identity file
`config/propagation/identity`, storage `config/propagation/`. Users keep sending
through the normal path; the node is not in anyone's delivery path.

**D2. Off by default; the operator turns it on in Admin.** It stores other
people's traffic and uses disk and bandwidth, so it must be a conscious choice.
Setting `propagation_node_enabled`, default false. Enabling or disabling takes
effect without a restart (`enable_propagation()` / `disable_propagation()`).

**D3. Storage on local disk, with a hard cap.** The message store is a
write-per-message hot path, so it falls under the network-filesystem rule
(see the `/config` footgun): `config/` only, never `site/`. Admin sets the cap
(default 500 MB; `set_message_storage_limit`). The page shows current use. If
the data directory is detected on a network mount, refuse to enable and say why.

**D4. Settings, each with a safe default.** Name (shown in announces); storage
cap; per-transfer limit (LXMF default 256 KB, so attachments over that are not
accepted); auto-peering on/off, `max_peers`, `autopeer_maxdepth`; optional
static peer list (and "only static peers"); propagation stamp cost (spam
brake; LXMF default 16). Defaults are LXMF's, except storage cap.

**D5. Announce.** On enable, LXMF announces by itself after 20 s. We add a slow
re-announce (every 6 h, matching what the roadmap says is normal) so the node
stays in other people's path tables, plus an Announce-now button, rate limited.
Never at start-up before the interface is up.

**D6. Admin visibility (read-only first).** A "Propagation node" panel: state,
address (copyable, with the `lxmf@` form), uptime, store size vs cap, messages
held, peers (name, last heard, sync state), bytes in and out, last errors. Fed
from `compile_stats()`, polled; no websocket.

**D7. Not part of the public site.** The kiosk guest policy stays as it is: no
guest can see or reach the node settings or stats. The node itself is reachable
only through Reticulum, by design.

**D8. (Changed) Our own users are not switched to our own node automatically.**
The first plan was to list the node as "This portal" and prefer it. That needs
a Reticulum link from the portal's router to a destination in the same
process, and nothing here proves Reticulum delivers that locally (it normally
carries traffic between instances, not to itself). A wrong guess would
silently break message delivery for every user, so it is not done. The node's
address is shown in Admin; an operator can paste it into the picker's
"enter a node by hand" box, and check **L** step 4 in the validation suite
records whether that works on a real mesh. If it does, preferring the local
node becomes a small follow-up.

**D9. One router per run; most settings apply at restart.** Found while
building: an `LXMRouter` cannot be torn down (its job thread and Reticulum
destinations live until the process exits, and a second router on the same
identity would collide). So the router is created once, on first enable;
switching the node on and off reuses it, and the storage cap changes live.
Name, per-transfer limit, peering and stamp-cost settings are saved at once and
take effect at the next portal restart; the panel says "restart required".

**Decided with the operator ("use your preference"):** 500 MB cap, static peers
only until auto-peering is turned on, LXMF's default stamp cost of 16.

## What it must not do

- Start without the operator asking, or survive a config where the cap is unset.
- Run on a network filesystem (D3).
- Expose the control destination to anyone but its own identity (LXMF already
  enforces; a test pins that we never widen the allowed list).
- Let a failing node take down the portal. Enabling is wrapped: on any error
  the node is off, the portal carries on, and Admin shows the error.
- Store or log message contents. Counts and sizes only.

## Plan, in order, each with tests before code

1. `nomadnet_web/propagation_host.py`: a small module that owns the router's
   lifecycle (`enable/disable/status/announce`) with `LXMF` injected so tests
   use a fake. Pure decisions (settings to constructor arguments, network-mount
   check, stats to a display shape) are plain functions.
2. Settings and Admin routes (`/api/admin/propagation`: get, set, announce).
3. Admin panel, plus browser-suite coverage (new suite 15).
4. ~~Picker integration (D8)~~ deferred until check L step 4 shows loopback works.
5. Docs: README, CHANGELOG, `docs/` and a manual check **L** in
   `validation-suite.md` (two portals peer with each other; a message sent to
   an offline user through node A is collected from node B; a stranger's
   MeshChat uses the node).

What stays unverifiable here (no mesh in the sandbox): real peering, real
message hand-off, bandwidth cost. Those are check L.

## Open questions for the operator (answered above)

1. Default storage cap: 500 MB, or smaller for a small VPS?
2. Auto-peer by default, or static peers only until the operator opts in?
   (Auto-peering makes the node a good citizen immediately but pulls in other
   people's traffic up to the cap.)
3. Should the node require a propagation stamp cost above LXMF's 16 on a public
   kiosk host, to blunt spam?

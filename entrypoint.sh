#!/usr/bin/env bash
set -euo pipefail

# Ensure site directories exist (created here so the volume populates correctly
# on first run even before the user adds content)
SITE_PAGES_DIR="${SITE_PAGES_DIR:-/site/pages}"
SITE_FILES_DIR="${SITE_FILES_DIR:-/site/files}"
SITE_LIB_DIR="${SITE_LIB_DIR:-/site/lib}"
SITE_DATA_DIR="${SITE_DATA_DIR:-/site/data}"
SITE_REQS="${SITE_REQS:-/site/requirements.txt}"
mkdir -p "$SITE_PAGES_DIR" "$SITE_FILES_DIR" "$SITE_LIB_DIR" "$SITE_DATA_DIR"

# Install user-specified Python packages into the persistent site/lib/ dir
# so executable .mu pages can import them. Survives restarts because /site
# is the bind-mounted volume.
if [ -f "$SITE_REQS" ]; then
  echo "[site] Installing packages from $SITE_REQS into $SITE_LIB_DIR..."
  pip install --quiet --target "$SITE_LIB_DIR" -r "$SITE_REQS" \
    || echo "[site] WARNING: pip install reported errors — check $SITE_LIB_DIR"
fi

# Make site/lib/ importable in every child process (notably the subprocesses
# gunicorn spawns to run executable .mu pages).
export PYTHONPATH="${SITE_LIB_DIR}${PYTHONPATH:+:$PYTHONPATH}"

# First-run only: seed the default landing page from the bundled template.
# A marker file in /config/ records that we've seeded once, so a user who
# deletes index.mu won't have it regenerated on every restart.
SITE_SEED_MARKER="/config/.site-seeded"
DEFAULT_INDEX="/app/templates/site/index.mu"
if [ ! -f "$SITE_SEED_MARKER" ]; then
  if [ -f "$DEFAULT_INDEX" ] && [ ! -e "$SITE_PAGES_DIR/index.mu" ]; then
    cp "$DEFAULT_INDEX" "$SITE_PAGES_DIR/index.mu"
    chmod +x "$SITE_PAGES_DIR/index.mu"
    echo "[site] Seeded $SITE_PAGES_DIR/index.mu from $DEFAULT_INDEX"
  fi
  mkdir -p "$(dirname "$SITE_SEED_MARKER")"
  touch "$SITE_SEED_MARKER"
fi


# MTU sanity check. When this container's primary interface uses the
# default 1500 MTU but the outbound network path (a VPN, an upstream
# tunnel) has a smaller MTU, TCP connections to Reticulum hubs will
# establish, work briefly, then fail with `Connection reset by peer`
# after ~24-40 s once large enough packets get dropped by path-MTU-
# discovery blackholes. Symptoms surface as "cached link failed", "No
# response from node", "Link closed before response", "path discovery
# timed out" — all downstream of TCP itself dying silently. Reticulum
# sets aggressive TCP keepalives (5 s idle probe, 24 s TCP_USER_TIMEOUT)
# so the failure is fast and looks like the destination is being
# unresponsive when actually our packets aren't making it out.
#
# We can't autodetect the true PMTU here without hitting a hub, but
# we can flag the setup that almost always causes this: an eth0 with
# the default 1500 MTU inside a container. If your deployment is
# entirely on a local mesh without any VPN, the warning is harmless
# and you can silence it with NOMADPORTAL_SKIP_MTU_WARNING=true.
#
# The fix is a docker-compose change, not a NomadPortal code change:
# either set the docker network MTU to match your VPN's (typical
# WireGuard is 1280, OpenVPN varies), or share a VPN container's
# network namespace directly via network_mode: "service:gluetun".
if [ "${NOMADPORTAL_SKIP_MTU_WARNING:-false}" != "true" ]; then
  # Two failure shapes we want to catch:
  #
  # 1) Container is on a default docker bridge (eth0 MTU 1500) with
  #    NO tunnel interface visible in the namespace, but a lower-MTU
  #    VPN upstream on the host. TCP inside the container has no
  #    signal that the effective path MTU is smaller, so packets
  #    fragment or blackhole at the VPN boundary. Fix: docker network
  #    MTU or network_mode:container:<gluetun>.
  #
  # 2) Container shares a Gluetun-like VPN namespace (tun0/wg0
  #    present with sub-1400 MTU) but the sockets still see eth0 as
  #    1500. Even with MSS clamping in Gluetun, the very low tun
  #    MTUs some providers use (~1170-1280) cause enough overhead
  #    that Reticulum's default TCP hardware MTU (8192) generates
  #    payloads that don't survive. Fix: set fixed_mtu ~1000 on the
  #    tcp_clients entry in config.yml so RNS produces small enough
  #    chunks.
  MIN_TUNNEL_MTU=""
  MIN_TUNNEL_IF=""
  MAX_NON_TUNNEL_MTU=0
  MAX_NON_TUNNEL_IF=""
  for candidate in /sys/class/net/*; do
    [ -e "$candidate/mtu" ] || continue
    name="$(basename "$candidate")"
    case "$name" in
      lo|lo0) continue ;;
    esac
    m="$(cat "$candidate/mtu" 2>/dev/null || echo "")"
    [ -z "$m" ] && continue
    case "$name" in
      tun*|tap*|wg*|utun*)
        # Track the smallest tunnel MTU.
        if [ -z "$MIN_TUNNEL_MTU" ] || [ "$m" -lt "$MIN_TUNNEL_MTU" ]; then
          MIN_TUNNEL_MTU="$m"
          MIN_TUNNEL_IF="$name"
        fi
        ;;
      *)
        # Track the largest non-tunnel MTU.
        if [ "$m" -gt "$MAX_NON_TUNNEL_MTU" ]; then
          MAX_NON_TUNNEL_MTU="$m"
          MAX_NON_TUNNEL_IF="$name"
        fi
        ;;
    esac
  done

  if [ -n "$MIN_TUNNEL_MTU" ] && [ "$MIN_TUNNEL_MTU" -lt 1400 ]; then
    # Shape 2: tunnel present in namespace, MTU low enough that we
    # need to constrain RNS explicitly.
    echo "[startup] ================================================================" >&2
    echo "[startup] Reticulum TCP interface config note:" >&2
    echo "[startup]" >&2
    echo "[startup] A tunnel interface ($MIN_TUNNEL_IF, MTU $MIN_TUNNEL_MTU) is present in this" >&2
    echo "[startup] container's network namespace — likely a VPN routing outbound" >&2
    echo "[startup] traffic. When the tunnel MTU is below ~1400, RNS's default TCP" >&2
    echo "[startup] hardware MTU (8192) produces payloads that get fragmented or" >&2
    echo "[startup] silently dropped at the tunnel boundary, causing 'Link closed" >&2
    echo "[startup] before response' / 'No response from node' failures within" >&2
    echo "[startup] ~30-60s of Reticulum session start." >&2
    echo "[startup]" >&2
    echo "[startup] Fix: add fixed_mtu: 1000 to each tcp_clients entry in config.yml" >&2
    echo "[startup] (safe value under ~1200 tunnel MTUs). Example:" >&2
    echo "[startup]" >&2
    echo "[startup]   interfaces:" >&2
    echo "[startup]     tcp_clients:" >&2
    echo "[startup]       - name: MichMesh" >&2
    echo "[startup]         host: rns.michmesh.net" >&2
    echo "[startup]         port: 7822" >&2
    echo "[startup]         enabled: true" >&2
    echo "[startup]         fixed_mtu: 1000" >&2
    echo "[startup]" >&2
    echo "[startup] Silence: NOMADPORTAL_SKIP_MTU_WARNING=true" >&2
    echo "[startup] ================================================================" >&2
  elif [ -z "$MIN_TUNNEL_MTU" ] && [ -n "$MAX_NON_TUNNEL_IF" ] && [ "$MAX_NON_TUNNEL_MTU" -ge 1500 ]; then
    # Shape 1: no tunnel visible, non-tunnel primary at MTU 1500.
    # If a VPN sits upstream at the host or network level, we're
    # heading into a PMTU blackhole. Compact warning — the docker-
    # network-MTU / network_mode fix is in the README.
    echo "[startup] ================================================================" >&2
    echo "[startup] MTU note: primary interface $MAX_NON_TUNNEL_IF is at MTU $MAX_NON_TUNNEL_MTU," >&2
    echo "[startup] no tunnel interface visible in this namespace. If a VPN sits" >&2
    echo "[startup] upstream (Gluetun, host VPN, Tailscale exit node, …), TCP to" >&2
    echo "[startup] Reticulum hubs may silently blackhole after ~30-60s." >&2
    echo "[startup] Fix: docker network driver_opts.mtu, or network_mode:" >&2
    echo "[startup] container:<gluetun_container_name> to share a VPN namespace." >&2
    echo "[startup] See README 'Running behind a VPN'. Silence:" >&2
    echo "[startup] NOMADPORTAL_SKIP_MTU_WARNING=true" >&2
    echo "[startup] ================================================================" >&2
  fi
fi


# Prune old ratchet files before RNS starts. RNS keeps per-peer
# forward-secrecy ratchets under $RNS_CONFIG_DIR/storage/ratchets/,
# one small file each. On long-running installs this accumulates —
# 15K+ files was observed to cause ~10 min startups because RNS
# reads them one at a time during Reticulum() init. Ratchets are
# regenerated from live traffic, so pruning old ones costs at most
# a one-time re-establish per still-active peer we hear from again;
# genuinely stale peers just stay stale.
#
# Tunable via NOMADPORTAL_RATCHET_MAX_AGE_DAYS (default 30). Set to
# 0 to disable pruning entirely.
RATCHET_MAX_AGE_DAYS="${NOMADPORTAL_RATCHET_MAX_AGE_DAYS:-30}"
RATCHET_MAX_COUNT="${NOMADPORTAL_RATCHET_MAX_COUNT:-5000}"
RATCHET_DIR="${RNS_CONFIG_DIR:-/config/reticulum}/storage/ratchets"
if [ -d "$RATCHET_DIR" ]; then
  count_before=$(find "$RATCHET_DIR" -type f 2>/dev/null | wc -l)

  # Fast path: nothing to do — no files, or already at/below the cap
  # AND age pruning is disabled or would find nothing. Skipping the
  # noisy log lines keeps a fast restart from spamming duplicate
  # "prune" messages when there's genuinely no work.
  age_prune_active=0
  if [ "$RATCHET_MAX_AGE_DAYS" -gt 0 ] && [ "$count_before" -gt 0 ]; then
    # Peek — is there anything older than the age threshold?
    # ``find -print -quit`` (GNU find) exits after emitting the first
    # match, so we don't pipe an unbounded stream into ``head -n 1``.
    # That pattern was the SIGPIPE trap: on a large ratchet directory
    # (16k+ files observed on the mirror), ``head`` reads its one line
    # and closes, ``find`` gets SIGPIPE writing line 2, ``set -o
    # pipefail`` propagates 141, ``set -e`` exits the outer shell —
    # and the container crash-loops with exit 141 right before the
    # SSL / gunicorn setup, giving no log line to explain it.
    aged_candidate=$(find "$RATCHET_DIR" -type f -mtime "+${RATCHET_MAX_AGE_DAYS}" -print -quit 2>/dev/null)
    [ -n "$aged_candidate" ] && age_prune_active=1
  fi
  count_over_cap=0
  if [ "$RATCHET_MAX_COUNT" -gt 0 ] && [ "$count_before" -gt "$RATCHET_MAX_COUNT" ]; then
    count_over_cap=1
  fi

  if [ "$age_prune_active" -eq 1 ] || [ "$count_over_cap" -eq 1 ]; then
    echo "[startup] Pruning ratchets (currently ${count_before} files, cap ${RATCHET_MAX_COUNT}, age ${RATCHET_MAX_AGE_DAYS}d)..."

    # Step 1: age-based prune. Does the right thing on filesystems /
    # RNS versions where ratchet mtime reflects actual last-use time.
    if [ "$age_prune_active" -eq 1 ]; then
      find "$RATCHET_DIR" -type f -mtime "+${RATCHET_MAX_AGE_DAYS}" -delete 2>/dev/null || true
      count_after_age=$(find "$RATCHET_DIR" -type f 2>/dev/null | wc -l)
      aged_out=$((count_before - count_after_age))
      echo "[startup] Age-based prune removed ${aged_out}; ${count_after_age} remaining"
    else
      count_after_age="$count_before"
    fi

    # Step 2: hard count cap. Handles the common case where mtime is
    # useless because RNS bulk-updates it — keep only the newest N
    # files by mtime, delete the rest. Ratchets regenerate from live
    # traffic (one extra round-trip on first link with an affected peer).
    if [ "$RATCHET_MAX_COUNT" -gt 0 ] && [ "$count_after_age" -gt "$RATCHET_MAX_COUNT" ]; then
      to_delete=$((count_after_age - RATCHET_MAX_COUNT))
      # ``head -n "$to_delete"`` closes the pipe after N lines and
      # ``sort`` gets SIGPIPE on the (N+1)th line — same class of
      # SIGPIPE-under-pipefail trap as the peek pipeline above.
      # ``|| true`` absorbs the failing pipeline's exit code; the
      # deletion side-effect for the first N lines has already
      # happened via xargs by the time head closes.
      find "$RATCHET_DIR" -type f -printf '%T@ %p\n' 2>/dev/null \
        | sort -n \
        | head -n "$to_delete" \
        | cut -d' ' -f2- \
        | xargs -r rm -f \
        || true
      count_final=$(find "$RATCHET_DIR" -type f 2>/dev/null | wc -l)
      echo "[startup] Count-based prune removed ${to_delete}; ${count_final} remaining"
    fi
  fi
fi

HTTPS_PORT="${WEB_PORT_HTTPS:-}"
HTTP_PORT="${WEB_PORT:-}"

# Port-based deployment mode — no separate TLS flag, the choice is implicit
# in which ports you set:
#
#   WEB_PORT_HTTPS only       → TLS on that port. Self-signed cert generated
#                                if /config/ssl/cert.pem doesn't exist.
#   WEB_PORT only             → plain HTTP on that port. No cert, no redirect.
#                                Use this behind a reverse proxy that does TLS.
#   Both set                  → TLS on WEB_PORT_HTTPS plus an HTTP→HTTPS
#                                redirector on WEB_PORT (the classic setup).
#   Neither set               → falls back to defaults (HTTPS on 8443 +
#                                redirector on 8080) so existing deployments
#                                keep working.
if [ -z "$HTTPS_PORT" ] && [ -z "$HTTP_PORT" ]; then
  HTTPS_PORT="8443"
  HTTP_PORT="8080"
fi

if [ -n "$HTTPS_PORT" ]; then
  SSL_DIR=/config/ssl
  CERT="$SSL_DIR/cert.pem"
  KEY="$SSL_DIR/key.pem"

  mkdir -p "$SSL_DIR"

  if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    echo "[ssl] Generating self-signed certificate..."
    openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout "$KEY" -out "$CERT" \
      -days 3650 \
      -subj "/CN=nomadportal" \
      -addext "subjectAltName=IP:127.0.0.1,DNS:localhost" \
      2>/dev/null
    echo "[ssl] Certificate written to $CERT"
  fi

  if [ -n "$HTTP_PORT" ]; then
    echo "[tls] HTTPS on ${HTTPS_PORT}, HTTP→HTTPS redirector on ${HTTP_PORT}."
    python3 /app/redirect_http.py &
  else
    echo "[tls] HTTPS only on ${HTTPS_PORT} (no redirector — WEB_PORT not set)."
  fi

  exec gunicorn \
    --workers 1 \
    --threads 8 \
    --timeout 120 \
    --access-logfile - \
    --bind "0.0.0.0:${HTTPS_PORT}" \
    --certfile "$CERT" \
    --keyfile  "$KEY" \
    "app:create_wsgi()"
else
  echo "[tls] Plain HTTP on ${HTTP_PORT} (WEB_PORT_HTTPS not set — no in-container TLS)."
  echo "[tls] Put a reverse proxy with a real certificate in front if exposing publicly."

  exec gunicorn \
    --workers 1 \
    --threads 8 \
    --timeout 120 \
    --access-logfile - \
    --bind "0.0.0.0:${HTTP_PORT}" \
    "app:create_wsgi()"
fi

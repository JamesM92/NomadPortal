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

  # Step 1: age-based prune. This does the right thing on
  # filesystems / RNS versions where ratchet mtime reflects the
  # actual last-use time. In practice we've observed RNS bulk-
  # rewrites all ratchet mtimes on load, which makes this step a
  # no-op; the count-based step below still handles that case.
  if [ "$RATCHET_MAX_AGE_DAYS" -gt 0 ] && [ "$count_before" -gt 0 ]; then
    echo "[startup] Pruning ratchets older than ${RATCHET_MAX_AGE_DAYS}d (currently ${count_before} files)..."
    find "$RATCHET_DIR" -type f -mtime "+${RATCHET_MAX_AGE_DAYS}" -delete 2>/dev/null || true
    count_after_age=$(find "$RATCHET_DIR" -type f 2>/dev/null | wc -l)
    aged_out=$((count_before - count_after_age))
    if [ "$aged_out" -gt 0 ]; then
      echo "[startup] Age-based prune removed ${aged_out}; ${count_after_age} remaining"
    else
      echo "[startup] No ratchets older than ${RATCHET_MAX_AGE_DAYS}d by mtime"
    fi
  else
    count_after_age="$count_before"
  fi

  # Step 2: hard count cap. Keep only the newest N files (by mtime,
  # since that's what we have). This handles the common case where
  # mtime is useless because RNS bulk-updates it — we can still get
  # startup back under control by keeping a reasonable number of the
  # most-recently-touched entries. Ratchets we delete are regenerated
  # from live traffic on demand (one extra round-trip for the first
  # link with an affected peer).
  if [ "$RATCHET_MAX_COUNT" -gt 0 ] && [ "$count_after_age" -gt "$RATCHET_MAX_COUNT" ]; then
    to_delete=$((count_after_age - RATCHET_MAX_COUNT))
    echo "[startup] Ratchet count ${count_after_age} > cap ${RATCHET_MAX_COUNT}; deleting oldest ${to_delete}..."
    find "$RATCHET_DIR" -type f -printf '%T@ %p\n' 2>/dev/null \
      | sort -n \
      | head -n "$to_delete" \
      | cut -d' ' -f2- \
      | xargs -r rm -f
    count_final=$(find "$RATCHET_DIR" -type f 2>/dev/null | wc -l)
    echo "[startup] Ratchets after count-based prune: ${count_final}"
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

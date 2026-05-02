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

HTTPS_PORT="${WEB_PORT_HTTPS:-8443}"
HTTP_PORT="${WEB_PORT:-8080}"

# TLS_ENABLED toggles the in-container HTTPS stack:
#   true (default) — generate a self-signed cert if missing, run gunicorn
#                    with --certfile/--keyfile on $HTTPS_PORT, run the
#                    HTTP→HTTPS redirector on $HTTP_PORT.
#   false          — no cert generation, no redirect, gunicorn binds
#                    plain HTTP on $HTTPS_PORT. Use this when a reverse
#                    proxy (nginx, Caddy, Traefik, NPM) terminates TLS
#                    for you and forwards plain HTTP to the container.
TLS_ENABLED="${TLS_ENABLED:-true}"
case "${TLS_ENABLED,,}" in
  true|1|yes) TLS_ENABLED="true"  ;;
  *)          TLS_ENABLED="false" ;;
esac

if [ "$TLS_ENABLED" = "true" ]; then
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

  # HTTP → HTTPS redirect in background
  python3 /app/redirect_http.py &

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
  echo "[tls] TLS_ENABLED=false — binding plain HTTP on ${HTTPS_PORT}, no in-container TLS."
  echo "[tls] If you exposed this on a public network, put a reverse proxy with a real certificate in front."

  exec gunicorn \
    --workers 1 \
    --threads 8 \
    --timeout 120 \
    --access-logfile - \
    --bind "0.0.0.0:${HTTPS_PORT}" \
    "app:create_wsgi()"
fi

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

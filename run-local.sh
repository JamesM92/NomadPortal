#!/usr/bin/env bash
# Bring up NomadPortal natively (no Docker) for local development/testing.
#
# Reuses ./local-config (a copy of the production /config volume) so the
# hosted-site identity, nodes.json, message store, etc. survive across
# runs. First-time setup auto-creates the venv + installs deps.
#
# Usage:
#   ./run-local.sh                 — start with defaults (127.0.0.1:8080)
#   PORT=9000 ./run-local.sh       — override port
#   FRESH_CONFIG=1 ./run-local.sh  — start from an empty config dir
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

PORT="${PORT:-8080}"
HOST="${HOST:-127.0.0.1}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-local-test-only}"

# Confirm we're actually in the NomadPortal repo
if [ ! -f app.py ] || [ ! -d nomadnet_web ]; then
  echo "[error] $(pwd) doesn't look like the NomadPortal repo" >&2
  exit 1
fi

# Refuse to run on top of the host's port-8080 Docker container so the
# user knows which process they're hitting in the browser.
if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q "LISTEN" \
   || lsof -iTCP:"$PORT" -sTCP:LISTEN -P 2>/dev/null | grep -q LISTEN; then
  echo "[error] Port $PORT is already in use. Stop the conflicting process"
  echo "        or set PORT=<other> before re-running." >&2
  echo
  echo "        Likely culprits:"
  echo "          docker ps --filter publish=$PORT"
  echo "          pgrep -af 'gunicorn.*:$PORT'"
  exit 1
fi

# venv: create on first run, then activate
if [ ! -d .venv ]; then
  echo "[setup] Creating virtualenv at .venv ..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Install/refresh deps — skip if requirements.txt unchanged since last install
DEPS_STAMP=".venv/.deps-stamp"
if [ ! -f "$DEPS_STAMP" ] || [ requirements.txt -nt "$DEPS_STAMP" ]; then
  echo "[setup] Installing/updating dependencies ..."
  pip install --quiet -r requirements.txt
  touch "$DEPS_STAMP"
fi

# Local config:
#   - FRESH_CONFIG=1                            wipes ./local-config
#   - DOCKER_VOL=/path/to/docker/volume/_data    seeds ./local-config from an
#                                                existing Docker deployment so
#                                                the hosted-site identity, node
#                                                cache, etc. carry over
#   - (neither set)                              starts with empty config; RNS
#                                                will generate a fresh identity
if [ "${FRESH_CONFIG:-0}" = "1" ]; then
  echo "[setup] FRESH_CONFIG=1 — wiping ./local-config"
  rm -rf ./local-config
fi
if [ ! -d ./local-config ]; then
  DOCKER_VOL="${DOCKER_VOL:-}"
  if [ -n "$DOCKER_VOL" ] && [ -d "$DOCKER_VOL" ]; then
    echo "[setup] Copying $DOCKER_VOL → ./local-config (needs sudo) ..."
    sudo cp -a "$DOCKER_VOL" ./local-config
    sudo chown -R "$USER:$USER" ./local-config
  else
    if [ -n "$DOCKER_VOL" ]; then
      echo "[setup] DOCKER_VOL=$DOCKER_VOL does not exist — starting fresh"
    fi
    mkdir -p ./local-config/reticulum
  fi
fi
mkdir -p ./local-site/pages ./local-site/files

# Seed the default index page on first run — mirrors the Docker entrypoint.
# A marker in local-config records the seed so deleting index.mu later
# doesn't trigger re-creation.
SEED_MARKER="./local-config/.site-seeded"
DEFAULT_INDEX="templates/site/index.mu"
if [ ! -f "$SEED_MARKER" ] && [ -f "$DEFAULT_INDEX" ] && [ ! -e "./local-site/pages/index.mu" ]; then
  cp "$DEFAULT_INDEX" "./local-site/pages/index.mu"
  chmod +x "./local-site/pages/index.mu"
  echo "[setup] Seeded ./local-site/pages/index.mu from $DEFAULT_INDEX"
  touch "$SEED_MARKER"
fi

# Paths NomadPortal reads at startup
export RNS_CONFIG_DIR="$(pwd)/local-config/reticulum"
export NOMADNET_CONFIG="$(pwd)/local-config/nomadnetwork"
export CONFIG_YML="$(pwd)/local-config/config.yml"
export USERS_YML="$(pwd)/local-config/users.yml"
export SITE_PAGES_DIR="$(pwd)/local-site/pages"
export SITE_FILES_DIR="$(pwd)/local-site/files"

# Web server config
export WEB_HOST="$HOST"
export WEB_PORT="$PORT"
export ADMIN_USERNAME="$ADMIN_USER"
export ADMIN_PASSWORD="$ADMIN_PASS"
export LOG_LEVEL="${LOG_LEVEL:-DEBUG}"

cat <<EOF

  NomadPortal local dev
  ─────────────────────
  URL:        http://$HOST:$PORT/
  Admin:      $ADMIN_USER / $ADMIN_PASS
  Config:     $(pwd)/local-config
  Site root:  $(pwd)/local-site
  Log level:  $LOG_LEVEL

  Ctrl+C to stop.

EOF

exec gunicorn \
  --workers 1 \
  --threads 8 \
  --timeout 120 \
  --access-logfile - \
  --bind "$HOST:$PORT" \
  'app:create_wsgi()'

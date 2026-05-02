#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# ── Colours ────────────────────────────────────────────────────────────────
GRN='\033[0;32m'; YLW='\033[0;33m'; RED='\033[0;31m'; CYN='\033[0;36m'; RST='\033[0m'
info()  { echo -e "${CYN}[info]${RST}  $*"; }
ok()    { echo -e "${GRN}[ok]${RST}    $*"; }
warn()  { echo -e "${YLW}[warn]${RST}  $*"; }
die()   { echo -e "${RED}[err]${RST}   $*" >&2; exit 1; }

echo
echo -e "${CYN}  ■ NomadPortal${RST}"
echo    "  ─────────────────"
echo

# ── Pre-flight checks ──────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "Docker is not installed or not in PATH."

if ! docker_err="$(docker info 2>&1 >/dev/null)"; then
  # Distinguish "permission denied" (daemon up, user not in docker group) from
  # "daemon not running". Misdiagnosing the first as the second leads to
  # `sudo systemctl start docker` looping on an already-running daemon.
  if echo "$docker_err" | grep -qiE "permission denied|connect:.*permission"; then
    warn "Docker daemon is running but your user lacks permission to talk to it."
    warn "Fix: sudo usermod -aG docker $USER && newgrp docker"
    warn "(or log out and back in after the usermod). Then re-run this script."
    die  "Permission denied on Docker socket."
  fi

  daemon_active=0
  if command -v systemctl >/dev/null 2>&1 \
     && systemctl list-unit-files docker.service >/dev/null 2>&1; then
    if systemctl is-active --quiet docker.service; then
      daemon_active=1
    fi
  fi

  if [ "$daemon_active" -eq 1 ]; then
    # Daemon claims to be active but `docker info` still failed — surface the real error.
    warn "Docker reports: $docker_err"
    die  "Docker daemon is active but unreachable. Check 'docker info' output above."
  fi

  if command -v systemctl >/dev/null 2>&1 \
     && systemctl list-unit-files docker.service >/dev/null 2>&1; then
    info "Docker daemon not running — starting via systemctl (may prompt for sudo)…"
    sudo systemctl start docker || die "Failed to start docker.service."
    for i in $(seq 1 15); do
      docker info >/dev/null 2>&1 && break
      sleep 1
    done
    docker info >/dev/null 2>&1 || die "Docker daemon did not become ready after start."
    ok "Docker daemon started."
  else
    die "Docker daemon is not running and no systemctl docker.service was found."
  fi
fi

docker compose version >/dev/null 2>&1 || die "docker compose (v2) not found."

# ── First-run: copy example config ────────────────────────────────────────
if [ ! -f config/config.yml ] && [ -f config/config.yml.example ]; then
  cp config/config.yml.example config/config.yml
  warn "config/config.yml not found — seeded from config/config.yml.example."
  warn "All interfaces start disabled; enable the ones you need from Admin → Interfaces."
  echo
fi

# ── Ownership: the container runs as UID 1000 ────────────────────────────
# When start.sh is invoked under sudo, files created above are owned by
# root, and the container can't write them — admin saves return 500.
# Normalize ownership of host-mounted dirs so UID 1000 in the container
# can read and write. Silently ignored if we lack permission to chown
# (e.g. running as a regular non-root, non-1000 user).
chown -R 1000:1000 config site 2>/dev/null || true

# ── Warn about default password ───────────────────────────────────────────
if grep -qE 'ADMIN_PASSWORD:\s*(changeme|admin|password)' docker-compose.yml 2>/dev/null; then
  warn "ADMIN_PASSWORD is set to a default value in docker-compose.yml."
  warn "Change it before exposing this service to a network."
  echo
fi

# ── Parse arguments ────────────────────────────────────────────────────────
REBUILD=0
DETACH=1
for arg in "$@"; do
  case "$arg" in
    --build|-b)   REBUILD=1 ;;
    --fg|--foreground) DETACH=0 ;;
    --help|-h)
      echo "Usage: $0 [--build] [--fg]"
      echo "  --build  Force rebuild of the Docker image"
      echo "  --fg     Run in foreground (stream logs to terminal)"
      exit 0 ;;
    *) warn "Unknown argument: $arg" ;;
  esac
done

# ── Build / start ──────────────────────────────────────────────────────────
BUILD_FLAG=""
[ "$REBUILD" -eq 1 ] && BUILD_FLAG="--build"

DETACH_FLAG="-d"
[ "$DETACH" -eq 0 ] && DETACH_FLAG=""

info "Starting NomadPortal…"
docker compose up $BUILD_FLAG $DETACH_FLAG

# ── Post-start info (only shown in detached mode) ──────────────────────────
if [ "$DETACH" -eq 1 ]; then
  # Wait briefly for the container to settle
  sleep 2

  HTTPS_PORT=$(grep 'WEB_PORT_HTTPS' docker-compose.yml 2>/dev/null \
    | grep -oE '[0-9]+' | head -1 || echo 8443)

  echo
  ok "Service started."
  echo
  echo -e "  Browser   ${GRN}https://localhost:${HTTPS_PORT}${RST}  ${YLW}(self-signed cert — accept the browser warning)${RST}"
  echo -e "  Admin     ${GRN}https://localhost:${HTTPS_PORT}/admin${RST}"
  echo -e "  HTTP      redirects automatically to HTTPS"
  echo
  echo -e "  Logs      ${CYN}docker compose logs -f${RST}"
  echo -e "  Stop      ${CYN}docker compose down${RST}"
  echo -e "  Restart   ${CYN}docker compose restart${RST}"
  echo -e "  NomadNet  ${CYN}docker exec -it nomadportal nomadnet-tui${RST}"
  echo
fi

FROM python:3.14-slim-trixie@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

LABEL org.opencontainers.image.title="NomadPortal"
LABEL org.opencontainers.image.description="Web browser for NomadNet nodes with LXMF messaging"
LABEL org.opencontainers.image.source="https://github.com/JamesM92/NomadPortal"

# System dependencies for Reticulum (cryptography / serial transports).
# `apt-get upgrade` pulls in Debian's own security-repo patches for
# packages already baked into the base image (util-linux and friends —
# CVE-2026-53612/-53613/-53614/-53615) that the next scheduled
# python:3.14-slim-trixie rebuild hasn't picked up yet. Bumping the
# pinned base-image digest alone isn't enough while that gap exists;
# this is the standard "don't just trust the base image is fresh"
# Docker hardening step, and belongs here regardless of digest churn.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
      gcc \
      libssl-dev \
      openssl \
    && rm -rf /var/lib/apt/lists/*

# python:3.12-slim only ships the interpreter at /usr/local/bin/python3.
# User-authored .mu pages conventionally start with `#!/usr/bin/python3`,
# so add the canonical /usr/bin path as a symlink for compatibility.
RUN ln -sf /usr/local/bin/python3 /usr/bin/python3

# Non-root user — matches UID 1000 so host volume permissions align with
# `chown -R 1000:1000 ./config` on the host side.
RUN groupadd -r -g 1000 nomadnet \
 && useradd -r -u 1000 -g nomadnet -s /sbin/nologin -d /app nomadnet

WORKDIR /app

# Install Python dependencies first (layer-cached).
# pip/setuptools/wheel upgraded explicitly first — the base image's
# bundled setuptools (70.3.0) carries CVE-2025-47273 (path traversal);
# ensurepip only installs it once at base-image build time and doesn't
# track security fixes, so it has to be bumped here.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

# Copy application source (Micron2HTML is installed via pip, not bundled)
COPY nomadnet_web/     ./nomadnet_web/
COPY templates/        ./templates/
COPY static/           ./static/
COPY app.py            .
COPY redirect_http.py  .
COPY entrypoint.sh     .
COPY LICENSE           .

# Convenience wrapper: `docker exec -it nomadportal nomadnet-tui`
RUN printf '#!/bin/sh\nexec nomadnet --config /config/nomadnetwork "$@"\n' \
    > /usr/local/bin/nomadnet-tui \
    && chmod +x /usr/local/bin/nomadnet-tui

RUN chown -R nomadnet:nomadnet /app

# Pre-create the volume mount points as nomadnet-owned dirs so anonymous
# docker volumes (e.g. in CI, or `docker run` without an explicit -v)
# inherit the correct ownership. Without this, docker creates the paths
# owned by root and the entrypoint's `mkdir -p /site/pages` fails —
# gunicorn never starts.
#
# Operators bind-mounting from the host still need to `chown -R 1000:1000`
# their host dirs (documented below); this only fixes the no-mount case.
RUN mkdir -p /config /site \
 && chown -R nomadnet:nomadnet /config /site

# /config is volume-mounted and must be writable by UID 1000.
# On the host run: chown -R 1000:1000 ./config
VOLUME ["/config"]
VOLUME ["/site"]

EXPOSE 8080 8443

ENV RNS_CONFIG_DIR=/config/reticulum \
    NOMADNET_CONFIG=/config/nomadnetwork \
    CONFIG_YML=/config/config.yml \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    WEB_PORT_HTTPS=8443 \
    CACHE_TTL=300 \
    LOG_LEVEL=DEBUG

# Healthcheck hits /healthz, which returns 503 if RNS has no online
# interfaces (different from /api/status, which only confirms gunicorn is
# listening). start-period is generous because RNS state replay can take
# 60-300s when destination_table has accumulated months of paths — real
# deployments have been observed at ~3:30 with 1700+ known nodes and
# 28K LXMF peers. 300s covers the observed distribution comfortably;
# containers that are still warming past that mark start reporting
# unhealthy, which is the correct signal at that point.
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
  CMD python -c "import os, urllib.request, ssl; https=(os.environ.get('WEB_PORT_HTTPS') or '').strip(); http=(os.environ.get('WEB_PORT') or '').strip(); port=https or http or '8443'; tls=bool(https) or (not http); ctx=(ssl._create_unverified_context() if tls else None); urllib.request.urlopen(f'{\"https\" if tls else \"http\"}://localhost:{port}/healthz', context=ctx, timeout=4)"

USER nomadnet

CMD ["/app/entrypoint.sh"]

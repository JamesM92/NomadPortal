FROM python:3.14-slim-trixie@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1

LABEL org.opencontainers.image.title="NomadPortal"
LABEL org.opencontainers.image.description="Web browser for NomadNet nodes with LXMF messaging"
LABEL org.opencontainers.image.source="https://github.com/JamesM92/NomadPortal"

# System dependencies for Reticulum (cryptography / serial transports).
RUN apt-get update && apt-get install -y --no-install-recommends \
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

# Install Python dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
    LOG_LEVEL=INFO

# Healthcheck hits /healthz, which returns 503 if RNS has no online
# interfaces (different from /api/status, which only confirms gunicorn is
# listening). start-period is generous because RNS state replay can take
# 60+ s when destination_table has accumulated months of paths.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os, urllib.request, ssl; https=(os.environ.get('WEB_PORT_HTTPS') or '').strip(); http=(os.environ.get('WEB_PORT') or '').strip(); port=https or http or '8443'; tls=bool(https) or (not http); ctx=(ssl._create_unverified_context() if tls else None); urllib.request.urlopen(f'{\"https\" if tls else \"http\"}://localhost:{port}/healthz', context=ctx, timeout=4)"

USER nomadnet

CMD ["/app/entrypoint.sh"]

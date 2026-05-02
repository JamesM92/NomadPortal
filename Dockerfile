FROM python:3.12-slim@sha256:46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3

LABEL org.opencontainers.image.title="NomadPortal"
LABEL org.opencontainers.image.description="Web browser for NomadNet nodes with LXMF messaging"
LABEL org.opencontainers.image.source="https://github.com/JamesM92/NomadPortal"

# System dependencies for Reticulum (cryptography / serial transports).
# git is needed for pip to install Micron2HTML from its GitHub tag.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc \
      git \
      libssl-dev \
      openssl \
    && rm -rf /var/lib/apt/lists/*

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

# Healthcheck honours both WEB_PORT_HTTPS and TLS_ENABLED so it works for
# the default in-container TLS setup AND for deployments where a reverse
# proxy terminates TLS upstream and the container speaks plain HTTP.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request, ssl; port=os.environ.get('WEB_PORT_HTTPS','8443'); tls=os.environ.get('TLS_ENABLED','true').lower() in ('true','1','yes'); ctx=(ssl._create_unverified_context() if tls else None); urllib.request.urlopen(f'{\"https\" if tls else \"http\"}://localhost:{port}/api/status', context=ctx, timeout=4)"

USER nomadnet

CMD ["/app/entrypoint.sh"]

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CONFIG_HOME=/tmp/aria-config \
    XDG_CACHE_HOME=/tmp/aria-cache \
    ARIA_HOST_ROOT=/host \
    ARIA_DOCKER_SNAPSHOT_FILE=/status/containers.json \
    ARIA_RED_TOKEN_FILE=/run/secrets/red_update_token

COPY install_tunnel.py /tmp/install_tunnel.py
RUN python /tmp/install_tunnel.py \
    && rm /tmp/install_tunnel.py \
    && tunnel-client --version \
    && mkdir -p /app /host/proc /host/etc /run/secrets /run/aria-agent /status

WORKDIR /app
COPY server.py management.py entrypoint.sh *-tools.json ./
RUN chmod 0555 /app/entrypoint.sh /app/server.py \
    && chmod 0444 /app/management.py /app/*-tools.json
USER 65532:65532
# Root's private Git checkout may have mode 0600 files. Validate the actual
# runtime user during the build, before an upgrade stops the working bridge.
RUN python -c "import management, server" && test -x /app/entrypoint.sh

# The health listener stays inside the container. No ports need publishing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, server, management, urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3).read(1024); management.request(os.environ['ARIA_AGENT_SOCKET'], 'capabilities', {}) if os.environ.get('ARIA_AGENT_SOCKET') else None" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]

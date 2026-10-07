#!/bin/sh
set -eu

if [ -z "${CONTROL_PLANE_TUNNEL_ID:-}" ]; then
    echo 'Set CONTROL_PLANE_TUNNEL_ID to the tunnel ID from OpenAI Platform.' >&2
    exit 1
fi
for secret in /run/secrets/control-plane-api-key /run/secrets/red_update_token; do
    if [ ! -f "$secret" ] || [ ! -r "$secret" ] || [ ! -s "$secret" ]; then
        echo 'Mount readable, nonempty control-plane-api-key and red_update_token secret files.' >&2
        exit 1
    fi
done

exec tunnel-client run \
    --control-plane.tunnel-id "$CONTROL_PLANE_TUNNEL_ID" \
    --control-plane.api-key file:/run/secrets/control-plane-api-key \
    --mcp.command 'python /app/server.py' \
    --mcp.max-concurrent-requests 1 \
    --control-plane.max-inflight 8 \
    --health.listen-addr 127.0.0.1:8080
